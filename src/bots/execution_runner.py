import asyncio
import logging
from typing import (
    Any, List, Dict, Optional, Type, Protocol, 
    runtime_checkable, AsyncContextManager
)
from types import TracebackType
from datetime import datetime, timedelta
from dataclasses import dataclass
import time
import psutil

logger = logging.getLogger(__name__)

@runtime_checkable
class TradingBotProtocol(Protocol, AsyncContextManager):
    """Protocol defining required bot interface"""
    @property
    def name(self) -> str: ...
    async def execute(self, data: Any) -> None: ...
    async def get_market_data(self) -> Any: ...
    async def shutdown(self) -> None: ...

@dataclass
class BotMetrics:
    success_count: int = 0
    error_count: int = 0
    last_execution: Optional[datetime] = None
    avg_latency: float = 0.0
    circuit_open: bool = False

class BotExecutionError(Exception):
    """Base exception for bot execution failures"""

class CircuitBreakerOpen(BotExecutionError):
    """Raised when bot circuit breaker is open"""

class BotRunner:
    def __init__(
        self,
        bots: List[TradingBotProtocol],
        *,
        loop_delay: float = 1.0,
        fail_fast: bool = False,
        max_concurrency: Optional[int] = None,
        circuit_breaker_threshold: int = 5,
        shutdown_timeout: float = 30.0
    ):
        self.bots = self._validate_bots(bots)
        self.loop_delay = loop_delay
        self.fail_fast = fail_fast
        self.shutdown_timeout = shutdown_timeout
        self.circuit_breaker_threshold = circuit_breaker_threshold
        
        self._task_registry: Dict[str, asyncio.Task] = {}
        self._metrics: Dict[str, BotMetrics] = {}
        self._stop_flag = asyncio.Event()
        self._start_time = datetime.utcnow()
        self._semaphore = asyncio.Semaphore(max_concurrency) if max_concurrency else None
        self._resource_monitor = psutil.Process()

    def _validate_bots(self, bots: List[Any]) -> List[TradingBotProtocol]:
        """Validate bot interface compliance"""
        valid_bots = []
        for idx, bot in enumerate(bots):
            if not isinstance(bot, TradingBotProtocol):
                raise TypeError(
                    f"Bot at index {idx} doesn't implement TradingBotProtocol. "
                    f"Missing required methods: {set(TradingBotProtocol.__abstractmethods__)}"
                )
            valid_bots.append(bot)
        return valid_bots

    async def __aenter__(self) -> "BotRunner":
        """Async context manager entry"""
        return self

    async def __aexit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[TracebackType]
    ) -> None:
        """Async context manager exit"""
        await self.shutdown_all()

    async def run_all(self) -> None:
        """Run all bots with lifecycle management and monitoring"""
        logger.info(
            f"Starting execution of {len(self.bots)} bots (PID: {self._resource_monitor.pid})"
        )
        self._metrics = {bot.name: BotMetrics() for bot in self.bots}

        try:
            async with asyncio.TaskGroup() as tg:
                for bot in self.bots:
                    task = tg.create_task(
                        self._run_bot(bot),
                        name=f"BotTask-{bot.name}"
                    )
                    self._task_registry[bot.name] = task

            logger.info("All bots completed execution normally")
        except* Exception as eg:
            for exc in eg.exceptions:
                logger.critical(f"Critical error in bot execution: {exc}", exc_info=exc)
        finally:
            await self.shutdown_all()

    async def _run_bot(self, bot: TradingBotProtocol) -> None:
        """Managed execution loop with circuit breaker and metrics"""
        logger.info(f"[{bot.name}] Initializing bot execution")
        metrics = self._metrics[bot.name]
        retries = 0

        async with bot:  # Use async context manager if available
            while not self._stop_flag.is_set():
                try:
                    if metrics.circuit_open:
                        raise CircuitBreakerOpen(f"Circuit open for {bot.name}")

                    async with self._semaphore:
                        start_time = time.monotonic()
                        data = await self._safe_get_data(bot)
                        await self._safe_execute(bot, data)
                        execution_time = time.monotonic() - start_time

                        # Update metrics
                        metrics.success_count += 1
                        metrics.last_execution = datetime.utcnow()
                        metrics.avg_latency = (
                            metrics.avg_latency * (metrics.success_count - 1) + execution_time
                        ) / metrics.success_count
                        retries = 0  # Reset retry counter on success

                except CircuitBreakerOpen:
                    await asyncio.sleep(self.loop_delay * 5)  # Backoff when circuit open
                    continue
                except asyncio.CancelledError:
                    logger.warning(f"[{bot.name}] Execution cancelled")
                    break
                except Exception as e:
                    await self._handle_bot_error(bot, e, metrics)
                    retries += 1
                    if retries > 3:
                        logger.error(f"[{bot.name}] Max retries exceeded")
                        break
                finally:
                    await asyncio.sleep(self.loop_delay)

        logger.info(f"[{bot.name}] Execution loop terminated")

    async def _safe_get_data(self, bot: TradingBotProtocol) -> Any:
        """Wrapper for data fetching with resource monitoring"""
        if self._resource_monitor.memory_percent() > 90:
            logger.warning(f"[{bot.name}] High memory usage: {self._resource_monitor.memory_percent()}%")
        
        return await bot.get_market_data()

    async def _safe_execute(self, bot: TradingBotProtocol, data: Any) -> None:
        """Execute with timeout and resource constraints"""
        try:
            await asyncio.wait_for(bot.execute(data), timeout=self.loop_delay * 2)
        except asyncio.TimeoutError:
            logger.error(f"[{bot.name}] Execution timed out")
            raise

    async def _handle_bot_error(
        self,
        bot: TradingBotProtocol,
        error: Exception,
        metrics: BotMetrics
    ) -> None:
        """Error handling with circuit breaker pattern"""
        metrics.error_count += 1
        logger.error(
            f"[{bot.name}] Error in execution cycle: {str(error)}",
            exc_info=not isinstance(error, asyncio.CancelledError)
        )

        if metrics.error_count >= self.circuit_breaker_threshold:
            metrics.circuit_open = True
            logger.critical(f"[{bot.name}] Circuit breaker triggered!")
            if self.fail_fast:
                self._stop_flag.set()

    async def shutdown_all(self) -> None:
        """Graceful shutdown with timeout protection"""
        if self._stop_flag.is_set():
            return

        logger.info("Initiating controlled shutdown sequence...")
        self._stop_flag.set()

        # Phase 1: Cancel running tasks
        for name, task in self._task_registry.items():
            if not task.done():
                task.cancel()

        # Phase 2: Await task completion with timeout
        try:
            await asyncio.wait_for(
                asyncio.gather(*self._task_registry.values(), return_exceptions=True),
                timeout=self.shutdown_timeout
            )
        except asyncio.TimeoutError:
            logger.warning("Shutdown timed out - forcing termination")

        # Phase 3: Individual bot shutdown
        shutdown_tasks = []
        for bot in self.bots:
            shutdown_tasks.append(
                asyncio.create_task(
                    self._safe_shutdown(bot),
                    name=f"Shutdown-{bot.name}"
                )
            )

        done, pending = await asyncio.wait(
            shutdown_tasks,
            timeout=self.shutdown_timeout,
            return_when=asyncio.ALL_COMPLETED
        )

        if pending:
            logger.warning(f"{len(pending)} bots failed to shutdown cleanly")

        logger.info("Shutdown sequence complete")

    async def _safe_shutdown(self, bot: TradingBotProtocol) -> None:
        """Protected shutdown with error handling"""
        try:
            await asyncio.wait_for(bot.shutdown(), timeout=self.shutdown_timeout)
            logger.info(f"[{bot.name}] Shutdown completed successfully")
        except Exception as e:
            logger.warning(f"[{bot.name}] Shutdown error: {str(e)}")

    def status_report(self) -> Dict[str, Dict[str, Any]]:
        """Detailed system status report"""
        return {
            bot.name: {
                "status": self._task_status(bot.name),
                "metrics": self._metrics[bot.name].__dict__,
                "resource_usage": {
                    "cpu": self._resource_monitor.cpu_percent(),
                    "memory": self._resource_monitor.memory_info().rss,
                },
                "uptime": str(datetime.utcnow() - self._start_time)
            }
            for bot in self.bots
        }

    def _task_status(self, name: str) -> str:
        """Get task status with proper state inspection"""
        task = self._task_registry.get(name)
        if not task:
            return "not_started"
        if task.done():
            return "completed" if not task.cancelled() else "cancelled"
        return "running"

    async def kill(self) -> None:
        """Immediate termination procedure"""
        logger.critical("EMERGENCY KILL SWITCH ACTIVATED")
        self._stop_flag.set()
        
        # Cancel all tasks without cleanup
        for task in self._task_registry.values():
            if not task.done():
                task.cancel()

        # Free resources immediately
        await asyncio.sleep(0)  # Yield control to allow cancellations
