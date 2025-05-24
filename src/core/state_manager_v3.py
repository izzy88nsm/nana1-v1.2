# src/core/state_manager.py
import asyncio
import json
import logging
import zlib
from abc import ABC, abstractmethod
from datetime import datetime, timezone, timedelta
from enum import Enum
from functools import wraps
from pathlib import Path
from typing import Optional, Union, Dict, Any, Type, Protocol

import aiofiles
import aiosqlite
from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator, ConfigDict
from redis.asyncio import Redis, ConnectionPool
from redis.exceptions import RedisError
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from prometheus_client import Summary, Counter, Gauge

logger = logging.getLogger(__name__)

# Metrics
STATE_OPS = Summary('state_operations_seconds', 'State operation latency', ['operation', 'backend'])
STATE_ERRORS = Counter('state_errors_total', 'State operation errors', ['operation', 'backend', 'type'])
STATE_CIRCUIT = Gauge('state_circuit_state', 'Current circuit breaker state', ['backend', 'state'])

# ---------- Core Models ---------- #
class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half-open"

class StateConfig(BaseModel):
    """Unified configuration model for state management"""
    encryption_key: Optional[SecretStr] = Field(
        None,
        description="Fernet key for AES-128 encryption (44-character URL-safe base64)"
    )
    compression_level: int = Field(6, ge=0, le=9)
    circuit_threshold: int = 5
    retry_attempts: int = 3
    backup_retention: int = 7  # days
    cache_ttl: int = 300  # 5 minutes
    validate_checksum: bool = True
    schema_version: str = "2.2"

    @field_validator('encryption_key')
    @classmethod
    def validate_key_length(cls, value: Optional[SecretStr]) -> Optional[SecretStr]:
        if value is not None and len(value.get_secret_value()) != 44:
            raise ValueError("Invalid Fernet key length")
        return value

class BotState(BaseModel):
    """Enhanced state model with versioning and integrity checks"""
    model_config = ConfigDict(validate_assignment=True)
    
    last_modified: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: Dict[str, Any] = Field(default_factory=dict)
    checksum: Optional[str] = None
    version_lock: str = "2.2"
    encrypted_data: Optional[bytes] = None

    @model_validator(mode='after')
    def generate_checksum(self) -> 'BotState':
        data = self.model_dump(exclude={'checksum', 'encrypted_data'}, mode='json')
        self.checksum = hashlib.sha256(json.dumps(data, sort_keys=True).hexdigest()
        self.checksum = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()

    @property
    def is_valid(self) -> bool:
        data = self.model_dump(exclude={'checksum', 'encrypted_data'}, mode='json')
        return self.checksum == hashlib.sha256(json.dumps(data, sort_keys=True).hexdigest()

# ---------- Core Interface ---------- #
class StateManager(Protocol):
    @abstractmethod
    async def save_state(self, bot_id: str, state: BotState) -> None:
        """Save state for a specific bot"""
        
    @abstractmethod
    async def load_state(self, bot_id: str) -> BotState:
        """Load state for a specific bot"""
        
    @abstractmethod
    async def health_check(self) -> Dict[str, Any]:
        """Check backend health status"""
        
    @abstractmethod
    async def rotate_backups(self) -> None:
        """Maintain backup rotation"""

# ---------- Decorators ---------- #
def circuit_breaker(max_failures=3, reset_timeout=60):
    def decorator(func):
        @wraps(func)
        async def wrapper(self, *args, **kwargs):
            if self._failures >= max_failures:
                if (datetime.now(timezone.utc) - self._last_failure).total_seconds() > reset_timeout:
                    self._failures = 0
                else:
                    STATE_CIRCUIT.labels(self.backend_type, 'open').inc()
                    raise CircuitOpenError("Service unavailable")
            
            try:
                result = await func(self, *args, **kwargs)
                self._failures = 0
                STATE_CIRCUIT.labels(self.backend_type, 'closed').inc()
                return result
            except Exception as e:
                self._failures += 1
                self._last_failure = datetime.now(timezone.utc)
                STATE_CIRCUIT.labels(self.backend_type, 'half-open').inc()
                logger.error("Operation failed", exc_info=e)
                raise
        return wrapper
    return decorator

# ---------- Implementations ---------- #
class JSONStateManager:
    """Secure JSON file storage with compression and backup rotation"""
    backend_type = "json"

    def __init__(self, config: StateConfig, directory: Union[str, Path]):
        self.config = config
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._cipher = Fernet(config.encryption_key.get_secret_value()) if config.encryption_key else None
        self._lock = asyncio.Lock()
        self._failures = 0
        self._last_failure = None

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(), retry=retry_if_exception_type(IOError))
    @circuit_breaker()
    @STATE_OPS.labels('save', 'json').time()
    async def save_state(self, bot_id: str, state: BotState) -> None:
        async with self._lock:
            try:
                path = self.directory / f"{bot_id}.json"
                data = state.model_dump_json()
                
                if self.config.compression_level > 0:
                    data = zlib.compress(data.encode(), self.config.compression_level)
                
                if self._cipher:
                    data = self._cipher.encrypt(data)
                
                async with aiofiles.open(path, "wb") as f:
                    await f.write(data)
                
                await self._create_backup(bot_id, data)
            except Exception as e:
                STATE_ERRORS.labels('save', 'json', type(e).__name__).inc()
                raise

    async def _create_backup(self, bot_id: str, data: bytes) -> None:
        backup_dir = self.directory / "backups"
        backup_dir.mkdir(exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        async with aiofiles.open(backup_dir / f"{bot_id}_{timestamp}.bak", "wb") as f:
            await f.write(data)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(), retry=retry_if_exception_type(IOError))
    @circuit_breaker()
    @STATE_OPS.labels('load', 'json').time()
    async def load_state(self, bot_id: str) -> BotState:
        async with self._lock:
            path = self.directory / f"{bot_id}.json"
            if not path.exists():
                return BotState()

            async with aiofiles.open(path, "rb") as f:
                data = await f.read()
            
            if self._cipher:
                data = self._cipher.decrypt(data)
            
            if self.config.compression_level > 0:
                data = zlib.decompress(data)
            
            return BotState.model_validate_json(data.decode())

    async def rotate_backups(self) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.config.backup_retention)
        backup_dir = self.directory / "backups"
        
        async for file in backup_dir.glob("*.bak"):
            if file.stat().st_mtime < cutoff.timestamp():
                await file.unlink()

class RedisStateManager:
    """Redis-backed state storage with connection pooling"""
    backend_type = "redis"

    def __init__(self, config: StateConfig, redis_url: str):
        self.config = config
        self.pool = ConnectionPool.from_url(
            redis_url,
            max_connections=10,
            decode_responses=False
        )
        self._cipher = Fernet(config.encryption_key.get_secret_value()) if config.encryption_key else None
        self._failures = 0
        self._last_failure = None

    @property
    def client(self) -> Redis:
        return Redis(connection_pool=self.pool)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(), retry=retry_if_exception_type(RedisError))
    @circuit_breaker()
    @STATE_OPS.labels('save', 'redis').time()
    async def save_state(self, bot_id: str, state: BotState) -> None:
        async with self.client as conn:
            data = state.model_dump_json().encode()
            if self._cipher:
                data = self._cipher.encrypt(data)
            await conn.setex(f"bot:{bot_id}:state", self.config.cache_ttl, data)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(), retry=retry_if_exception_type(RedisError))
    @circuit_breaker()
    @STATE_OPS.labels('load', 'redis').time()
    async def load_state(self, bot_id: str) -> BotState:
        async with self.client as conn:
            data = await conn.get(f"bot:{bot_id}:state")
            if not data:
                return BotState()
            
            if self._cipher:
                data = self._cipher.decrypt(data)
            
            return BotState.model_validate_json(data.decode())

class SQLiteStateManager:
    """SQLite-based state storage with encryption"""
    backend_type = "sqlite"

    def __init__(self, config: StateConfig, db_path: Union[str, Path]):
        self.config = config
        self.db_path = Path(db_path)
        self._cipher = Fernet(config.encryption_key.get_secret_value()) if config.encryption_key else None
        self._failures = 0
        self._last_failure = None

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(), retry=retry_if_exception_type(IOError))
    @circuit_breaker()
    @STATE_OPS.labels('save', 'sqlite').time()
    async def save_state(self, bot_id: str, state: BotState) -> None:
        async with aiosqlite.connect(self.db_path) as conn:
            data = state.model_dump_json()
            if self._cipher:
                data = self._cipher.encrypt(data.encode())
            
            await conn.execute("""
                INSERT INTO states (bot_id, data) 
                VALUES (?, ?)
                ON CONFLICT(bot_id) DO UPDATE SET data=excluded.data
            """, (bot_id, data))
            await conn.commit()

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(), retry=retry_if_exception_type(IOError))
    @circuit_breaker()
    @STATE_OPS.labels('load', 'sqlite').time()
    async def load_state(self, bot_id: str) -> BotState:
        async with aiosqlite.connect(self.db_path) as conn:
            cursor = await conn.execute("SELECT data FROM states WHERE bot_id=?", (bot_id,))
            row = await cursor.fetchone()
            if not row:
                return BotState()
            
            data = row[0]
            if self._cipher:
                data = self._cipher.decrypt(data).decode()
            
            return BotState.model_validate_json(data)

# ---------- Factory ---------- #
class StateManagerFactory:
    @staticmethod
    def create(config: StateConfig, backend_type: str, **kwargs) -> StateManager:
        match backend_type.lower():
            case "json":
                return JSONStateManager(config, **kwargs)
            case "redis":
                return RedisStateManager(config, **kwargs)
            case "sqlite":
                return SQLiteStateManager(config, **kwargs)
            case _:
                raise ValueError(f"Unsupported backend: {backend_type}")

# ---------- Exceptions ---------- #
class CircuitOpenError(Exception):
    """Circuit breaker is in open state"""

class SecurityError(Exception):
    """Data security validation failed"""