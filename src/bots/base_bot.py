# src/bots/base_bot.py

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, Type, Callable, cast, TypeVar, Generic
from datetime import datetime, timedelta
from contextlib import AsyncExitStack, contextmanager, asynccontextmanager
from functools import partial
import inspect
import traceback
from pydantic import BaseModel, ValidationError
from cryptography.fernet import Fernet

from src.core.state_manager import create_state_manager, BotState, StateManager
from .plugins import PluginManager, PluginContext
from .journal import TradeJournal, AuditEntry, EntryType
from .healthcheck import DependencyManager, HealthStatus
from .strategy_loader import StrategyLoader, StrategyReloadError
from .execution_runner import BotRunner  
from src.utils.metrics import ExecutionMetrics
from src.utils.decorators import EnhancedDecorators
from src.utils.exceptions import (
    CriticalError,
    TransientError,
    DegradedServiceError
)

logger = logging.getLogger(__name__)
MAX_CONSECUTIVE_ERRORS = 10  # Circuit breaker threshold
STATE_AUTOSAVE_INTERVAL = 300  # 5 minutes

T = TypeVar('T', bound=BaseModel)

class BotConfig(BaseModel):
    enabled: bool = True
    version: str
    max_concurrency: int = 10
    safe_mode_enabled: bool = True
    encrypted_fields: list[str] = []

class BaseBot(ABC, Generic[T]):
    VERSION = "4.2.0"
    _instance_lock = asyncio.Lock()

    def __init__(
        self,
        name: str,
        strategy_config: Dict[str, Any],
        config: T,
        asset: Optional[str] = None,
        exchange: Optional[str] = None,
        state_source: str = "json",
        state_path: str = "bot_state.json",
        plugin_manager: Optional[PluginManager] = None,
        journal: Optional[TradeJournal] = None,
        health: Optional[DependencyManager] = None,
        metrics: Optional[ExecutionMetrics] = None,
        encryption_key: Optional[bytes] = None
    ):
        self.name = name
        self.config = self._validate_config(config)
        self.asset = asset
        self.exchange = exchange
        self.encryption = Fernet(encryption_key) if encryption_key else None

        # State management initialization
        self.state_manager = self._init_state_manager(state_source, state_path)
        self._state: BotState = BotState()

        # Remaining initialization remains the same
        self.plugin_manager = plugin_manager or PluginManager()
        self.journal = journal or TradeJournal(name)
        self.health = health or DependencyManager()
        self.metrics = metrics or ExecutionMetrics()
        self.strategy_loader = StrategyLoader(strategy_config)

        # Runtime state
        self._strategy = None
        self._shutdown_flag = asyncio.Event()
        self._execution_lock = asyncio.Semaphore(self.config.max_concurrency)
        self._circuit_breaker_state = "closed"
        self._strategy_lock = asyncio.Lock()
        self._autosave_task: Optional[asyncio.Task] = None

        self._register_core_plugins()
        self._setup_metrics()
        self._register_lifecycle_hooks()

    def _init_state_manager(self, state_source: str, state_path: str) -> StateManager:
        """Initialize state manager with proper configuration"""
        try:
            return create_state_manager(
                source=state_source,
                filepath=state_path,
                encryption=self.encryption
            )
        except (ValueError, ImportError) as e:
            self._log_config_error(e, state_source)
            raise

    async def initialize(self) -> None:
        """Async initialization with state loading"""
        await self._load_state()
        await self._load_strategy_async()
        await self._verify_dependencies()
        self._start_background_tasks()

    async def _load_state(self) -> None:
        """Load bot state from persistent storage"""
        self._state = await self.state_manager.load() or BotState()
        logger.info(f"Loaded state for {self.name}: {self._state}")

    async def _handle_success(self, result: Any) -> Any:
        """Handle successful execution with state persistence"""
        self._state.consecutive_errors = 0
        self.metrics.increment('execution.success')
        await self.plugin_manager.trigger_hook('execution_success', result)
        await self.state_manager.save(self._state)
        return result

    async def _handle_error(self, error: Exception):
        """Handle execution errors with state persistence"""
        self._state.consecutive_errors += 1
        self.metrics.increment('execution.errors')
        
        await self.plugin_manager.trigger_hook(
            'execution_error',
            error,
            bot=self,
            state=self._state
        )
        
        if isinstance(error, CriticalError):
            await self._enter_safe_mode()
        
        await self.state_manager.save(self._state)

    async def _enter_safe_mode(self) -> None:
        """Enter failsafe mode with state persistence"""
        if not self.config.safe_mode_enabled:
            return
            
        self._circuit_breaker_state = "open"
        self.journal.log(AuditEntry(
            entry_type=EntryType.SAFE_MODE,
            message="Entered failsafe mode",
            details={"state": self._state.dict()}
        ))
        
        if hasattr(self, "safe_strategy"):
            self._strategy = self.safe_strategy()
            await self.plugin_manager.trigger_hook('safe_mode_activated', self)
        
        await self.state_manager.save(self._state)

    async def shutdown(self):
        """Enhanced shutdown with state persistence"""
        self._shutdown_flag.set()
        await self.plugin_manager.trigger_hook('pre_shutdown', self)
        
        if self._autosave_task:
            self._autosave_task.cancel()
            await self.state_manager.save(self._state)
        
        await self.plugin_manager.trigger_hook('post_shutdown', self)
        logger.info(f"Bot {self.name} shutdown completed")

    # Remaining methods remain unchanged
    # ... (rest of the original class implementation)


    async def handle_market_data(self, market_data):
        if self.strategy.should_buy(market_data):
            await self.execute_trade('buy', market_data)
        elif self.strategy.should_sell(market_data):
            await self.execute_trade('sell', market_data)
    