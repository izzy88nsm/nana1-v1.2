import json
import logging
from pathlib import Path
from abc import ABC, abstractmethod
from typing import Optional, Dict, Any, Union, Type
from datetime import datetime
from pydantic import BaseModel, ValidationError
import asyncio

logger = logging.getLogger(__name__)

# ---------- Serializable Bot State ---------- #
class BotState(BaseModel):
    last_success: Optional[datetime] = None
    consecutive_errors: int = 0
    circuit_state: str = "closed"  # Possible states: closed, open, half-open

    def reset(self):
        self.last_success = None
        self.consecutive_errors = 0
        self.circuit_state = "closed"

# ---------- State Manager Interface ---------- #
class StateManager(ABC):
    @abstractmethod
    async def load(self) -> Optional[BotState]:
        """Asynchronously load bot state from storage"""
        pass

    @abstractmethod
    async def save(self, state: BotState) -> None:
        """Asynchronously persist bot state to storage"""
        pass

    @abstractmethod
    async def flush(self) -> None:
        """Flush any buffered operations"""
        pass

    @staticmethod
    def create(bot_name: str, backend: str = "json", **kwargs) -> 'StateManager':
        """Factory method for creating state managers"""
        implementations = {
            "json": JSONFileStateManager,
            "redis": RedisStateManager,
            "sqlite": SQLiteStateManager
        }
        
        if backend not in implementations:
            raise ValueError(f"Unsupported backend: {backend}")
            
        return implementations[backend](bot_name, **kwargs)

# ---------- JSON File Backend ---------- #
try:
    import aiofiles

    class JSONFileStateManager(StateManager):
        def __init__(self, bot_name: str, state_dir: Union[str, Path] = "data/state"):
            self.bot_name = bot_name
            self.file_path = Path(state_dir) / f"{bot_name}_state.json"
            self.file_path.parent.mkdir(parents=True, exist_ok=True)

        async def load(self) -> Optional[BotState]:
            if not self.file_path.exists():
                return None
            try:
                async with aiofiles.open(self.file_path, 'r') as f:
                    raw = await f.read()
                return BotState.model_validate_json(raw)
            except (ValidationError, json.JSONDecodeError) as e:
                logger.warning(f"Invalid state data for {self.bot_name}: {e}")
            except Exception as e:
                logger.error(f"Failed to load state for {self.bot_name}: {e}")
            return None

        async def save(self, state: BotState) -> None:
            try:
                async with aiofiles.open(self.file_path, 'w') as f:
                    await f.write(state.model_dump_json(indent=2))
            except Exception as e:
                logger.error(f"Failed to save state for {self.bot_name}: {e}")
                raise

        async def flush(self) -> None:
            pass

except ImportError:
    logger.warning("aiofiles not installed. JSONFileStateManager disabled.")
    JSONFileStateManager = None  # type: ignore

# ---------- Redis Backend ---------- #
try:
    from redis.asyncio import Redis
    import redis.exceptions as redis_exceptions

    class RedisStateManager(StateManager):
        def __init__(self, bot_name: str, redis_url: str = "redis://localhost"):
            self.bot_name = bot_name
            self.redis_url = redis_url
            self.key = f"bot:{bot_name}:state"
            self._client = None

        async def _get_client(self) -> Redis:
            if not self._client or await self._client.ping() is False:
                self._client = Redis.from_url(self.redis_url,
                                            socket_timeout=5,
                                            socket_connect_timeout=5)
            return self._client

        async def load(self) -> Optional[BotState]:
            try:
                client = await self._get_client()
                raw = await client.get(self.key)
                return BotState.model_validate_json(raw) if raw else None
            except (redis_exceptions.RedisError, ValidationError) as e:
                logger.error(f"Redis load failed for {self.bot_name}: {e}")
                return None

        async def save(self, state: BotState) -> None:
            try:
                client = await self._get_client()
                await client.set(self.key, state.model_dump_json())
            except redis_exceptions.RedisError as e:
                logger.error(f"Redis save failed for {self.bot_name}: {e}")
                raise

        async def flush(self) -> None:
            if self._client:
                await self._client.close()
                self._client = None

except ImportError:
    logger.warning("redis-py not installed. RedisStateManager disabled.")
    RedisStateManager = None  # type: ignore

# ---------- SQLite Backend ---------- #
try:
    import aiosqlite

    class SQLiteStateManager(StateManager):
        def __init__(self, bot_name: str, db_path: str = "data/state/bot_states.db"):
            self.bot_name = bot_name
            self.db_path = Path(db_path)
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = None

        async def _get_connection(self) -> aiosqlite.Connection:
            if not self._conn:
                self._conn = await aiosqlite.connect(self.db_path)
                await self._conn.execute('''CREATE TABLE IF NOT EXISTS bot_states
                    (bot_name TEXT PRIMARY KEY, state_json TEXT)''')
                await self._conn.commit()
            return self._conn

        async def load(self) -> Optional[BotState]:
            try:
                conn = await self._get_connection()
                async with conn.execute(
                    "SELECT state_json FROM bot_states WHERE bot_name = ?",
                    (self.bot_name,)
                ) as cursor:
                    row = await cursor.fetchone()
                    return BotState.model_validate_json(row[0]) if row else None
            except (aiosqlite.Error, ValidationError) as e:
                logger.error(f"SQLite load failed for {self.bot_name}: {e}")
                return None

        async def save(self, state: BotState) -> None:
            try:
                conn = await self._get_connection()
                await conn.execute('''INSERT OR REPLACE INTO bot_states
                    (bot_name, state_json) VALUES (?, ?)''',
                    (self.bot_name, state.model_dump_json()))
                await conn.commit()
            except aiosqlite.Error as e:
                logger.error(f"SQLite save failed for {self.bot_name}: {e}")
                raise

        async def flush(self) -> None:
            if self._conn:
                await self._conn.close()
                self._conn = None

except ImportError:
    logger.warning("aiosqlite not installed. SQLiteStateManager disabled.")
    SQLiteStateManager = None  # type: ignore
