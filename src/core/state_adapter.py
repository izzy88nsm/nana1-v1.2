# src/core/state_adapter.py

from typing import Optional, Type, Literal, Dict, Any, Callable, List
from datetime import datetime, timedelta
from functools import wraps
import asyncio
import hashlib
import json
import logging

from tenacity import (
    retry, 
    stop_after_attempt, 
    wait_exponential, 
    retry_if_exception_type,
    RetryError
)
from pydantic import ValidationError
from cryptography.fernet import Fernet, InvalidToken
from prometheus_client import Counter, REGISTRY

def create_state_adapter_errors_counter(registry=REGISTRY):
    return Counter(
        'state_adapter_errors_total',
        'State operation errors',
        ['operation', 'type'],
        registry=registry
    )

# Instead of defining globally, call it inside your class/module init
STATE_ERRORS = create_state_adapter_errors_counter()

# Import state management components
from src.core.state_manager_v2 import (
    StateConfig,
    StateManager,
    BotState,
    StateSchema,
    JSONFileStateManager,
    SQLiteStateManager,
    RedisStateManager,
    StateManagerType
)

logger = logging.getLogger(__name__)

# Metrics
STATE_SAVE_TIME = Summary('state_save_duration_seconds', 'Time spent saving state')
STATE_LOAD_TIME = Summary('state_load_duration_seconds', 'Time spent loading state')
STATE_ERRORS = Counter('state_adapter_errors_total', 'State operation errors', ['operation', 'type'])

# Cache configuration
CACHE_TTL = timedelta(minutes=5)
MAX_CACHE_SIZE = 1000

class UnifiedStateManager:
    def __init__(
        self,
        mode: Literal["light", "enterprise"] = "enterprise",
        primary_config: Optional[Dict[str, Any]] = None,
        secondary_config: Optional[Dict[str, Any]] = None,
        enable_caching: bool = True,
        enable_encryption: bool = True,
        enable_validation: bool = True,
        enable_fallback: bool = True
    ):
        self.mode = mode
        self.enable_caching = enable_caching
        self.enable_encryption = enable_encryption
        self.enable_validation = enable_validation
        self.enable_fallback = enable_fallback

        self.primary_manager: Optional[StateManager] = None
        self.secondary_manager: Optional[StateManager] = None
        self.cache: Dict[str, BotState] = {}
        self.lock = asyncio.Lock()
        self.migration_registry: Dict[str, List[Callable]] = {}
        self.metrics: Dict[str, Any] = {
            'last_save': None,
            'last_load': None,
            'error_count': 0
        }

        self._init_managers(primary_config, secondary_config)
        self._init_security()
        self._verify_connection()

    def _init_managers(self, primary: Dict, secondary: Dict) -> None:
        if self.mode == "light":
            self.primary_manager = JSONFileStateManager(
                StateConfig(**primary.get('config', {})),
                primary.get('directory', 'data/state')
            )
        else:
            manager_type = StateManagerType(primary.get('type', StateManagerType.REDIS))
            self.primary_manager = self._create_manager(manager_type, primary)

        if secondary and self.enable_fallback:
            self.secondary_manager = self._create_manager(
                StateManagerType(secondary.get('type', StateManagerType.SQLITE)),
                secondary
            )

    def _create_manager(self, manager_type: StateManagerType, config: Dict) -> StateManager:
        common_args = {
            'config': StateConfig(**config.get('config', {})),
            'directory': config.get('directory'),
            'db_path': config.get('db_path'),
            'redis_url': config.get('redis_url')
        }

        if manager_type == StateManagerType.JSON:
            return JSONFileStateManager(**common_args)
        elif manager_type == StateManagerType.SQLITE:
            return SQLiteStateManager(**common_args)
        elif manager_type == StateManagerType.REDIS:
            return RedisStateManager(**common_args)
        else:
            raise ValueError(f"Unsupported manager type: {manager_type}")

    def _init_security(self) -> None:
        if self.enable_encryption:
            config = self.primary_manager.config if self.primary_manager else StateConfig()
            self.cipher = Fernet(config.encryption_key) if config.encryption_key else None
        else:
            self.cipher = None

    def _verify_connection(self) -> None:
        async def _check():
            if self.primary_manager:
                health = await self.primary_manager.health_check()
                if not health.get('healthy'):
                    raise ConnectionError("Primary state store unavailable")

            if self.secondary_manager and self.enable_fallback:
                health = await self.secondary_manager.health_check()
                if not health.get('healthy'):
                    logger.warning("Secondary state store unavailable")

        asyncio.run(_check())

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((IOError, TimeoutError))
    )
    @STATE_SAVE_TIME.time()
    async def save(self, bot_id: str, state: BotState) -> None:
        async with self.lock:
            try:
                validated_state = self._validate_state(state)
                encrypted_state = self._encrypt_state(validated_state)

                try:
                    await self.primary_manager.save_state(bot_id, encrypted_state)
                except Exception as primary_error:
                    if self.secondary_manager:
                        logger.warning("Primary save failed, using secondary", error=str(primary_error))
                        await self.secondary_manager.save_state(bot_id, encrypted_state)
                    else:
                        raise

                if self.enable_caching:
                    self._update_cache(bot_id, validated_state)

                self.metrics.update({
                    'last_save': datetime.now(),
                    'save_count': self.metrics.get('save_count', 0) + 1
                })

            except Exception as e:
                STATE_ERRORS.labels('save', type(e).__name__).inc()
                self.metrics['error_count'] += 1
                logger.error("State save failed", error=str(e))
                raise

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((IOError, TimeoutError))
    )
    @STATE_LOAD_TIME.time()
    async def load(self, bot_id: str, schema: Type[StateSchema]) -> BotState:
        async with self.lock:
            try:
                if self.enable_caching and bot_id in self.cache:
                    return self._process_loaded_state(bot_id, self.cache[bot_id], schema)

                try:
                    encrypted_state = await self.primary_manager.load_state(bot_id)
                except Exception as primary_error:
                    if self.secondary_manager:
                        logger.warning("Primary load failed, using secondary", error=str(primary_error))
                        encrypted_state = await self.secondary_manager.load_state(bot_id)
                    else:
                        raise

                decrypted_state = self._decrypt_state(encrypted_state)
                validated_state = self._validate_schema(decrypted_state, schema)
                migrated_state = self._apply_migrations(validated_state)

                if self.enable_caching:
                    self._update_cache(bot_id, migrated_state)

                self.metrics.update({
                    'last_load': datetime.now(),
                    'load_count': self.metrics.get('load_count', 0) + 1
                })

                return migrated_state

            except Exception as e:
                STATE_ERRORS.labels('load', type(e).__name__).inc()
                self.metrics['error_count'] += 1
                logger.error("State load failed", error=str(e))
                raise

    def _validate_state(self, state: BotState) -> BotState:
        if self.enable_validation:
            if not state.is_valid:
                raise ValidationError("Invalid state structure")

            if state.metadata.get('version') != self.primary_manager.config.schema_version:
                raise ValidationError("State version mismatch")

            checksum = self._calculate_checksum(state.json())
            if state.metadata.get('checksum') != checksum:
                raise ValidationError("State checksum validation failed")

        return state

    def _encrypt_state(self, state: BotState) -> BotState:
        if self.enable_encryption and self.cipher:
            encrypted_data = self.cipher.encrypt(state.json().encode())
            return BotState.parse_raw(encrypted_data.decode())
        return state

    def _decrypt_state(self, state: BotState) -> BotState:
        if self.enable_encryption and self.cipher:
            try:
                decrypted_data = self.cipher.decrypt(state.json().encode())
                return BotState.parse_raw(decrypted_data.decode())
            except InvalidToken:
                raise SecurityError("Invalid encryption token")
        return state

    def _update_cache(self, bot_id: str, state: BotState) -> None:
        if len(self.cache) >= MAX_CACHE_SIZE:
            self.cache.pop(next(iter(self.cache)))
        self.cache[bot_id] = state

    def register_migration(self, from_version: str, to_version: str, migration_fn: Callable) -> None:
        version_key = f"{from_version}-{to_version}"
        self.migration_registry.setdefault(version_key, []).append(migration_fn)

    def _apply_migrations(self, state: BotState) -> BotState:
        current_version = state.metadata.get('version', '1.0')
        target_version = self.primary_manager.config.schema_version

        while current_version != target_version:
            migration_key = f"{current_version}-{target_version}"
            if migration_key not in self.migration_registry:
                raise MigrationError(f"No migration path from {current_version} to {target_version}")

            for migration in self.migration_registry[migration_key]:
                state = migration(state)
                current_version = state.metadata.get('version')

        return state

    async def health_check(self) -> Dict[str, Any]:
        health = {
            'primary': await self.primary_manager.health_check(),
            'secondary': await self.secondary_manager.health_check() if self.secondary_manager else None,
            'cache': {
                'size': len(self.cache),
                'hit_rate': self._calculate_cache_hit_rate()
            },
            'metrics': self.metrics
        }
        return health

    def _calculate_cache_hit_rate(self) -> float:
        total = self.metrics.get('load_count', 0)
        hits = self.metrics.get('cache_hits', 0)
        return hits / total if total > 0 else 0.0

    def _calculate_checksum(self, data: str) -> str:
        return hashlib.sha256(data.encode()).hexdigest()

    @property
    def encryption_enabled(self) -> bool:
        return self.enable_encryption and self.cipher is not None

    async def flush_cache(self) -> None:
        async with self.lock:
            self.cache.clear()

    async def backup_state(self, bot_id: str) -> None:
        pass

class SecurityError(Exception):
    pass

class MigrationError(Exception):
    pass
