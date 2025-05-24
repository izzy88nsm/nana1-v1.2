# src/market_data/data_feed.py

import asyncio
from typing import Any, Dict, List, Optional, AsyncGenerator
from dataclasses import dataclass
import time
import random
from functools import lru_cache
from contextlib import asynccontextmanager
import logging

# ─── Constants ────────────────────────────────────────────────────────────────
DEFAULT_TIMEOUT = 10.0
MAX_RETRIES = 3
BASE_RECONNECT_DELAY = 1.0
HEARTBEAT_INTERVAL = 30.0
CACHE_TTL = 60  # Seconds

# ─── Exceptions ───────────────────────────────────────────────────────────────
class DataFeedError(Exception):
    """Base exception for all data feed errors"""

class ConnectionError(DataFeedError):
    """Raised when connection to feed fails"""

class TimeoutError(DataFeedError):
    """Raised when operation times out"""

class RateLimitError(DataFeedError):
    """Raised when rate limits are exceeded"""

# ─── Data Structures ──────────────────────────────────────────────────────────
@dataclass(frozen=True)
class TickerData:
    symbol: str
    price: float
    volume: float
    timestamp: float
    bid: float
    ask: float

@dataclass
class ConnectionParams:
    endpoint: str
    api_key: str
    timeout: float = DEFAULT_TIMEOUT
    max_retries: int = MAX_RETRIES

# ─── Core Implementation ──────────────────────────────────────────────────────
class MarketDataFeed:
    def __init__(
        self,
        config: ConnectionParams,
        http_client: Optional[Any] = None,
        logger: Optional[logging.Logger] = None
    ):
        self.config = config
        self.http = http_client
        self.logger = logger or logging.getLogger(__name__)
        self._connection_lock = asyncio.Lock()
        self._connected = False
        self._circuit_open = False
        self._last_failure_time = 0.0
        self._request_counter = 0
        self._last_heartbeat = 0.0
        self._subscriptions = set()
        self._data_queue = asyncio.Queue()

    @property
    def is_healthy(self) -> bool:
        return (
            self._connected and 
            not self._circuit_open and
            time.monotonic() - self._last_heartbeat < HEARTBEAT_INTERVAL * 2
        )

    @asynccontextmanager
    async def connection(self):
        """Async context manager for connection lifecycle"""
        await self.connect()
        try:
            yield self
        finally:
            await self.disconnect()

    async def connect(self) -> None:
        """Connect with retries and circuit breaker pattern"""
        if self._connected or self._circuit_open:
            return

        async with self._connection_lock:
            for attempt in range(self.config.max_retries):
                try:
                    await self._establish_connection()
                    self._connected = True
                    self._start_background_tasks()
                    return
                except ConnectionError as e:
                    if attempt == self.config.max_retries - 1:
                        self._trip_circuit()
                        raise
                    delay = BASE_RECONNECT_DELAY * (2 ** attempt)
                    await asyncio.sleep(delay)

    async def _establish_connection(self):
        """Simulate connection with potential failure"""
        if random.random() < 0.2:  # 20% failure rate for demo
            raise ConnectionError("Simulated connection failure")
        
        await asyncio.sleep(0.5)  # Simulate connection time
        self.logger.info("Connected to market data feed")

    def _trip_circuit(self):
        """Handle circuit breaker tripping"""
        self._circuit_open = True
        self._last_failure_time = time.monotonic()
        self.logger.error("Circuit breaker tripped")

    async def disconnect(self) -> None:
        """Graceful disconnection"""
        async with self._connection_lock:
            self._connected = False
            await self._cleanup_resources()
            self.logger.info("Disconnected from market data feed")

    async def get_price(
        self, 
        symbol: str,
        timeout: float = DEFAULT_TIMEOUT
    ) -> TickerData:
        """Get price data with caching and rate limiting"""
        try:
            return await self._get_price_with_retry(symbol, timeout)
        except Exception as e:
            self.logger.error(f"Price fetch failed for {symbol}", exc_info=True)
            raise

    @lru_cache(maxsize=1024)
    async def _get_price_with_retry(
        self,
        symbol: str,
        timeout: float
    ) -> TickerData:
        """Retry wrapper with cache and rate limiting"""
        async with self._rate_limiter():
            try:
                data = await asyncio.wait_for(
                    self._fetch_price(symbol),
                    timeout=timeout
                )
                self._update_health()
                return data
            except asyncio.TimeoutError:
                self._record_failure()
                raise TimeoutError(f"Timeout fetching {symbol}")

    async def _fetch_price(self, symbol: str) -> TickerData:
        """Simulate real price fetching with error injection"""
        if not self._connected:
            raise ConnectionError("Not connected to feed")
        
        # Simulate occasional errors
        if random.random() < 0.1:
            raise DataFeedError("Simulated data error")
        
        await asyncio.sleep(0.1)  # Simulate network latency
        return TickerData(
            symbol=symbol,
            price=random.uniform(90.0, 110.0),
            volume=random.randint(1000, 10000),
            timestamp=time.time(),
            bid=random.uniform(89.5, 109.5),
            ask=random.uniform(90.5, 110.5)
        )

    async def stream_data(self) -> AsyncGenerator[TickerData, None]:
        """Real-time data streaming"""
        while self._connected:
            try:
                yield await self._data_queue.get()
            except asyncio.CancelledError:
                break

    async def subscribe(self, symbols: List[str]) -> None:
        """Subscribe to multiple symbols"""
        self._subscriptions.update(symbols)
        self.logger.info(f"Subscribed to {len(symbols)} symbols")

    def _start_background_tasks(self):
        """Start maintenance tasks"""
        loop = asyncio.get_event_loop()
        self._tasks = [
            loop.create_task(self._heartbeat()),
            loop.create_task(self._process_data_stream())
        ]

    async def _heartbeat(self):
        """Maintain connection health"""
        while self._connected:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            try:
                await self._send_heartbeat()
                self._last_heartbeat = time.monotonic()
            except Exception as e:
                self.logger.warning("Heartbeat failed", exc_info=True)
                await self.disconnect()
                await self.connect()

    async def _process_data_stream(self):
        """Process real-time data updates"""
        while self._connected:
            # Simulate real-time data updates
            await asyncio.sleep(1)
            for symbol in self._subscriptions:
                try:
                    data = await self.get_price(symbol)
                    await self._data_queue.put(data)
                except DataFeedError as e:
                    self.logger.warning(f"Stream update failed for {symbol}")

    @asynccontextmanager
    async def _rate_limiter(self):
        """Rate limiting context manager"""
        start_time = time.monotonic()
        self._request_counter += 1
        try:
            yield
        finally:
            processing_time = time.monotonic() - start_time
            if processing_time < 0.1:
                await asyncio.sleep(0.1 - processing_time)
            self._request_counter -= 1

    def _update_health(self):
        """Update circuit breaker state"""
        if self._circuit_open and time.monotonic() > self._last_failure_time + 30:
            self._circuit_open = False
            self.logger.info("Circuit breaker reset")

    def _record_failure(self):
        """Track failures for circuit breaker"""
        self._last_failure_time = time.monotonic()
        if not self._circuit_open:
            self.logger.warning("Recording connection failure")

# ─── Example Usage ────────────────────────────────────────────────────────────
async def main():
    config = ConnectionParams(
        endpoint="wss://market-data.example.com",
        api_key="your_api_key"
    )
    
    async with MarketDataFeed(config) as feed:
        # Single price request
        try:
            data = await feed.get_price("AAPL")
            logger.info(f"Apple stock price: {data.price}")
        except DataFeedError as e:
            logger.info(f"Error: {e}")

        # Streaming data
        async for update in feed.stream_data():
            logger.info(f"Real-time update: {update}")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
