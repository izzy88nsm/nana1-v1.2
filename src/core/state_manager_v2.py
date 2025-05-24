# src/core/state_manager_v2.py

from typing import Optional, Dict, Any, Protocol, TypeVar, Generic
from pydantic import (
    BaseModel,
    Field,
    SecretStr,
    field_validator,
    model_validator,
    ConfigDict
)
from enum import Enum
import abc
import asyncio
import logging
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta  # Added timedelta
from functools import wraps
from cryptography.fernet import Fernet, InvalidToken
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    RetryError
)
import aiosqlite
from redis.asyncio import Redis, ConnectionPool
from redis.exceptions import RedisError
import aiofiles
import hashlib
from prometheus_client import Summary, Counter
import zlib

logger = logging.getLogger(__name__)

# Metrics
STATE_OPS = Summary('state_operations_seconds', 'State operation latency', ['operation'])
STATE_ERRORS = Counter('state_errors_total', 'State operation errors', ['operation', 'type'])

# ----- Circuit Breaker Implementation ----- #
class CircuitOpenError(Exception):
    """Circuit breaker is open"""

def circuit_breaker(max_failures=3, reset_timeout=60):
    def decorator(func):
        @wraps(func)
        async def wrapper(self, *args, **kwargs):
            if self._circuit_breaker_failures >= max_failures:
                if (datetime.now(timezone.utc) - self._circuit_breaker_last_failure).total_seconds() > reset_timeout:
                    self._circuit_breaker_failures = 0
                    self._circuit_breaker_last_failure = None
                else:
                    raise CircuitOpenError("Service unavailable")

            try:
                result = await func(self, *args, **kwargs)
                self._circuit_breaker_failures = 0
                return result
            except Exception as e:
                self._circuit_breaker_failures += 1
                self._circuit_breaker_last_failure = datetime.now(timezone.utc)
                raise
        return wrapper
    return decorator

# ----- Security Exceptions ----- #
class SecurityError(Exception):
    """Cryptography-related exceptions"""

# Type variables
T = TypeVar('T', bound='BaseModel')

# ----- Enhanced Config Model ----- #
class StateConfig(BaseModel):
    encryption_key: Optional[SecretStr] = Field(
        None,
        description="Fernet key for AES-128 encryption"
    )
    schema_version: str = "2.1"
    timeout_seconds: int = 10
    max_retries: int = 3
    backup_enabled: bool = True
    cache_ttl: int = 300  # 5 minutes
    validate_checksum: bool = True
    compression_enabled: bool = True
    backup_retention: int = 7  # days

    @field_validator('encryption_key')
    @classmethod
    def validate_key_length(cls, v: Optional[SecretStr]) -> Optional[SecretStr]:
        if v is not None:
            if len(v.get_secret_value()) != 44:
                raise ValueError("Invalid Fernet key length")
        return v

# ----- State Data Contracts ----- #
class StateSchema(BaseModel):
    model_config = ConfigDict(validate_assignment=True)
    
    metadata: Dict[str, Any] = Field(
        default_factory=lambda: {
            "version": "2.1",
            "created_at": datetime.now(timezone.utc)
        }
    )
    checksum_hash: Optional[str] = None

    @model_validator(mode='after')
    def generate_checksum(self) -> 'StateSchema':
        data_dict = self.model_dump(exclude={'checksum_hash'}, mode='json')
        data = json.dumps(data_dict, sort_keys=True)
        checksum = hashlib.sha256(data.encode()).hexdigest()
        return self.model_copy(update={'checksum_hash': checksum})

class BotState(StateSchema):
    last_modified: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    encrypted_data: Optional[bytes] = None
    version_lock: str = "2.1"

    @property
    def is_valid(self) -> bool:
        data_dict = self.model_dump(exclude={'checksum_hash'}, mode='json')
        data = json.dumps(data_dict, sort_keys=True)
        current_hash = hashlib.sha256(data.encode()).hexdigest()
        return current_hash == self.checksum_hash

# ----- Supported Backends ----- #
class StateManagerType(str, Enum):
    JSON = "json"
    SQLITE = "sqlite"
    REDIS = "redis"
    MEMORY = "memory"

# ----- Interface Contract ----- #
class StateManager(Protocol):
    config: StateConfig
    _circuit_open: bool
    _last_failure: datetime

    async def save_state(self, bot_id: str, state: BotState) -> None:
        ...

    async def load_state(self, bot_id: str) -> BotState:
        ...

    async def health_check(self) -> Dict[str, Any]:
        ...

    async def backup_state(self, bot_id: str) -> None:
        ...

    async def restore_state(self, bot_id: str) -> BotState:
        ...

# ----- Enhanced Backend Implementations ----- #
class JSONFileStateManager:
    def __init__(self, config: StateConfig, directory: str = "data/state"):
        self.config = config
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._cipher = Fernet(config.encryption_key.get_secret_value()) if config.encryption_key else None
        self._lock = asyncio.Lock()
        self._circuit_breaker_failures = 0
        self._circuit_breaker_last_failure: Optional[datetime] = None

    def _compress(self, data: str) -> bytes:
        return zlib.compress(data.encode())

    def _decompress(self, data: bytes) -> str:
        return zlib.decompress(data).decode()

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((IOError, TimeoutError))
    )
    @circuit_breaker()
    @STATE_OPS.labels('save').time()
    async def save_state(self, bot_id: str, state: BotState) -> None:
        async with self._lock:
            try:
                json_data = state.model_dump_json()
                if self.config.compression_enabled:
                    processed_data = self._compress(json_data)
                else:
                    processed_data = json_data.encode()
                
                encrypted = self._encrypt_data(processed_data)
                path = self.directory / f"{bot_id}.json"
                async with aiofiles.open(path, "wb") as f:
                    await f.write(encrypted)
                
                if self.config.backup_enabled:
                    await self._create_backup(bot_id, encrypted)
            except Exception as e:
                STATE_ERRORS.labels('save', type(e).__name__).inc()
                logger.error("Save failed", exc_info=True)
                raise

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((IOError, TimeoutError))
    )
    @circuit_breaker()
    @STATE_OPS.labels('load').time()
    async def load_state(self, bot_id: str) -> BotState:
        async with self._lock:
            try:
                path = self.directory / f"{bot_id}.json"
                async with aiofiles.open(path, "rb") as f:
                    encrypted = await f.read()
                decrypted = self._decrypt_data(encrypted)
                
                if self.config.compression_enabled:
                    decompressed = self._decompress(decrypted)
                else:
                    decompressed = decrypted.decode()
                
                return BotState.model_validate_json(decompressed)
            except FileNotFoundError:
                return BotState()
            except Exception as e:
                STATE_ERRORS.labels('load', type(e).__name__).inc()
                logger.error("Load failed", exc_info=True)
                raise

    def _encrypt_data(self, data: bytes) -> bytes:
        if self._cipher:
            return self._cipher.encrypt(data)
        return data

    def _decrypt_data(self, data: bytes) -> bytes:
        if self._cipher:
            try:
                return self._cipher.decrypt(data)
            except InvalidToken:
                raise SecurityError("Invalid decryption token")
        return data

    async def _create_backup(self, bot_id: str, data: bytes) -> None:
        backup_dir = self.directory / "backups"
        backup_dir.mkdir(exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        async with aiofiles.open(backup_dir / f"{bot_id}_{timestamp}.bak", "wb") as f:
            await f.write(data)
        await self._cleanup_old_backups(bot_id)

    async def _cleanup_old_backups(self, bot_id: str) -> None:
        backup_dir = self.directory / "backups"
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.config.backup_retention)
        async for file in backup_dir.glob(f"{bot_id}_*.bak"):
            if file.stat().st_mtime < cutoff.timestamp():
                await file.unlink()

class SQLiteStateManager:
    def __init__(self, config: StateConfig, db_path: str = "state.db"):
        self.config = config
        self.db_path = Path(db_path)
        self._cipher = Fernet(config.encryption_key.get_secret_value()) if config.encryption_key else None

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((IOError, TimeoutError))
    )
    @circuit_breaker()
    @STATE_OPS.labels('save').time()
    async def save_state(self, bot_id: str, state: BotState) -> None:
        encrypted = self._encrypt_data(state.model_dump_json())
        async with aiosqlite.connect(self.db_path) as conn:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS bot_states (
                    bot_id TEXT PRIMARY KEY,
                    state BLOB,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await conn.execute("""
                INSERT INTO bot_states (bot_id, state)
                VALUES (?, ?)
                ON CONFLICT(bot_id) DO UPDATE SET
                    state = excluded.state,
                    updated_at = CURRENT_TIMESTAMP
            """, (bot_id, encrypted))
            await conn.commit()

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((IOError, TimeoutError))
    )
    @circuit_breaker()
    @STATE_OPS.labels('load').time()
    async def load_state(self, bot_id: str) -> BotState:
        async with aiosqlite.connect(self.db_path) as conn:
            cursor = await conn.execute(
                "SELECT state FROM bot_states WHERE bot_id = ?",
                (bot_id,)
            )
            row = await cursor.fetchone()
            if row and row[0]:
                decrypted = self._decrypt_data(row[0])
                return BotState.model_validate_json(decrypted.decode())
            return BotState()

class RedisStateManager:
    def __init__(self, config: StateConfig, redis_url: str = "redis://localhost:6379"):
        self.config = config
        self.pool = ConnectionPool.from_url(
            redis_url,
            max_connections=10,
            ssl_cert_reqs="required" if "rediss://" in redis_url else None
        )
        self._cipher = Fernet(config.encryption_key.get_secret_value()) if config.encryption_key else None

    @property
    def client(self) -> Redis:
        return Redis(connection_pool=self.pool)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((RedisError, TimeoutError))
    )
    @circuit_breaker()
    @STATE_OPS.labels('save').time()
    async def save_state(self, bot_id: str, state: BotState) -> None:
        async with self.client as conn:
            encrypted = self._encrypt_data(state.model_dump_json().encode())
            await conn.set(
                f"bot:state:{bot_id}",
                encrypted,
                ex=self.config.cache_ttl
            )

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type((RedisError, TimeoutError))
    )
    @circuit_breaker()
    @STATE_OPS.labels('load').time()
    async def load_state(self, bot_id: str) -> BotState:
        async with self.client as conn:
            data = await conn.get(f"bot:state:{bot_id}")
            if data:
                decrypted = self._decrypt_data(data)
                return BotState.model_validate_json(decrypted.decode())
            return BotState()

# ----- Factory Pattern ----- #
class StateManagerFactory:
    @staticmethod
    def create_manager(
        manager_type: StateManagerType,
        config: StateConfig,
        **kwargs
    ) -> StateManager:
        if manager_type == StateManagerType.JSON:
            return JSONFileStateManager(config, **kwargs)
        elif manager_type == StateManagerType.SQLITE:
            return SQLiteStateManager(config, **kwargs)
        elif manager_type == StateManagerType.REDIS:
            return RedisStateManager(config, **kwargs)
        else:
            raise ValueError(f"Unsupported manager type: {manager_type}")

# ----- Security Exceptions ----- #
class SecurityError(Exception):
    """Cryptography-related exceptions"""

# ----- Circuit Breaker Decorator ----- #
def circuit_breaker(max_failures=3, reset_timeout=60):
    def decorator(func):
        @wraps(func)
        async def wrapper(self, *args, **kwargs):
            if self._circuit_breaker_failures >= max_failures:
                if (datetime.now(timezone.utc) - self._circuit_breaker_last_failure).total_seconds() > reset_timeout:
                    self._circuit_breaker_failures = 0
                    self._circuit_breaker_last_failure = None
                else:
                    raise CircuitOpenError("Service unavailable")

            try:
                result = await func(self, *args, **kwargs)
                self._circuit_breaker_failures = 0
                return result
            except Exception as e:
                self._circuit_breaker_failures += 1
                self._circuit_breaker_last_failure = datetime.now(timezone.utc)
                raise
        return wrapper
    return decorator

class CircuitOpenError(Exception):
    """Circuit breaker is open"""
