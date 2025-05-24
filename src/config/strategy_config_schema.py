# src/config/strategy_config_schema.py

from typing import Literal, Optional, List, Annotated
from datetime import timedelta
from enum import Enum
import re

from pydantic import (
    BaseModel,
    Field,
    PositiveFloat,
    PositiveInt,
    field_validator,
    model_validator
)

class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"

class TimeInForce(str, Enum):
    GTC = "GTC"
    IOC = "IOC"
    FOK = "FOK"
    DAY = "DAY"

class RiskParameters(BaseModel):
    max_position_size: Annotated[float, Field(ge=0.01, le=1.0)] = 0.1
    stop_loss_pct: Annotated[float, Field(ge=0.001, le=0.5)] = 0.05
    take_profit_pct: Annotated[float, Field(ge=0.001, le=2.0)] = 0.1
    max_leverage: Annotated[int, Field(ge=1, le=100)] = 1
    risk_per_trade: Annotated[float, Field(ge=0.001, le=0.5)] = 0.02
    cool_off_period: Annotated[Optional[timedelta], Field()] = None

    @field_validator("take_profit_pct")
    @classmethod
    def validate_profit_loss_ratio(cls, v, info):
        if info.data["stop_loss_pct"] >= v:
            raise ValueError("Take profit must exceed stop loss percentage")
        return v

class StrategyConfig(BaseModel):
    name: Annotated[str, Field(min_length=3, max_length=50, pattern=r"^[a-zA-Z0-9_\-]+$")]
    version: Annotated[str, Field(pattern=r"^\d+\.\d+\.\d+(?:-[a-zA-Z0-9\.]+)?(?:\+[a-zA-Z0-9\.]+)?$")] = "1.0.0"
    mode: Literal["live", "paper", "backtest"] = "paper"
    history_window: Annotated[int, Field(ge=50, le=10000)] = 200
    rsi_period: Annotated[int, Field(ge=5, le=100)] = 14
    rsi_overbought: Annotated[float, Field(ge=50.0, le=90.0)] = 70.0
    rsi_oversold: Annotated[float, Field(ge=10.0, le=50.0)] = 30.0
    sma_fast_period: Annotated[int, Field(ge=3, le=100)] = 5
    sma_slow_period: Annotated[int, Field(ge=10, le=500)] = 21
    risk: RiskParameters = Field(default_factory=RiskParameters)
    order_type: OrderType = OrderType.MARKET
    time_in_force: TimeInForce = TimeInForce.GTC
    slippage_tolerance: Annotated[float, Field(ge=0.0, le=0.1)] = 0.005
    enable_dynamic_params: bool = False
    rebalance_interval: Annotated[Optional[timedelta], Field(ge=timedelta(minutes=1))] = None
    plugin_confidence_threshold: Annotated[float, Field(ge=0.0, le=1.0)] = 0.7
    max_concurrent_trades: Annotated[int, Field(ge=1, le=100)] = 5
    tags: Annotated[List[str], Field(max_items=10)] = Field(default_factory=list)
    description: Annotated[Optional[str], Field(min_length=10, max_length=1000)] = None

    @field_validator("sma_slow_period")
    @classmethod
    def validate_sma_periods(cls, v, info):
        if v <= info.data["sma_fast_period"]:
            raise ValueError("Slow SMA period must exceed fast SMA period")
        if v / info.data["sma_fast_period"] < 2:
            import structlog
            logger = structlog.get_logger(__name__)
            logger.warning("SMA period ratio < 2:1 may generate excessive signals")
        return v

    @field_validator("rsi_oversold")
    @classmethod
    def validate_rsi_thresholds(cls, v, info):
        if v >= info.data["rsi_overbought"]:
            raise ValueError("Oversold threshold must be below overbought")
        spread = info.data["rsi_overbought"] - v
        if spread < 20:
            import structlog
            logger = structlog.get_logger(__name__)
            logger.warning("RSI threshold spread < 20 may increase false signals")
        return v

    @field_validator("tags")
    @classmethod
    def validate_tags(cls, v):
        if len(v) != len(set(v)):
            raise ValueError("Duplicate tags detected")
        for tag in v:
            if not re.match(r"^[a-z0-9\-_]{2,25}$", tag):
                raise ValueError("Tags must be lowercase alphanumeric with dashes/underscores")
        return v

    @model_validator(mode="after")
    def validate_strategy_parameters(self):
        if self.mode == "live" and self.enable_dynamic_params:
            raise ValueError("Dynamic parameters cannot be enabled in live mode")
        return self

    model_config = {
        "extra": "forbid",
        "validate_assignment": True,
        "json_encoders": {
            timedelta: lambda v: f"{v.total_seconds()}s",
            Enum: lambda v: v.value
        },
        "schema_extra": {
            "examples": [
                {
                    "name": "quant_momentum_v3",
                    "version": "2.2.1",
                    "mode": "paper",
                    "history_window": 500,
                    "rsi_period": 21,
                    "rsi_overbought": 65.0,
                    "rsi_oversold": 35.0,
                    "sma_fast_period": 10,
                    "sma_slow_period": 50,
                    "risk": {
                        "max_position_size": 0.2,
                        "stop_loss_pct": 0.04,
                        "take_profit_pct": 0.12,
                        "max_leverage": 5,
                        "risk_per_trade": 0.03,
                        "cool_off_period": "5m"
                    },
                    "order_type": "stop_limit",
                    "time_in_force": "DAY",
                    "slippage_tolerance": 0.008,
                    "plugin_confidence_threshold": 0.75,
                    "max_concurrent_trades": 3,
                    "tags": ["high-frequency", "crypto", "ai"],
                    "description": "AI-optimized momentum strategy with dynamic stop limits"
                }
            ]
        }
    }

