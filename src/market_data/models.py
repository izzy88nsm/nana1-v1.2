from pydantic import (
    BaseModel, 
    Field, 
    condecimal, 
    validator, 
    root_validator,
    PositiveInt,
    model_validator
)
from datetime import datetime, timezone
from typing import Optional, Literal, Dict, List, Tuple
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
import statistics

class PriceSource(str, Enum):
    REAL_TIME = "real_time"
    HISTORICAL = "historical"
    SIMULATED = "simulated"
    CONSENSUS = "consensus"

class MarketDepth(BaseModel):
    price: condecimal(gt=0, max_digits=12, decimal_places=8)
    quantity: condecimal(ge=0, max_digits=16, decimal_places=8)

class MarketData(BaseModel):
    symbol: str = Field(..., min_length=2, max_length=30, example="BTC/USDT", description="Trading pair")
    exchange: str = Field(..., min_length=2, max_length=30, example="binance", description="Exchange ID")
    asset_class: Literal["spot", "future", "option"] = "spot"

    interval: Optional[Literal['1m', '3m', '5m', '15m', '30m', '1h', '2h', '4h', '6h', '12h', '1d', '3d', '1w']] = Field(None, description="Standardized timeframe")
    custom_interval: Optional[str] = Field(None, pattern=r"^\d+[smhdw]$", description="Custom interval (e.g., '15s', '4h')")
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), description="Exchange timestamp in UTC")
    received_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), description="Local receipt timestamp in UTC")

    bid: condecimal(gt=0, max_digits=12, decimal_places=8)
    ask: condecimal(gt=0, max_digits=12, decimal_places=8)
    last_price: condecimal(gt=0, max_digits=12, decimal_places=8)
    open: condecimal(gt=0, max_digits=12, decimal_places=8)
    high: condecimal(gt=0, max_digits=12, decimal_places=8)
    low: condecimal(gt=0, max_digits=12, decimal_places=8)
    close: condecimal(gt=0, max_digits=12, decimal_places=8)

    volume_base: condecimal(ge=0, max_digits=16, decimal_places=8) = Field(..., description="Volume in base currency")
    volume_quote: condecimal(ge=0, max_digits=16, decimal_places=8) = Field(..., description="Volume in quote currency")
    trade_count: PositiveInt = Field(..., description="Number of trades in interval")

    bids: List[MarketDepth] = Field(default_factory=list, description="Top 5 bid levels (price/quantity)")
    asks: List[MarketDepth] = Field(default_factory=list, description="Top 5 ask levels (price/quantity)")

    funding_rate: Optional[condecimal(ge=-1, le=1)] = Field(None, description="Perpetual funding rate")
    mark_price: Optional[condecimal(gt=0)] = Field(None, description="Derivatives mark price")
    index_price: Optional[condecimal(gt=0)] = Field(None, description="Underlying index price")

    source: PriceSource = PriceSource.REAL_TIME
    latency_ms: condecimal(ge=0, max_digits=8, decimal_places=3) = Field(..., description="Exchange to system latency")
    data_quality: condecimal(ge=0, le=1) = Field(1.0, description="Data quality score 0-1")

    @validator("high")
    def validate_high(cls, v, values):
        if "low" in values and v < values["low"]:
            raise ValueError("High price must be >= low price")
        return v

    @validator("low")
    def validate_low(cls, v, values):
        if "high" in values and v > values["high"]:
            raise ValueError("Low price must be <= high price")
        return v

    @model_validator(mode="after")
    def validate_ohlc(cls, values):
        o, h, l, c = values.get("open"), values.get("high"), values.get("low"), values.get("close")
        if None in (o, h, l, c):
            return values
        if h < max(o, c) or l > min(o, c):
            raise ValueError("Invalid OHLC relationship")
        return values

    @validator("timestamp", "received_at")
    def validate_timestamps(cls, v):
        if v > datetime.now(timezone.utc):
            raise ValueError("Future timestamps not allowed")
        return v

    @validator("bids", "asks")
    def validate_depth_levels(cls, v):
        if len(v) > 1:
            prices = [float(level.price) for level in v]
            if v is cls.bids and not all(x >= y for x, y in zip(prices, prices[1:])):
                raise ValueError("Bids must be in descending order")
            if v is cls.asks and not all(x <= y for x, y in zip(prices, prices[1:])):
                raise ValueError("Asks must be in ascending order")
        return v

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid

    @property
    def spread_pct(self) -> Decimal:
        return (self.spread / self.midpoint).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)

    @property
    def midpoint(self) -> Decimal:
        return ((self.bid + self.ask) / 2).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)

    @property
    def vwap(self) -> Decimal:
        if self.volume_quote == 0 or self.volume_base == 0:
            return self.close
        return (self.volume_quote / self.volume_base).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_UP)

    @property
    def price_change(self) -> Decimal:
        return ((self.close - self.open) / self.open).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)

    @property
    def is_valid(self) -> bool:
        return all([
            self.bid < self.ask,
            self.low <= self.open <= self.high,
            self.low <= self.close <= self.high,
            self.volume_base >= 0,
            self.volume_quote >= 0,
            self.timestamp <= self.received_at
        ])

    class Config:
        json_encoders = {
            Decimal: lambda v: str(v.normalize()),
            datetime: lambda v: v.isoformat()
        }
        schema_extra = {
            "example": {
                "symbol": "BTC/USDT",
                "exchange": "binance",
                "interval": "1h",
                "bid": Decimal("50123.45"),
                "ask": Decimal("50125.00"),
                "last_price": Decimal("50124.00"),
                "open": Decimal("50000.00"),
                "high": Decimal("50200.00"),
                "low": Decimal("49800.00"),
                "close": Decimal("50124.00"),
                "volume_base": Decimal("12.345678"),
                "volume_quote": Decimal("620000.00"),
                "trade_count": 1500,
                "bids": [{"price": 50123.45, "quantity": 0.5}],
                "asks": [{"price": 50125.00, "quantity": 0.7}],
                "latency_ms": Decimal("45.234"),
                "data_quality": Decimal("0.99")
            }
        }