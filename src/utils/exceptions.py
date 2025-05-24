from dataclasses import dataclass, field
from typing import Optional, Dict, Any, ClassVar, Type, List
import logging
import json
from datetime import datetime
import uuid

logger = logging.getLogger(__name__)

class ErrorMeta(type):
    """Metaclass to enforce unique error codes across exception classes."""
    _registry: Dict[int, str] = {}

    def __new__(cls, name, bases, namespace):
        error_code = namespace.get('error_code')
        if error_code is not None:
            if error_code in cls._registry:
                existing = cls._registry[error_code]
                raise ValueError(f"Duplicate error code {error_code} in {name}. Already used by {existing}")
            cls._registry[error_code] = name
        return super().__new__(cls, name, bases, namespace)

@dataclass
class BotFrameworkError(Exception, metaclass=ErrorMeta):
    error_code: ClassVar[int] = 0
    error_category: ClassVar[str] = "UNKNOWN"
    default_message: ClassVar[str] = "An unexpected error occurred"
    suggested_action: ClassVar[str] = "Review error details and system status"
    documentation_link: ClassVar[str] = "https://docs.botframework.com/errors"
    is_retryable: ClassVar[bool] = False
    http_status_code: ClassVar[Optional[int]] = None

    error_id: str = field(init=False, default_factory=lambda: str(uuid.uuid4()))
    message: str = field(default="")
    context: Dict[str, Any] = field(default_factory=dict)
    root_cause: Optional[Exception] = None
    additional_info: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.message:
            object.__setattr__(self, 'message', self.default_message)
        super().__init__(self.message)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "error_id": self.error_id,
            "error_type": self.__class__.__name__,
            "error_code": self.error_code,
            "message": self.message,
            "category": self.error_category,
            "context": self.context,
            "documentation": self.documentation_link,
            "suggested_action": self.suggested_action,
            "timestamp": datetime.utcnow().isoformat(),
            "root_cause": str(self.root_cause) if self.root_cause else None,
            **self.additional_info
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "BotFrameworkError":
        error_cls: Type[BotFrameworkError] = globals().get(data["error_type"], BotFrameworkError)
        return error_cls(
            message=data.get("message"),
            context=data.get("context", {}),
            root_cause=data.get("root_cause"),
            **data.get("additional_info", {})
        )

    def log(self, logger: logging.Logger = logger, include_traceback: bool = True):
        logger.error(json.dumps(self.to_dict(), default=str), exc_info=include_traceback)

# -------------------- OPERATIONAL ERRORS --------------------

@dataclass
class OperationalError(BotFrameworkError):
    error_category: ClassVar[str] = "OPERATIONAL"
    suggested_action: ClassVar[str] = "Check system health and dependencies"

@dataclass
class CriticalError(OperationalError):
    error_code: ClassVar[int] = 1001
    default_message: ClassVar[str] = "Critical system failure requiring immediate shutdown"
    suggested_action: ClassVar[str] = "Initiate emergency shutdown procedure"
    http_status_code: ClassVar[int] = 500
    is_retryable: ClassVar[bool] = False

    component: str = field(default="unknown")
    failure_type: str = field(default="unspecified")

    def __post_init__(self):
        self.message = f"Critical failure in {self.component}: {self.failure_type}"
        self.context.update({
            "component": self.component,
            "failure_type": self.failure_type
        })
        super().__post_init__()

@dataclass
class TransientError(OperationalError):
    error_code: ClassVar[int] = 1002
    default_message: ClassVar[str] = "Temporary system disturbance detected"
    suggested_action: ClassVar[str] = "Implement retry with exponential backoff"
    http_status_code: ClassVar[int] = 503
    is_retryable: ClassVar[bool] = True

    max_retries: int = 5
    retry_delay: float = 1.0

@dataclass
class RetryableError(OperationalError):
    error_code: ClassVar[int] = 1006
    default_message: ClassVar[str] = "Retryable operational error occurred"
    suggested_action: ClassVar[str] = "Retry the operation with appropriate backoff strategy"
    http_status_code: ClassVar[int] = 429
    is_retryable: ClassVar[bool] = True

    retry_after_seconds: Optional[float] = None

    def __post_init__(self):
        self.context.update({
            "retry_after_seconds": self.retry_after_seconds
        })
        super().__post_init__()

@dataclass
class DegradedServiceError(OperationalError):
    error_code: ClassVar[int] = 1003
    default_message: ClassVar[str] = "Service operating in degraded mode"
    suggested_action: ClassVar[str] = "Failover to secondary systems if available"
    http_status_code: ClassVar[int] = 206
    degradation_level: int = 1

@dataclass
class CircuitBreakerTrippedError(OperationalError):
    error_code: ClassVar[int] = 1004
    default_message: ClassVar[str] = "System protection circuit breaker activated"
    suggested_action: ClassVar[str] = "Wait for automatic reset or manual intervention"
    cooldown_period: float = 300.0

@dataclass
class ResourceExhaustedError(TransientError):
    error_code: ClassVar[int] = 1005
    default_message: ClassVar[str] = "Critical resource exhaustion detected"
    resource_type: str = "memory"

# -------------------- CONFIGURATION ERRORS --------------------

@dataclass
class ConfigurationError(BotFrameworkError):
    error_category: ClassVar[str] = "CONFIGURATION"
    suggested_action: ClassVar[str] = "Validate configuration files and environment variables"

@dataclass
class BotConfigurationError(ConfigurationError):
    error_code: ClassVar[int] = 2001
    default_message: ClassVar[str] = "Invalid bot configuration detected"
    http_status_code: ClassVar[int] = 400

    missing_fields: List[str] = field(default_factory=list)
    invalid_values: Dict[str, str] = field(default_factory=dict)

    def __post_init__(self):
        self.message = f"Missing fields: {self.missing_fields}, Invalid values: {self.invalid_values}"
        self.context.update({
            "missing_fields": self.missing_fields,
            "invalid_values": self.invalid_values
        })
        super().__post_init__()

# -------------------- DATA ERRORS --------------------

@dataclass
class DataError(BotFrameworkError):
    error_category: ClassVar[str] = "DATA"
    suggested_action: ClassVar[str] = "Validate input data sources and formats"

@dataclass
class InvalidMarketDataError(DataError):
    error_code: ClassVar[int] = 3001
    default_message: ClassVar[str] = "Invalid market data structure"
    http_status_code: ClassVar[int] = 422

    data_type: str = "unknown"
    validation_errors: Dict[str, str] = field(default_factory=dict)
    source_system: Optional[str] = None

    def __post_init__(self):
        self.message = f"Invalid {self.data_type} data: {len(self.validation_errors)} validation errors"
        self.context.update({
            "data_type": self.data_type,
            "validation_errors": self.validation_errors,
            "source_system": self.source_system
        })
        super().__post_init__()

@dataclass
class DataValidationError(InvalidMarketDataError):
    error_code: ClassVar[int] = 3002
    default_message: ClassVar[str] = "Data validation rules violated"

# -------------------- STRATEGY ERRORS --------------------

@dataclass
class StrategicError(BotFrameworkError):
    error_category: ClassVar[str] = "STRATEGY"
    suggested_action: ClassVar[str] = "Review strategy parameters and market conditions"

@dataclass
class StrategyExecutionError(StrategicError):
    error_code: ClassVar[int] = 4001
    default_message: ClassVar[str] = "Trading strategy execution failure"
    http_status_code: ClassVar[int] = 500

    strategy_name: str = "unnamed"
    execution_stage: str = "unspecified"
    market_conditions: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        self.message = f"Strategy '{self.strategy_name}' failed at {self.execution_stage} stage"
        self.context.update({
            "strategy_name": self.strategy_name,
            "execution_stage": self.execution_stage,
            "market_conditions": self.market_conditions
        })
        super().__post_init__()

# -------------------- TEST BLOCK --------------------

if __name__ == "__main__":
    try:
        raise InvalidMarketDataError(
            data_type="OHLC",
            validation_errors={
                "timestamp": "Missing required field",
                "volume": "Negative value not allowed"
            },
            source_system="NASDAQ"
        )
    except DataError as e:
        e.log(include_traceback=False)
        logger.info(e.to_dict())

    try:
        raise StrategyExecutionError(
            strategy_name="MeanReversionV2",
            execution_stage="risk-assessment",
            market_conditions={
                "volatility": 0.45,
                "liquidity": "low"
            }
        )
    except StrategicError as e:
        e.log(logger=logging.getLogger("trading"))

