# src/core/state_schema.py

from pydantic import (
    BaseModel, Field, root_validator, validator, 
    condecimal, conint, constr, SecretStr
)
from typing import Dict, Any, Optional, List, Literal
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from uuid import UUID, uuid4
import hashlib
import json

# Constants
MAX_POSITIONS = 100
MAX_TRADE_HISTORY = 1000
MAX_ERROR_HISTORY = 50

class TradeRecord(BaseModel):
    trade_id: UUID = Field(default_factory=uuid4)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    symbol: constr(min_length=2, max_length=15, pattern=r"^[A-Z]+/[A-Z]+$")  # BTC/USD
    side: Literal["buy", "sell", "short", "cover"]
    order_type: Literal["market", "limit", "stop", "fok", "ioc"]
    status: Literal["filled", "partial", "canceled"]
    quantity: condecimal(gt=0, max_digits=18, decimal_places=8)
    price: condecimal(gt=0, max_digits=18, decimal_places=8)
    pnl: condecimal(max_digits=18, decimal_places=8)
    fees: Dict[str, condecimal(ge=0)] = Field(
        default_factory=dict,
        description="Fee breakdown by type {'exchange': 0.001, 'network': 0.0005}"
    )
    slippage: Optional[condecimal(ge=0)] = None
    strategy_version: constr(pattern=r"^\d+\.\d+\.\d+$")  # Semver
    risk_parameters: Dict[str, Any] = Field(
        default_factory=dict,
        description="Snapshot of risk parameters at trade time"
    )

    @validator('timestamp')
    def validate_future_trades(cls, v):
        if v > datetime.now(timezone.utc):
            raise ValueError("Future-dated trades not allowed")
        return v

    @model_validator(skip_on_failure=True)
    def validate_pnl_calculation(cls, values):
        if values.get('pnl') is not None:
            qty = values.get('quantity')
            price = values.get('price')
            expected_pnl = qty * price * (
                1 if values.get('side') in ['buy', 'cover'] else -1
            )
            if abs(values['pnl'] - expected_pnl) > Decimal('1e-8'):
                raise ValueError("PNL calculation mismatch")
        return values

class PositionState(BaseModel):
    position_id: UUID = Field(default_factory=uuid4)
    symbol: constr(min_length=2, max_length=15)
    entry_price: condecimal(gt=0, max_digits=18, decimal_places=8)
    quantity: condecimal(gt=0, max_digits=18, decimal_places=8)
    side: Literal["long", "short"]
    leverage: conint(gt=0, le=100) = 1
    margin: condecimal(gt=0, max_digits=18, decimal_places=8)
    open_time: datetime
    last_update: datetime
    unrealized_pnl: condecimal(max_digits=18, decimal_places=8)
    realized_pnl: condecimal(max_digits=18, decimal_places=8)
    stop_loss: Optional[condecimal(gt=0)] = None
    take_profit: Optional[condecimal(gt=0)] = None
    status: Literal["open", "closed", "liquidated"] = "open"
    risk_adjusted: bool = False

    @model_validator(skip_on_failure=True)
    def validate_position_dates(cls, values):
        if values['open_time'] > values['last_update']:
            raise ValueError("Position update time before open time")
        return values

    @validator('unrealized_pnl', 'realized_pnl')
    def validate_pnl_precision(cls, v):
        return v.quantize(Decimal('0.00000001'), rounding=ROUND_HALF_UP)

class RuntimeState(BaseModel):
    status: Literal["running", "paused", "error", "maintenance"] = "running"
    error_history: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Last 50 errors with timestamps and metadata"
    )
    performance_metrics: Dict[str, Any] = Field(
        default_factory=dict,
        description="Latency, throughput, resource usage"
    )
    last_successful_run: Optional[datetime] = None
    consecutive_failures: conint(ge=0) = 0
    circuit_breakers: Dict[str, bool] = Field(
        default_factory=dict,
        description="Active risk circuit breakers"
    )
    resource_usage: Dict[str, float] = Field(
        default_factory=dict,
        description="CPU, memory, network usage metrics"
    )

    @validator('error_history')
    def limit_error_history(cls, v):
        return v[-MAX_ERROR_HISTORY:]

class StrategyState(BaseModel):
    name: constr(min_length=2, max_length=50)
    version: constr(pattern=r"^\d+\.\d+\.\d+(-\w+)?(+\w+)?$")  # Full semver
    parameters: Dict[str, Any]
    parameter_hash: constr(min_length=64, max_length=64)
    performance_metrics: Dict[str, Any] = Field(
        default_factory=dict,
        description="Sharpe ratio, win rate, max drawdown"
    )
    state_hash: Optional[str] = Field(
        None,
        description="Hash of internal state for consistency checks"
    )
    risk_parameters: Dict[str, Any] = Field(
        default_factory=dict,
        description="Current risk management configuration"
    )

    @model_validator(skip_on_failure=True)
    def validate_parameter_hash(cls, values):
        params = json.dumps(values.get('parameters'), sort_keys=True)
        calculated_hash = hashlib.sha256(params.encode()).hexdigest()
        if values.get('parameter_hash') != calculated_hash:
            raise ValueError("Parameter hash mismatch")
        return values

class SecurityMetadata(BaseModel):
    encryption_key_version: constr(min_length=1, max_length=10)
    last_audit: datetime
    access_controls: List[str] = Field(
        default_factory=list,
        description="Roles with state access permissions"
    )
    tamper_evidence: Optional[str] = None

class Metadata(BaseModel):
    version: constr(pattern=r"^\d+\.\d+\.\d+$") = "1.0.0"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    checksum: constr(min_length=64, max_length=64)
    owner_id: constr(min_length=4, max_length=50)
    description: Optional[constr(max_length=500)] = None
    security: SecurityMetadata
    data_retention: Dict[str, Any] = Field(
        default_factory=dict,
        description="Retention policies for historical data"
    )

    @validator('checksum')
    def validate_checksum(cls, v, values):
        state_json = json.dumps(values, sort_keys=True)
        calculated = hashlib.sha256(state_json.encode()).hexdigest()
        if v != calculated:
            raise ValueError("State checksum validation failed")
        return v

class BotState(BaseModel):
    metadata: Metadata
    runtime: RuntimeState
    strategy: StrategyState
    positions: List[PositionState] = Field(
        default_factory=list,
        max_items=MAX_POSITIONS
    )
    trade_history: List[TradeRecord] = Field(
        default_factory=list,
        max_items=MAX_TRADE_HISTORY
    )
    custom_state: Dict[str, Any] = Field(
        default_factory=dict,
        description="Encrypted sensitive data storage"
    )
    api_keys: Optional[Dict[str, SecretStr]] = Field(
        None,
        description="Encrypted exchange API credentials"
    )

    @model_validator(skip_on_failure=True)
    def validate_state_consistency(cls, values):
        # Validate position/trade history alignment
        open_positions = [p for p in values.get('positions', []) if p.status == "open"]
        open_symbols = {p.symbol for p in open_positions}
        
        for trade in values.get('trade_history', []):
            if trade.symbol in open_symbols and trade.side not in ['buy', 'sell']:
                raise ValueError("Invalid trade side for open position")
        
        # Validate strategy state hash
        strategy_state = values.get('strategy')
        if strategy_state.state_hash:
            calculated = hashlib.sha256(
                json.dumps(strategy_state.dict()).encode()
            ).hexdigest()
            if strategy_state.state_hash != calculated:
                raise ValueError("Strategy state hash mismatch")
        
        return values

    @property
    def is_operational(self) -> bool:
        return self.runtime.status in ["running", "maintenance"]

    @property
    def total_exposure(self) -> Decimal:
        return sum(p.quantity * p.entry_price for p in self.positions)

class StateSchema(BaseModel):
    bot_state: BotState
    signature: Optional[constr(min_length=64, max_length=64)] = Field(
        None,
        description="Digital signature for state integrity"
    )

    @model_validator(skip_on_failure=True)
    def validate_signature(cls, values):
        if values.get('signature'):
            state_json = json.dumps(values['bot_state'].dict(), sort_keys=True)
            calculated = hashlib.sha256(state_json.encode()).hexdigest()
            if values['signature'] != calculated:
                raise ValueError("State signature validation failed")
        return values

    class Config:
        json_encoders = {
            Decimal: lambda v: str(v.normalize()),
            datetime: lambda v: v.isoformat(),
            SecretStr: lambda v: v.get_secret_value() if v else None
        }
        schema_extra = {
            "example": {
                "bot_state": {
                    "metadata": {
                        "version": "1.0.0",
                        "checksum": "e3b0c44298fc1c149afbf4c8996fb924...",
                        "security": {
                            "encryption_key_version": "v2",
                            "last_audit": "2023-07-20T12:00:00Z"
                        }
                    },
                    "runtime": {
                        "status": "running",
                        "performance_metrics": {
                            "latency_ms": 45.2,
                            "throughput_tps": 120
                        }
                    },
                    "strategy": {
                        "name": "quant_momentum_v3",
                        "version": "2.1.0",
                        "parameters": {"rsi_period": 14},
                        "parameter_hash": "a1b2c3..."
                    }
                },
                "signature": "e3b0c44298fc1c149afbf4c8996fb924..."
            }
        }
