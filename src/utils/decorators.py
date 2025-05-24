import asyncio
import functools
import logging
import random
import time
from typing import Callable, Type, Optional, Union, TypeVar, ParamSpec
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Type variables for decorator typing
T = TypeVar('T')
P = ParamSpec('P')

@dataclass
class CircuitBreakerState:
    failure_count: int = 0
    last_failure_time: Optional[float] = None
    state: str = "closed"  # closed, open, half-open
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

class EnhancedDecorators:
    @staticmethod
    def async_retry(
        max_retries: int = 3,
        initial_delay: float = 1.0,
        backoff_factor: float = 2.0,
        max_delay: float = 30.0,
        jitter: float = 0.1,
        retry_exceptions: tuple[Type[Exception], ...] = (Exception,),
        no_retry_exceptions: tuple[Type[Exception], ...] = ()
    ) -> Callable[[Callable[P, T]], Callable[P, T]]:
        """
        Enhanced async retry decorator with exponential backoff and jitter.
        
        Features:
        - Configurable exponential backoff with jitter
        - Exception whitelisting/blacklisting
        - Maximum delay cap
        - Detailed logging
        """
        def decorator(func: Callable[P, T]) -> Callable[P, T]:
            @functools.wraps(func)
            async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
                delay = initial_delay
                for attempt in range(max_retries + 1):
                    try:
                        return await func(*args, **kwargs)
                    except no_retry_exceptions as e:
                        logger.error(f"Non-retryable exception caught: {e}")
                        raise
                    except retry_exceptions as e:
                        if attempt == max_retries:
                            logger.error(f"Max retries ({max_retries}) exceeded for {func.__name__}")
                            raise

                        # Calculate next delay with jitter
                        jitter_amount = delay * jitter * random.uniform(-1, 1)
                        actual_delay = min(max_delay, delay + jitter_amount)
                        
                        logger.warning(
                            f"Retry {attempt + 1}/{max_retries} for {func.__name__} "
                            f"in {actual_delay:.2f}s (Error: {e})"
                        )
                        
                        await asyncio.sleep(actual_delay)
                        delay *= backoff_factor
                raise RuntimeError("Unreachable code reached")  # For type checker
            return wrapper
        return decorator

    @staticmethod
    def timeout(
        seconds: Union[int, float],
        timeout_exception: Type[Exception] = asyncio.TimeoutError,
        timeout_message: Optional[str] = None
    ) -> Callable[[Callable[P, T]], Callable[P, T]]:
        """
        Enhanced timeout decorator with better cancellation handling.
        
        Features:
        - Custom timeout exceptions
        - Clean task cancellation
        - Detailed timeout information
        """
        def decorator(func: Callable[P, T]) -> Callable[P, T]:
            @functools.wraps(func)
            async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
                message = timeout_message or f"{func.__name__} timed out after {seconds}s"
                
                try:
                    return await asyncio.wait_for(
                        func(*args, **kwargs),
                        timeout=seconds
                    )
                except asyncio.TimeoutError as e:
                    logger.error(message)
                    raise timeout_exception(message) from e
                except asyncio.CancelledError:
                    logger.warning(f"{func.__name__} was cancelled during execution")
                    raise
            return wrapper
        return decorator

    @staticmethod
    def circuit_breaker(
        failure_threshold: int = 5,
        reset_timeout: float = 30.0,
        excluded_exceptions: tuple[Type[Exception], ...] = (),
        catch_exceptions: tuple[Type[Exception], ...] = (Exception,)
    ) -> Callable[[Callable[P, T]], Callable[P, T]]:
        """
        Production-ready circuit breaker decorator with state management.
        
        Features:
        - Three states: closed, open, half-open
        - Failure threshold and automatic reset
        - Exclusion of specific exceptions from counting as failures
        - Thread-safe state management
        """
        def decorator(func: Callable[P, T]) -> Callable[P, T]:
            state = CircuitBreakerState()

            @functools.wraps(func)
            async def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
                async with state.lock:
                    if state.state == "open":
                        if time.monotonic() - state.last_failure_time > reset_timeout:
                            state.state = "half-open"
                            logger.warning("Circuit entering half-open state")
                        else:
                            raise CircuitOpenError(
                                f"Circuit breaker open for {func.__name__}"
                            )

                try:
                    result = await func(*args, **kwargs)
                    
                    if state.state == "half-open":
                        async with state.lock:
                            state.state = "closed"
                            state.failure_count = 0
                            logger.warning("Circuit reset to closed state")
                    
                    return result
                except excluded_exceptions as e:
                    raise
                except catch_exceptions as e:
                    async with state.lock:
                        state.failure_count += 1
                        state.last_failure_time = time.monotonic()
                        
                        if state.state == "half-open" or state.failure_count >= failure_threshold:
                            state.state = "open"
                            logger.error(
                                f"Circuit breaker opened for {func.__name__} "
                                f"({state.failure_count} failures)"
                            )
                        
                        raise
            return wrapper
        return decorator

class CircuitOpenError(Exception):
    """Custom exception for circuit breaker open state"""
    pass

# Usage examples
if __name__ == "__main__":
    @EnhancedDecorators.async_retry(max_retries=3, initial_delay=0.1)
    @EnhancedDecorators.timeout(seconds=2)
    @EnhancedDecorators.circuit_breaker(failure_threshold=3)
    async def example_service_call():
        # Your business logic here
        await asyncio.sleep(0.5)
        return "Success"

    async def main():
        try:
            result = await example_service_call()
            logger.info(f"Result: {result}")
        except Exception as e:
            logger.info(f"Error: {e}")

    asyncio.run(main())
