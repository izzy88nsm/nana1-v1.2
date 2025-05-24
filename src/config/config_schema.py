from pathlib import Path
from typing import List, Optional, Dict, Any, Literal, Union, Annotated, ClassVar
from pydantic import (
    BaseModel,
    Field,
    PositiveFloat,
    PositiveInt,
    NonNegativeFloat,
    field_validator,
    model_validator,
    SecretStr,
    ValidationError,
    HttpUrl,
    RedisDsn,
    AmqpDsn,
    PostgresDsn,
    ValidationInfo,
    computed_field
)
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic.types import conint, constr
import yaml
import json
import tomli
import os
import logging
import hvac
from cryptography.fernet import Fernet, InvalidToken
from enum import Enum
import importlib
from functools import lru_cache, cached_property
from urllib.parse import urlparse
from ssl import SSLContext, create_default_context
import time

logger = logging.getLogger(__name__)

class TimeInterval(str, Enum):
    SECONDS = "seconds"
    MINUTES = "minutes"
    HOURS = "hours"
    DAYS = "days"
    WEEKS = "weeks"

class EnvironmentType(str, Enum):
    DEVELOPMENT = "dev"
    STAGING = "staging"
    PRODUCTION = "production"

class EncryptionKeyModel(BaseModel):
    current: SecretStr
    previous: Optional[SecretStr] = None
    rotation_interval: conint(ge=86400, le=2592000) = 604800
    next_rotation: Optional[float] = None

class VaultAuthMethod(str, Enum):
    TOKEN = "token"
    APPROLE = "approle"
    AWS_IAM = "aws-iam"

class VaultAppRoleConfig(BaseModel):
    role_id: SecretStr
    secret_id: SecretStr

class RetryPolicy(BaseModel):
    max_retries: int = 3
    backoff_factor: float = 2.0
    timeout: int = 10

class MarketDataSource(BaseModel):
    type: Literal["websocket", "rest", "file"]
    source: str
    refresh_interval: Optional[int] = 60

class MarketPair(BaseModel):
    base: constr(pattern=r"^[A-Z]{3,10}$")
    quote: constr(pattern=r"^[A-Z]{3,10}$")

    @computed_field(return_type=str)
    def symbol(self) -> str:
        return f"{self.base}/{self.quote}"

class ExchangeConfig(BaseModel):
    name: constr(pattern=r"^[a-zA-Z0-9_]+$")
    api_endpoint: HttpUrl
    websocket_endpoint: Optional[HttpUrl] = None
    ssl_verify: bool = True
    timeout: conint(ge=1, le=60) = 10

class RiskParameters(BaseModel):
    max_drawdown: Annotated[float, Field(ge=0.1, le=50.0)] = 5.0
    daily_loss_limit: Annotated[float, Field(ge=0.1, le=25.0)] = 2.0
    position_size_limit: Annotated[float, Field(ge=0.01, le=1.0)] = 0.5
    leverage: Annotated[float, Field(ge=1.0, le=100.0)] = 1.0
    volatility_threshold: Annotated[float, Field(ge=0.01, le=10.0)] = 2.5

class EncryptionConfig(BaseModel):
    enabled: bool = Field(False)
    algorithm: Literal["AES-256-GCM", "Fernet-256"] = "Fernet-256"
    keys: EncryptionKeyModel
    key_store: Literal["vault", "file"] = "file"
    key_path: Optional[Path] = Field(None)

    @model_validator(mode='after')
    def validate_key_source(self) -> "EncryptionConfig":
        if self.enabled and self.key_store == "file" and not self.key_path.exists():
            raise FileNotFoundError(f"Encryption key file missing: {self.key_path}")
        return self

class VaultConfig(BaseModel):
    enabled: bool = False
    endpoint: HttpUrl
    auth_method: VaultAuthMethod = VaultAuthMethod.TOKEN
    token: Optional[SecretStr] = None
    approle: Optional[VaultAppRoleConfig] = None
    secrets_path: str = "trading-app/secrets"
    namespace: Optional[str] = None
    ssl_verify: bool = True
    timeout: conint(ge=1, le=60) = 5

    @model_validator(mode='after')
    def validate_auth_method(self) -> "VaultConfig":
        if self.enabled:
            if self.auth_method == VaultAuthMethod.TOKEN and not self.token:
                raise ValueError("Token required for token auth method")
            if self.auth_method == VaultAuthMethod.APPROLE and not self.approle:
                raise ValueError("AppRole config required for approle auth method")
        return self

class NurseResourceThresholds(BaseModel):
    cpu: float = 85.0
    memory: float = 85.0
    disk: float = 90.0

class NurseTimeouts(BaseModel):
    check_plugins: int = 60
    check_dependencies: int = 30
    check_memory_and_cpu: int = 30

class NurseSettings(BaseModel):
    plugin_failure_threshold: int = 3
    timeouts: NurseTimeouts = NurseTimeouts()
    resource_thresholds: NurseResourceThresholds = NurseResourceThresholds()
    data_feed_timeout: int = 5
    monitored_partitions: List[str] = ["/"]

class AppConfig(BaseSettings):
    class Config:
        env_prefix = "APP_"
        env_file = ".env"
        case_sensitive = False

        retry: RetryPolicy = Field(default_factory=RetryPolicy)
        nurse: NurseSettings = Field(default_factory=NurseSettings)

    version: str
    environment: EnvironmentType
    core: Dict[str, Any]
    bots: List[Dict[str, Any]]
    metrics: Dict[str, Any] = Field(default_factory=dict)
    encryption: Optional[EncryptionConfig] = None
    vault: Optional[VaultConfig] = None
    secrets: Dict[str, SecretStr] = Field(default_factory=dict)
    retry: RetryPolicy = Field(default_factory=RetryPolicy)

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "AppConfig":
        config_path = path or Path(os.getenv("APP_CONFIG", "config/config.yaml"))
        config_data = cls._load_config_file(config_path)
        config_data = cls._decrypt_local_secrets(config_data)
        return cls(**config_data)

    @classmethod
    def _load_config_file(cls, path: Path) -> Dict[str, Any]:
        if not path.exists():
            raise FileNotFoundError(f"Configuration file not found: {path}")
        with path.open("r") as f:
            if path.suffix.lower() == ".yaml":
                return yaml.safe_load(f)
            elif path.suffix.lower() == ".json":
                return json.load(f)
            elif path.suffix.lower() == ".toml":
                return tomli.load(f)
            else:
                raise ValueError(f"Unsupported config format: {path.suffix}")

    @classmethod
    def _decrypt_local_secrets(cls, config: Dict[str, Any]) -> Dict[str, Any]:
        if config.get("encryption", {}).get("enabled", False):
            try:
                key_path = config["encryption"].get("key_path")
                if not key_path:
                    raise ValueError("Encryption key path not provided")
                key = Path(key_path).read_text().strip()
                fernet = Fernet(key)
                for secret_key in config.get("secrets", {}):
                    encrypted_value = config["secrets"][secret_key]
                    config["secrets"][secret_key] = fernet.decrypt(encrypted_value.encode()).decode()
            except (InvalidToken, ValueError, FileNotFoundError) as e:
                logger.error("Secret decryption failed", exc_info=True)
                raise RuntimeError("Invalid encryption key or file missing") from e
        return config

@lru_cache(maxsize=1)
def get_config() -> AppConfig:
    return AppConfig.load()

def reload_config() -> None:
    get_config.cache_clear()

class BacktestStrategyConfig(BaseModel):
    rsi_period: int = 14
    sma_fast_period: int = 5
    sma_slow_period: int = 20
    rsi_oversold: float = 30.0
    rsi_overbought: float = 70.0
    plugin_confidence_threshold: float = 0.7
    risk: Dict[str, Union[float, int]] = Field(
        default_factory=lambda: {
            "max_position_size": 1.0,
            "risk_per_trade": 0.02,
            "cool_off_period": 60
        }
    )