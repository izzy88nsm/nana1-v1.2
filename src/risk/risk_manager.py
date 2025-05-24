# src/risk/risk_manager.py

import logging
from typing import Any, Optional, Dict, Tuple, List
from dataclasses import dataclass
from functools import lru_cache
import time
import json
from contextlib import contextmanager
from collections import defaultdict
from pydantic import BaseModel, ValidationError
import numpy as np
from src.utils.circuit_breaker import CircuitBreaker

logger = logging.getLogger(__name__)

# ─── Data Models ──────────────────────────────────────────────────────────────
class RiskParameters(BaseModel):
    max_position_size: float = 1000000.0
    max_daily_loss: float = 0.05  # 5%
    max_exposure: float = 0.25    # 25% of capital
    volatility_threshold: float = 0.15
    max_leverage: int = 5
    stop_loss_pct: float = 0.03   # 3%
    concentration_limit: float = 0.1  # 10% per instrument
    liquidation_timeout: int = 300  # 5 minutes
    risk_window_size: int = 1000  # Number of trades to consider
    cooling_period: int = 60      # 1 minute after breach

class TradeData(BaseModel):
    symbol: str
    quantity: float
    price: float
    position_type: str  # "long" or "short"
    portfolio_value: float
    current_exposure: float
    historical_volatility: float

# ─── Core Implementation ──────────────────────────────────────────────────────
class RiskManager:
    def __init__(
        self,
        config: RiskParameters,
        circuit_breaker: Optional[CircuitBreaker] = None,
        metrics_enabled: bool = True
    ):
        self.config = config
        self.circuit_breaker = circuit_breaker or CircuitBreaker()
        self.metrics_enabled = metrics_enabled
        self._lock = threading.RLock()
        self._risk_metrics = defaultdict(float)
        self._exposure_history = deque(maxlen=config.risk_window_size)
        self._last_breach_time = 0.0
        self._position_book = defaultdict(float)
        self._volatility_cache = {}
        
        logger.info("RiskManager initialized with config: %s", config.dict())

    def load_configuration(self, new_config: Dict[str, Any]) -> None:
        """Hot-reload risk parameters with validation"""
        try:
            with self._lock:
                self.config = RiskParameters(**new_config)
                logger.info("Successfully reloaded risk parameters")
        except ValidationError as e:
            logger.error("Invalid risk configuration: %s", e)
            raise ValueError("Invalid risk parameters") from e

    def assess_risk(self, trade_data: TradeData) -> Tuple[bool, List[str]]:
        """Comprehensive risk assessment with multiple checks"""
        if self.circuit_breaker.is_tripped():
            return False, ["circuit_breaker_active"]

        if time.time() - self._last_breach_time < self.config.cooling_period:
            return False, ["cooling_period_active"]

        checks = [
            self._check_exposure_limit,
            self._check_position_size,
            self._check_stop_loss,
            self._check_volatility,
            self._check_concentration,
            self._check_liquidity
        ]

        try:
            passed = True
            reasons = []
            
            for check in checks:
                check_passed, message = check(trade_data)
                if not check_passed:
                    passed = False
                    reasons.append(message)
                    
                    if "critical" in message:
                        self.circuit_breaker.trip()
                        self._last_breach_time = time.time()
                        logger.critical("Critical risk breach: %s", message)
                        
            self._update_position_book(trade_data)
            self._update_metrics(passed, reasons)
            
            return passed, reasons
            
        except Exception as e:
            logger.error("Risk assessment failed: %s", str(e), exc_info=True)
            self.circuit_breaker.trip()
            return False, ["system_error"]

    def _check_exposure_limit(self, data: TradeData) -> Tuple[bool, str]:
        """Check total exposure limits"""
        exposure = data.current_exposure + (data.quantity * data.price)
        if exposure > self.config.max_exposure * data.portfolio_value:
            return False, "exposure_limit_exceeded"
        return True, "exposure_ok"

    def _check_position_size(self, data: TradeData) -> Tuple[bool, str]:
        """Validate position size against limits"""
        position_value = abs(data.quantity * data.price)
        if position_value > self.config.max_position_size:
            return False, "position_size_exceeded"
        return True, "position_size_ok"

    def _check_stop_loss(self, data: TradeData) -> Tuple[bool, str]:
        """Dynamic stop-loss calculation"""
        current_volatility = self._get_volatility(data.symbol)
        dynamic_sl = self.config.stop_loss_pct * current_volatility
        potential_loss = data.quantity * data.price * dynamic_sl
        
        if potential_loss > self.config.max_daily_loss * data.portfolio_value:
            return False, "stop_loss_triggered"
        return True, "stop_loss_ok"

    def _check_volatility(self, data: TradeData) -> Tuple[bool, str]:
        """Volatility-based risk check"""
        if data.historical_volatility > self.config.volatility_threshold:
            return False, "high_volatility"
        return True, "volatility_ok"

    def _check_concentration(self, data: TradeData) -> Tuple[bool, str]:
        """Portfolio concentration check"""
        instrument_exposure = (data.quantity * data.price) / data.portfolio_value
        if instrument_exposure > self.config.concentration_limit:
            return False, "concentration_limit_exceeded"
        return True, "concentration_ok"

    def _check_liquidity(self, data: TradeData) -> Tuple[bool, str]:
        """Market liquidity estimation check"""
        # Placeholder for liquidity estimation model
        liquidity_risk = 0.0  # Implement actual liquidity calculation
        if liquidity_risk > 0.8:  # Threshold for illiquid instruments
            return False, "insufficient_liquidity"
        return True, "liquidity_ok"

    @lru_cache(maxsize=1024)
    def _get_volatility(self, symbol: str) -> float:
        """Cached volatility lookup with fallback"""
        return self._volatility_cache.get(symbol, 0.1)  # 10% default

    def _update_position_book(self, data: TradeData) -> None:
        """Maintain real-time position tracking"""
        with self._lock:
            self._position_book[data.symbol] += data.quantity

    def _update_metrics(self, passed: bool, reasons: List[str]) -> None:
        """Update risk metrics and monitoring statistics"""
        if not self.metrics_enabled:
            return
            
        with self._lock:
            self._risk_metrics["total_checks"] += 1
            self._risk_metrics["passed_checks"] += int(passed)
            
            for reason in reasons:
                self._risk_metrics[f"breach_{reason}"] += 1

    def get_risk_metrics(self) -> Dict[str, float]:
        """Get current risk metrics snapshot"""
        with self._lock:
            return dict(self._risk_metrics)

    @contextmanager
    def risk_context(self, portfolio_state: Dict[str, Any]):
        """Context manager for risk-aware trading sessions"""
        try:
            self._pre_trade_checks(portfolio_state)
            yield
            self._post_trade_checks(portfolio_state)
        except RiskViolation as e:
            self._handle_risk_violation(e)
            raise

    def _pre_trade_checks(self, portfolio_state: Dict[str, Any]) -> None:
        """Pre-trade portfolio-wide checks"""
        if portfolio_state["leverage"] > self.config.max_leverage:
            raise RiskViolation("excessive_leverage")

    def _post_trade_checks(self, portfolio_state: Dict[str, Any]) -> None:
        """Post-trade validation"""
        self._check_liquidation_timeout()

    def _check_liquidation_timeout(self) -> None:
        """Monitor for forced liquidation scenarios"""
        if time.time() - self._last_breach_time > self.config.liquidation_timeout:
            self.trigger_liquidation()

    def trigger_liquidation(self) -> None:
        """Initiate emergency liquidation procedures"""
        logger.critical("Initiating emergency liquidation")
        self.circuit_breaker.trip()
        # Implement actual liquidation logic here

# ─── Exceptions ───────────────────────────────────────────────────────────────
class RiskViolation(Exception):
    """Critical risk management violation occurred"""

# ─── Usage Example ────────────────────────────────────────────────────────────
if __name__ == "__main__":
    config = RiskParameters(
        max_position_size=500000,
        max_daily_loss=0.03,
        volatility_threshold=0.2
    )
    
    risk_mgr = RiskManager(config)
    
    trade_data = TradeData(
        symbol="AAPL",
        quantity=100,
        price=150.0,
        position_type="long",
        portfolio_value=1000000,
        current_exposure=0.15,
        historical_volatility=0.12
    )
    
    approved, reasons = risk_mgr.assess_risk(trade_data)
    logger.info(f"Trade approved: {approved}, Reasons: {reasons}")
