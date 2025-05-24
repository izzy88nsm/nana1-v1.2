# src/utils/circuit_breaker.py

import time
import logging
from enum import Enum, auto
from typing import Optional, Callable, Deque, Tuple, Dict, Any
from collections import deque
from contextlib import asynccontextmanager
import asyncio
import functools
import math

logger = logging.getLogger(__name__)

class CircuitState(Enum):
    CLOSED = auto()
    OPEN = auto()
    HALF_OPEN = auto()

class ThresholdStrategy(Enum):
    COUNT_BASED = auto()
    TIME_BASED = auto()
    HYBRID = auto()

class BackoffStrategy(Enum):
    LINEAR = auto()
    EXPONENTIAL = auto()
    FIBONACCI = auto()

class CircuitBreakerError(Exception):
    """Base exception for circuit breaker failures"""

class CircuitOpenError(CircuitBreakerError):
    """Raised when the circuit is open"""

class ServiceOverloadError(CircuitBreakerError):
    """Raised when service is overloaded"""

class CircuitBreaker:
    def __init__(
        self,
        failure_threshold: int = 3,
        recovery_threshold: int = 3,
        recovery_timeout: float = 60,
        strategy: ThresholdStrategy = ThresholdStrategy.HYBRID,
        backoff_strategy: BackoffStrategy = BackoffStrategy.EXPONENTIAL,
        max_backoff: float = 300,
        sliding_window_size: int = 100,
        error_threshold_ratio: float = 0.5,
        health_check: Optional[Callable] = None,
    ):
        self._state = CircuitState.CLOSED
        self.failure_threshold = failure_threshold
        self.recovery_threshold = recovery_threshold
        self.recovery_timeout = recovery_timeout
        self.strategy = strategy
        self.backoff_strategy = backoff_strategy
        self.max_backoff = max_backoff
        self.error_threshold_ratio = error_threshold_ratio
        self.health_check = health_check
        
        self._failure_count = 0
        self._success_count = 0
        self._consecutive_failures = 0
        self._last_failure_time = 0.0
        self._last_success_time = 0.0
        self._backoff_duration = recovery_timeout
        self._lock = asyncio.Lock()
        self._failure_window: Deque[Tuple[float, float]] = deque(maxlen=sliding_window_size)
        self._metrics: Dict[str, Any] = {
            "total_requests": 0,
            "total_failures": 0,
            "state_changes": [],
            "consecutive_failures": 0,
            "current_backoff": 0.0,
        }

    @property
    def state(self) -> CircuitState:
        return self._state

    @property
    def metrics(self) -> Dict[str, Any]:
        return self._metrics.copy()

    async def __call__(self, func: Callable):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            async with self.protection():
                return await func(*args, **kwargs)
        return wrapper

    @asynccontextmanager
    async def protection(self):
        if await self._is_open():
            raise CircuitOpenError("Circuit is open")
        
        try:
            yield
            await self.record_success()
        except Exception as e:
            await self.record_failure()
            raise

    async def _is_open(self) -> bool:
        async with self._lock:
            if self._state == CircuitState.OPEN:
                if time.time() - self._last_failure_time > self._backoff_duration:
                    await self._attempt_recovery()
                return True
            return False

    async def _attempt_recovery(self):
        if self.health_check:
            try:
                result = await self.health_check()
                if result:
                    await self._half_open()
                    return
            except Exception as e:
                logger.debug("Health check failed: %s", str(e))
        
        self._state = CircuitState.HALF_OPEN
        logger.info("Circuit entering half-open state")

    async def record_failure(self, error_weight: float = 1.0):
        async with self._lock:
            now = time.time()
            self._failure_count += 1
            self._consecutive_failures += 1
            self._success_count = 0
            self._last_failure_time = now
            self._failure_window.append((now, error_weight))
            self._metrics["total_failures"] += 1
            self._metrics["consecutive_failures"] = self._consecutive_failures

            if self._should_trip():
                await self._trip()

    async def record_success(self):
        async with self._lock:
            now = time.time()
            self._success_count += 1
            self._consecutive_failures = 0
            self._last_success_time = now
            self._metrics["total_requests"] += 1

            if self._state == CircuitState.HALF_OPEN:
                if self._success_count >= self.recovery_threshold:
                    await self._close()

    async def _should_trip(self) -> bool:
        if self.strategy == ThresholdStrategy.COUNT_BASED:
            return self._consecutive_failures >= self.failure_threshold
        
        if self.strategy == ThresholdStrategy.TIME_BASED:
            window_start = time.time() - self.recovery_timeout
            failures = sum(
                weight for timestamp, weight in self._failure_window
                if timestamp >= window_start
            )
            return failures >= self.failure_threshold
        
        if self.strategy == ThresholdStrategy.HYBRID:
            recent_failures = sum(
                weight for timestamp, weight in self._failure_window
                if time.time() - timestamp < self.recovery_timeout
            )
            return (
                recent_failures >= self.failure_threshold or
                self._consecutive_failures >= self.failure_threshold
            )
        return False

    async def _trip(self):
        self._state = CircuitState.OPEN
        self._update_backoff()
        self._metrics["state_changes"].append(("OPEN", time.time()))
        self._metrics["current_backoff"] = self._backoff_duration
        logger.critical(
            "Circuit tripped! Backoff: %.1fs, Consecutive failures: %d",
            self._backoff_duration, self._consecutive_failures
        )

    async def _half_open(self):
        self._state = CircuitState.HALF_OPEN
        self._metrics["state_changes"].append(("HALF_OPEN", time.time()))
        logger.info("Circuit in half-open state")

    async def _close(self):
        self._state = CircuitState.CLOSED
        self._reset_counters()
        self._metrics["state_changes"].append(("CLOSED", time.time()))
        self._metrics["current_backoff"] = 0.0
        logger.info("Circuit closed")

    def _update_backoff(self):
        base = self.recovery_timeout
        
        if self.backoff_strategy == BackoffStrategy.LINEAR:
            self._backoff_duration = base * (1 + self._consecutive_failures)
        elif self.backoff_strategy == BackoffStrategy.EXPONENTIAL:
            self._backoff_duration = base * (2 ** self._consecutive_failures)
        elif self.backoff_strategy == BackoffStrategy.FIBONACCI:
            fib = (math.sqrt(5) + 1) / 2
            self._backoff_duration = base * (fib ** self._consecutive_failures)
        
        self._backoff_duration = min(self._backoff_duration, self.max_backoff)

    def _reset_counters(self):
        self._failure_count = 0
        self._success_count = 0
        self._consecutive_failures = 0
        self._failure_window.clear()

    async def reset(self):
        async with self._lock:
            await self._close()

    def current_backoff(self) -> float:
        return self._backoff_duration

    async def check_health(self) -> bool:
        if self.health_check:
            try:
                return await self.health_check()
            except Exception as e:
                logger.warning("Health check failed: %s", str(e))
                return False
        return self._state == CircuitState.CLOSED

# Example usage:
async def main():
    # Create circuit breaker with exponential backoff
    cb = CircuitBreaker(
        failure_threshold=3,
        recovery_timeout=10,
        backoff_strategy=BackoffStrategy.EXPONENTIAL,
        max_backoff=300
    )

    @cb
    async def protected_call():
        # Your service call here
        pass

    try:
        await protected_call()
    except CircuitOpenError as e:
        logger.info("Circuit is open. Retry later.")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
