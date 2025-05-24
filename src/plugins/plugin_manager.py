# src/plugins/plugin_manager.py

import asyncio
import inspect
import time
from enum import Enum, auto
from typing import Any, Dict, Optional, List, Callable, TypedDict, Coroutine

import structlog
from pydantic import BaseModel, ValidationError, validator
from tenacity import (
    retry, 
    stop_after_attempt, 
    wait_exponential,
    AsyncRetrying,
    RetryCallState
)
from tenacity.stop import stop_base
from tenacity.wait import wait_base

logger = structlog.get_logger(__name__)

class PluginType(Enum):
    SIGNAL_ENHANCEMENT = auto()
    RISK_MANAGEMENT = auto()
    DATA_ENRICHMENT = auto()
    EXECUTION_HOOK = auto()

class PluginConfig(BaseModel):
    enabled: bool = True
    priority: int = 0
    timeout: float = 2.0
    max_retries: int = 1
    circuit_breaker_max_failures: int = 3
    circuit_breaker_reset_timeout: float = 30.0
    retry_wait_multiplier: float = 0.1
    retry_wait_max: float = 10.0

    @validator('priority')
    def validate_priority(cls, v):
        if v < 0 or v > 100:
            raise ValueError('Priority must be between 0 and 100')
        return v

class PluginMetadata(BaseModel):
    name: str
    version: str = "1.0.0"
    author: str = "Unknown"
    type: PluginType
    dependencies: List[str] = []
    config_schema: Optional[Dict[str, Any]] = None
    description: str = ""

    @validator('version')
    def validate_version(cls, v):
        if not all(x.isdigit() for x in v.split('.')):
            raise ValueError('Invalid semantic version format')
        return v

class PluginResult(BaseModel):
    success: bool
    data: Dict[str, Any]
    metrics: Dict[str, float]
    error: Optional[str] = None
    attempts: int = 1
    timestamp: float = time.time()

class CircuitBreaker:
    def __init__(self, max_failures=3, reset_timeout=30):
        self.failure_count = 0
        self.last_failure = 0.0
        self.max_failures = max_failures
        self.reset_timeout = reset_timeout
        self.total_requests = 0
        self.total_failures = 0

    def is_open(self):
        if time.time() - self.last_failure > self.reset_timeout:
            self.reset()
        return self.failure_count >= self.max_failures

    def record_failure(self):
        self.failure_count += 1
        self.total_failures += 1
        self.last_failure = time.time()

    def record_success(self):
        self.failure_count = max(0, self.failure_count - 1)

    def reset(self):
        self.failure_count = 0
        self.last_failure = 0.0

    @property
    def failure_rate(self):
        if self.total_requests == 0:
            return 0.0
        return self.total_failures / self.total_requests

class PluginState(TypedDict):
    func: Callable
    metadata: PluginMetadata
    config: PluginConfig
    enabled: bool
    circuit_breaker: CircuitBreaker
    execution_count: int
    success_count: int

class PluginManager:
    def __init__(self):
        self.plugins: Dict[str, PluginState] = {}
        self.dependency_graph: Dict[str, List[str]] = {}
        self._plugin_lock = asyncio.Lock()

    def register(self, plugin_func: Callable, metadata: PluginMetadata, config: PluginConfig = PluginConfig()):
        plugin_id = f"{metadata.name}@{metadata.version}"
        
        if plugin_id in self.plugins:
            raise ValueError(f"Plugin {plugin_id} already registered")

        # Validate function signature
        sig = inspect.signature(plugin_func)
        required_params = {"context", "data", "state", "config"}
        missing_params = required_params - set(sig.parameters)
        if missing_params:
            raise ValueError(f"Plugin {plugin_id} missing required parameters: {missing_params}")

        # Validate dependencies
        missing_deps = [dep for dep in metadata.dependencies if dep not in self.plugins]
        if missing_deps:
            logger.warning("Missing plugin dependencies", 
                         plugin=plugin_id,
                         missing_dependencies=missing_deps)

        # Create circuit breaker with config values
        circuit_breaker = CircuitBreaker(
            max_failures=config.circuit_breaker_max_failures,
            reset_timeout=config.circuit_breaker_reset_timeout
        )

        self.plugins[plugin_id] = {
            "func": plugin_func,
            "metadata": metadata,
            "config": config,
            "enabled": config.enabled,
            "circuit_breaker": circuit_breaker,
            "execution_count": 0,
            "success_count": 0
        }

        self.dependency_graph[plugin_id] = metadata.dependencies
        logger.info("Plugin registered", 
                  plugin=plugin_id,
                  type=metadata.type.name,
                  priority=config.priority)

    async def _execute_plugin(self, plugin_id: str, context: Dict, data: Any, state: Dict) -> PluginResult:
        plugin = self.plugins[plugin_id]
        cb = plugin["circuit_breaker"]
        config = plugin["config"]
        result = None
        attempts = 0
        last_error = None
        
        cb.total_requests += 1

        if cb.is_open():
            logger.warning("Circuit breaker open", plugin=plugin_id)
            return PluginResult(
                success=False,
                data={},
                metrics={},
                error="Circuit breaker open"
            )

        async def _call_plugin():
            nonlocal attempts
            attempts += 1
            start_time = time.perf_counter()
            exec_time = 0.0
            
            try:
                # Check for missing context dependencies
                missing_deps = [dep for dep in plugin["metadata"].dependencies if dep not in context]
                if missing_deps:
                    raise RuntimeError(f"Missing required dependencies: {missing_deps}")

                # Execute plugin with timeout
                async with asyncio.timeout(config.timeout):
                    result = plugin["func"](
                        context=context.copy(),
                        data=data,
                        state=state.copy(),
                        config=plugin["config"]
                    )
                    
                    if inspect.isawaitable(result):
                        result = await result

                exec_time = time.perf_counter() - start_time
                
                if not isinstance(result, dict):
                    raise ValueError("Plugin must return a dictionary")
                
                cb.record_success()
                return PluginResult(
                    success=True,
                    data=result,
                    metrics={"execution_time": exec_time},
                    attempts=attempts
                )
                
            except Exception as e:
                exec_time = time.perf_counter() - start_time
                cb.record_failure()
                logger.error("Plugin execution attempt failed",
                           plugin=plugin_id,
                           attempt=attempts,
                           error=str(e),
                           exec_time=exec_time)
                last_error = str(e)
                raise

        retryer = AsyncRetrying(
            stop=stop_after_attempt(config.max_retries + 1),
            wait=wait_exponential(
                multiplier=config.retry_wait_multiplier,
                max=config.retry_wait_max
            ),
            reraise=True
        )

        try:
            async for attempt in retryer:
                with attempt:
                    result = await _call_plugin()
        except Exception as e:
            return PluginResult(
                success=False,
                data={},
                metrics={"execution_time": exec_time},
                error=str(e),
                attempts=attempts
            )

        plugin["execution_count"] += 1
        if result.success:
            plugin["success_count"] += 1

        return result

    async def run_pipeline(self, 
                         pipeline_type: PluginType,
                         context: Dict[str, Any],
                         data: Any,
                         state: Dict[str, Any]) -> Dict[str, Any]:
        results = {}
        plugins_to_run = self._sort_plugins(pipeline_type)
        
        for plugin_id in plugins_to_run:
            plugin = self.plugins[plugin_id]
            
            if not plugin["enabled"]:
                logger.debug("Skipping disabled plugin", plugin=plugin_id)
                continue

            async with self._plugin_lock:  # Ensure atomic context updates
                result = await self._execute_plugin(plugin_id, context, data, state)
                results[plugin_id] = result.dict()
                
                if result.success:
                    context.update(result.data)
                    logger.info("Plugin executed successfully",
                              plugin=plugin_id,
                              exec_time=result.metrics.get("execution_time", 0))
                else:
                    logger.error("Plugin execution failed",
                               plugin=plugin_id,
                               error=result.error)

        return results

    def _sort_plugins(self, pipeline_type: PluginType) -> List[str]:
        # Implementation remains similar but uses networkx for more robust sorting
        # ... (maintain topological sorting logic)
        return sorted_plugins

    def get_plugin_stats(self) -> Dict[str, Dict]:
        return {
            p_id: {
                "enabled": p["enabled"],
                "execution_count": p["execution_count"],
                "success_rate": p["success_count"] / p["execution_count"] if p["execution_count"] else 0,
                "failure_rate": p["circuit_breaker"].failure_rate,
                "current_status": "open" if p["circuit_breaker"].is_open() else "closed"
            }
            for p_id, p in self.plugins.items()
        }

    # Additional utility methods
    def validate_pipeline(self, pipeline_type: PluginType) -> List[str]:
        missing_deps = []
        for plugin_id in self.plugins:
            if self.plugins[plugin_id]["metadata"].type != pipeline_type:
                continue
            for dep in self.plugins[plugin_id]["metadata"].dependencies:
                if dep not in self.plugins:
                    missing_deps.append(f"{plugin_id} -> {dep}")
        return missing_deps

    async def warmup_plugins(self):
        """Pre-initialize plugins with empty context"""
        logger.info("Starting plugin warmup")
        for plugin_id in self.plugins:
            if self.plugins[plugin_id]["config"].enabled:
                try:
                    await self._execute_plugin(plugin_id, {}, None, {})
                    logger.debug("Plugin warmup successful", plugin=plugin_id)
                except Exception as e:
                    logger.error("Plugin warmup failed", plugin=plugin_id, error=str(e))
        logger.info("Plugin warmup completed")

