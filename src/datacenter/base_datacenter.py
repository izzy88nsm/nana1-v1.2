# src/datacenter/base_datacenter.py

import asyncio
import inspect
import logging
from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from typing import Any, Optional, Dict, AsyncGenerator, TypeVar, Generic, Set
from functools import wraps
from pydantic import ValidationError
import structlog
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
    before_sleep_log,
)
from src.config.config_schema import AppConfig, RetryPolicy
from src.utils.metrics import DataCenterMetrics 

T = TypeVar('T')
logger = structlog.get_logger(__name__)

class DataCenterError(Exception):
    """Base exception for data center operations"""

class DataConnectionError(DataCenterError):
    """Failed to connect to data source"""

class DataValidationError(DataCenterError):
    """Data validation failed"""

def validate_async(func):
    """Decorator to validate method arguments and return values"""
    @wraps(func)
    async def wrapper(self, *args, **kwargs):
        if hasattr(self, 'config') and self.config:
            self._validate_inputs(args, kwargs)
        result = await func(self, *args, **kwargs)
        if hasattr(self, '_validate_output'):
            return self._validate_output(result)
        return result
    return wrapper

class BaseDataCenter(ABC, Generic[T]):
    VERSION = "2.1.0"
    
    def __init__(self, config: AppConfig):
        self.config = config
        self._running = False
        self._tasks: Set[asyncio.Task] = set()
        self._shutdown_lock = asyncio.Lock()
        self._semaphore = asyncio.Semaphore(
            self.config.core.max_concurrency
        )
        self.metrics = DataCenterMetrics(
            component=self.__class__.__name__,
            prom_config=config.monitoring.prometheus
        )
        self._service_registry: Dict[str, Any] = {}

    @property
    def is_running(self) -> bool:
        """Current operational status"""
        return self._running

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.stop()
        
    @retry(
        retry=retry_if_exception_type(DataConnectionError),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        before_sleep=before_sleep_log(logger, logging.WARNING),
    )
    async def start(self) -> None:
        """Initialize and start data processing"""
        if self._running:
            logger.warning("DataCenter already running", instance=self.__class__.__name__)
            return

        try:
            await self._setup_services()
            self._running = True
            logger.info(
                "DataCenter starting",
                version=self.VERSION,
                config=self.config.model_dump(),
                dependencies=list(self._service_registry.keys())
	)
            
            # Start background tasks
            self._create_task(self._monitor_operations())
            self._create_task(self._run_loop())
            
            await self.metrics.start_server()
            logger.info("DataCenter started successfully")

        except ValidationError as e:
            logger.error("Configuration validation failed", error=str(e))
            raise DataValidationError("Invalid configuration") from e
        except Exception as e:
            logger.error("Startup failed", error=str(e))
            await self.stop()
            raise DataConnectionError("Initialization failed") from e

    async def stop(self) -> None:
        """Graceful shutdown procedure"""
        async with self._shutdown_lock:
            if not self._running:
                return

            logger.info("Initiating shutdown sequence")
            self._running = False

            # Cancel ongoing tasks
            for task in self._tasks:
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

            await self._teardown_services()
            await self.metrics.stop_server()
            
            logger.info("Shutdown complete", tasks_cleared=len(self._tasks))
            self._tasks.clear()

    @abstractmethod
    async def _run_loop(self) -> None:
        """Main data processing loop implementation"""
        raise NotImplementedError

    async def _monitor_operations(self) -> None:
        """Monitor system health and resource utilization"""
        while self._running:
            self.metrics.update_metrics(
                active_tasks=len(self._tasks),
                system_load=len([t for t in self._tasks if not t.done()])
            )
            await asyncio.sleep(5)

    @asynccontextmanager
    async def _resource_manager(self, resource_id: str) -> AsyncGenerator[Any, None]:
        """Context manager for resource acquisition/release"""
        async with self._semaphore:
            try:
                resource = await self._acquire_resource(resource_id)
                yield resource
            finally:
                await self._release_resource(resource_id)
                self.metrics.resource_released(resource_id)

    def _create_task(self, coro) -> asyncio.Task:
        """Create and track managed tasks"""
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(lambda t: self._tasks.remove(t))
        return task

    async def _setup_services(self) -> None:
        """Initialize required external services"""
        # Example implementation - extend in subclasses
        try:
            logger.debug("Initializing service dependencies")
            # self._service_registry["db"] = await Database.connect()
            # self._service_registry["cache"] = await CachePool.create()
            pass
        except Exception as e:
            logger.error("Service initialization failed", error=str(e))
            raise DataConnectionError("Service setup failed") from e

    async def _teardown_services(self) -> None:
        """Cleanup external service connections"""
        logger.debug("Cleaning up services")
        for name, service in self._service_registry.items():
            try:
                if hasattr(service, 'close'):
                    await service.close()
                elif hasattr(service, 'disconnect'):
                    await service.disconnect()
            except Exception as e:
                logger.warning("Service cleanup failed", service=name, error=str(e))

    async def health_check(self) -> Dict[str, Any]:
        """System health status report"""
        return {
            "status": "OK" if self._running else "DOWN",
            "version": self.VERSION,
            "active_tasks": len(self._tasks),
            "dependencies": {
                name: "OK" if service.is_ready() else "ERROR"
                for name, service in self._service_registry.items()
            },
            "metrics": self.metrics.current_stats(),
        }

    # Example usage pattern
    @validate_async
    async def process_data(self, data: T) -> T:
        """Process data sample with validation"""
        async with self._resource_manager("data_processor"):
            processed = await self._transform_data(data)
            self.metrics.data_processed(len(data))
            return processed

    async def _transform_data(self, data: T) -> T:
        """Data transformation implementation (override in subclasses)"""
        return data

    # Implementation hooks for subclasses
    async def _acquire_resource(self, resource_id: str) -> Any:
        """Resource acquisition strategy (override in subclasses)"""
        raise NotImplementedError

    async def _release_resource(self, resource_id: str) -> None:
        """Resource release strategy (override in subclasses)"""
        raise NotImplementedError

    def _validate_inputs(self, args, kwargs) -> None:
        """Input validation logic (override in subclasses)"""
        pass

    def _validate_output(self, result) -> Any:
        """Output validation logic (override in subclasses)"""
        return result
