# src/datacenter/market_observer.py

import asyncio
import random
from datetime import datetime
from typing import Dict, Any, Optional
from decimal import Decimal

import structlog
from pydantic import BaseModel, ValidationError, Field, PositiveFloat
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from .base_datacenter import BaseDataCenter, DataConnectionError, DataValidationError
from src.config.config_schema import AppConfig, MarketDataSource

logger = structlog.get_logger(__name__)

class MarketData(BaseModel):
    symbol: str
    bid: Decimal
    ask: Decimal
    volume: Decimal
    timestamp: datetime
    exchange: str = Field(..., min_length=3)
    interval: str = Field(pattern=r"^\d+(m|h|d)$")

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid

class MarketObserver(BaseDataCenter[MarketData]):
    OBSERVER_TYPE = "market"
    VERSION = "2.3.0"

    def __init__(self, config: AppConfig):
        super().__init__(config)
        self.symbols: list[str] = []
        self._last_update: Optional[datetime] = None
        self._data_sources: Dict[str, MarketDataSource] = {}
        self._simulation_mode: bool = False
        self._rate_limit: float = 1.0

    async def _setup_services(self) -> None:
        """Initialize market data connections"""
        await super()._setup_services()
        
        market_config = next(
            (bot for bot in self.config.bots if bot.name == "market_observer"),
            None
        )
        
        if not market_config:
            raise DataValidationError("Missing market observer configuration")

        self.symbols = market_config.markets
        self._simulation_mode = market_config.mode == "paper"
        self._data_sources = {
            source.type: source for source in market_config.market_data.sources
        }

        # Parse rate limit (e.g., "10/1s" -> 0.1 seconds between requests)
        if market_config.execution.rate_limit:
            rate, _, interval = market_config.execution.rate_limit.partition('/')
            self._rate_limit = float(interval[:-1]) / float(rate)

        logger.info(
            "MarketObserver initialized",
            symbols=self.symbols,
            simulation_mode=self._simulation_mode,
            rate_limit=self._rate_limit
        )

    @retry(
        retry=retry_if_exception_type(DataConnectionError),
        stop=stop_after_attempt(5),
        wait=wait_exponential_jitter(max=10),
    )
    async def _fetch_market_data(self, symbol: str) -> MarketData:
        """Fetch market data from configured source"""
        async with self._resource_manager(symbol):
            if self._simulation_mode:
                return self._generate_synthetic_data(symbol)

            source = self._data_sources.get("websocket") or self._data_sources.get("rest")
            if not source:
                raise DataConnectionError("No valid data source configured")

            try:
                # Implementation would vary based on actual data source
                # Example:
                # async with aiohttp.ClientSession() as session:
                #     async with session.get(source.endpoints[0]) as response:
                #         data = await response.json()
                #         return MarketData(**data)
                
                # Temporary mock implementation
                return self._generate_synthetic_data(symbol)
            except ValidationError as e:
                logger.error("Data validation failed", symbol=symbol, error=str(e))
                raise DataValidationError("Invalid market data format") from e
            except Exception as e:
                logger.warning("Data fetch failed", symbol=symbol, error=str(e))
                raise DataConnectionError("Market data unavailable") from e

    async def _run_loop(self) -> None:
        """Main market observation loop"""
        logger.info("MarketObserver operational", symbols=self.symbols)
        
        while self.is_running:
            try:
                start_time = datetime.utcnow()
                
                # Parallel data fetching for all symbols
                tasks = [
                    self._create_task(self._process_symbol(symbol))
                    for symbol in self.symbols
                ]
                
                await asyncio.gather(*tasks)
                self._last_update = datetime.utcnow()
                
                # Rate limiting
                processing_time = (datetime.utcnow() - start_time).total_seconds()
                delay = max(0, self._rate_limit - processing_time)
                await asyncio.sleep(delay)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self.metrics.market_errors.inc()
                logger.error("Market loop error", error=str(e))
                await asyncio.sleep(5)

    async def _process_symbol(self, symbol: str) -> None:
        """Process data for individual symbol"""
        try:
            data = await self._fetch_market_data(symbol)
            validated_data = self._validate_output(data)
            
            self.metrics.price_updates.labels(symbol=symbol).inc()
            self.metrics.spread_histogram.labels(symbol=symbol).observe(float(validated_data.spread))
            
            logger.debug(
                "Market update",
                symbol=symbol,
                bid=float(validated_data.bid),
                ask=float(validated_data.ask),
                spread=float(validated_data.spread)
            )
            
            await self._notify_subscribers(validated_data)

        except DataValidationError as e:
            self.metrics.validation_errors.inc()
            logger.warning("Invalid data received", symbol=symbol, error=str(e))

    async def _notify_subscribers(self, data: MarketData) -> None:
        """Distribute market data to registered consumers"""
        # Implementation would use pub/sub system or websockets
        pass

    def _generate_synthetic_data(self, symbol: str) -> MarketData:
        """Generate simulated market data"""
        base_price = random.uniform(1000, 3000)
        spread = random.uniform(0.1, 2.0)
        
        return MarketData(
            symbol=symbol,
            bid=Decimal(f"{base_price:.4f}"),
            ask=Decimal(f"{base_price + spread:.4f}"),
            volume=Decimal(random.uniform(10, 1000)),
            timestamp=datetime.utcnow(),
            exchange=self.config.bots[0].exchange,
            interval="1m"
        )

    def _validate_output(self, result: MarketData) -> MarketData:
        """Validate market data integrity"""
        if result.ask <= result.bid:
            raise DataValidationError("Ask price must be greater than bid price")
        
        if result.volume < 0:
            raise DataValidationError("Volume cannot be negative")
            
        return result

    async def health_check(self) -> Dict[str, Any]:
        """Extended health check with market-specific metrics"""
        base_health = await super().health_check()
        return {
            **base_health,
            "market_status": {
                "symbols_monitored": len(self.symbols),
                "last_update": self._last_update.isoformat() if self._last_update else None,
                "update_frequency": self._rate_limit,
                "data_sources": list(self._data_sources.keys())
            }
        }

    async def _acquire_resource(self, resource_id: str) -> Any:
        """Acquire market data connection resource"""
        # Implementation for real resources would go here
        return {"connection_id": resource_id, "status": "active"}

    async def _release_resource(self, resource_id: str) -> None:
        """Release market data connection resource"""
        # Implementation for real resources would go here
        pass


    def register_subscriber(self, callback: Callable[[MarketData], Awaitable[None]]) -> None:
        if not hasattr(self, "subscribers"):
            self.subscribers = []
        self.subscribers.append(callback)

    async def _notify_subscribers(self, data: MarketData) -> None:
        for subscriber in getattr(self, "subscribers", []):
            try:
                await subscriber(data)
            except Exception as e:
                logger.warning("Subscriber callback failed", error=str(e))
    