import asyncio
import logging
import time
from enum import Enum
from typing import Callable, Dict, Optional, Awaitable, List, Tuple
from pydantic import BaseModel, Field
from collections import defaultdict

logger = logging.getLogger(__name__)

# ---------- Health States ---------- #
class HealthLevel(str, Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    CRITICAL = "CRITICAL"

# ---------- Health Reports ---------- #
class ComponentStatus(BaseModel):
    status: HealthLevel
    error: Optional[str] = None
    timestamp: float = Field(default_factory=time.time)

class HealthStatus(BaseModel):
    status: HealthLevel
    components: Dict[str, ComponentStatus]
    timestamp: float = Field(default_factory=time.time)
    metrics: Dict[str, dict] = Field(default_factory=dict)

# ---------- Dependency Configuration ---------- #
class DependencyConfig(BaseModel):
    check_fn: Callable[[], Awaitable[bool]]
    timeout: float = 5.0
    retries: int = 0
    retry_delay: float = 1.0
    is_critical: bool = True

# ---------- Registry Manager ---------- #
class DependencyManager:
    def __init__(self):
        self._registry: Dict[str, DependencyConfig] = {}
        self.metrics: Dict[str, dict] = defaultdict(lambda: {
            'successes': 0,
            'failures': 0,
            'last_checked': None
        })
        self.last_status: Optional[HealthStatus] = None

    def register(self, 
               name: str, 
               check_fn: Callable[[], Awaitable[bool]],
               timeout: float = 5.0,
               retries: int = 0,
               retry_delay: float = 1.0,
               is_critical: bool = True) -> None:
        """Register a dependency with configurable health checks"""
        self._registry[name] = DependencyConfig(
            check_fn=check_fn,
            timeout=timeout,
            retries=retries,
            retry_delay=retry_delay,
            is_critical=is_critical
        )

    async def check(self) -> HealthStatus:
        """Execute all health checks and compute system status"""
        start_time = time.time()
        
        # Execute all checks concurrently
        tasks = [self._safe_check(name, config) 
                for name, config in self._registry.items()]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Process results
        components = {}
        critical_components = []
        degraded_components = []
        
        for result in results:
            if isinstance(result, Exception):
                logger.error(f"Unexpected error in health check: {result}")
                continue
                
            name, (status, error) = result
            components[name] = ComponentStatus(
                status=status,
                error=error,
                timestamp=time.time()
            )
            
            # Update metrics
            self.metrics[name]['last_checked'] = time.time()
            if status == HealthLevel.HEALTHY:
                self.metrics[name]['successes'] += 1
            else:
                self.metrics[name]['failures'] += 1

            # Track statuses
            if status == HealthLevel.CRITICAL:
                critical_components.append(name)
            elif status == HealthLevel.DEGRADED:
                degraded_components.append(name)

        # Determine overall status
        if critical_components:
            overall_status = HealthLevel.CRITICAL
        elif degraded_components:
            overall_status = HealthLevel.DEGRADED
        else:
            overall_status = HealthLevel.HEALTHY

        # Create health status
        self.last_status = HealthStatus(
            status=overall_status,
            components=components,
            metrics=dict(self.metrics),
            timestamp=time.time()
        )

        logger.info(f"Health check completed in {time.time() - start_time:.2f}s")
        return self.last_status

    async def _safe_check(self, name: str, config: DependencyConfig) -> Tuple[str, Tuple[HealthLevel, Optional[str]]]:
        """Execute a single health check with retries and error handling"""
        last_error = None
        result = False
        
        for attempt in range(config.retries + 1):
            try:
                # Execute check with timeout
                result = await asyncio.wait_for(
                    config.check_fn(),
                    timeout=config.timeout
                )
                
                if result:
                    return (name, (HealthLevel.HEALTHY, None))
                
                # Non-success result doesn't trigger retries
                last_error = "Check returned False"
                break

            except asyncio.TimeoutError as e:
                last_error = f"Timeout after {config.timeout}s"
                logger.warning(f"[{name}] Timeout (attempt {attempt + 1})")
            except Exception as e:
                last_error = str(e)
                logger.error(f"[{name}] Error (attempt {attempt + 1}): {e}")

            # Exponential backoff before retry
            if attempt < config.retries:
                delay = config.retry_delay * (2 ** attempt)
                await asyncio.sleep(delay)

        # Determine final status
        if config.is_critical:
            status = HealthLevel.CRITICAL
        else:
            status = HealthLevel.DEGRADED

        return (name, (status, last_error))

    def get_metrics(self) -> Dict[str, dict]:
        """Get current health check metrics"""
        return dict(self.metrics)

    def status(self) -> Optional[HealthStatus]:
        """Get last computed health status"""
        return self.last_status

    async def close(self):
        """Clean up any resources used by health checks"""
        # Implement custom cleanup logic for check functions if needed
        pass
