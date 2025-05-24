# src/strategies/momentum_strategy.py

from src.strategies.ai_strategy import AIStrategy
from src.market_data.models import MarketData
from src.config.strategy_config_schema import StrategyConfig
from typing import Optional, Dict
import structlog
from datetime import datetime, timedelta

logger = structlog.get_logger(__name__)

class MomentumStrategy(AIStrategy):
    """
    Enhanced momentum strategy combining technical indicators with risk management.
    Features:
    - SMA crossover confirmation system
    - RSI-based entry/exit with threshold buffers
    - Dynamic position sizing
    - Trade cool-off periods
    - Integrated plugin signals
    - Price action confirmation
    """

    def __init__(self, config: StrategyConfig, plugin_manager=None):
        super().__init__(config=config, plugin_manager=plugin_manager)
        self._position_size: Optional[float] = None
        self._last_crossover: Optional[datetime] = None

    def should_buy(self, market_data: MarketData) -> bool:
        try:
            if not self._validate_market_data(market_data):
                return False

            if self._in_cooldown_period():
                logger.debug("Buy check skipped: In cool-down period")
                return False

            signals = self.evaluate_signals(market_data)
            decision = (
                self._check_technical_conditions(signals, "buy") 
                and self._check_risk_parameters()
                and self._check_plugin_signals(signals, "buy")
                and self._check_price_confirmation(market_data)
            )

            self._log_decision("BUY", signals, decision)
            if decision:
                self._update_position_size(signals)
                self._last_decision_time = datetime.now()
            return decision

        except Exception as e:
            logger.error("Buy evaluation failed", error=str(e))
            return False

    def should_sell(self, market_data: MarketData) -> bool:
        try:
            if not self._validate_market_data(market_data):
                return False

            signals = self.evaluate_signals(market_data)
            decision = (
                self._check_technical_conditions(signals, "sell")
                or self._check_stop_conditions(market_data)
                or self._check_plugin_signals(signals, "sell")
            )

            self._log_decision("SELL", signals, decision)
            return decision

        except Exception as e:
            logger.error("Sell evaluation failed", error=str(e))
            return False

    def _validate_market_data(self, market_data: MarketData) -> bool:
        required_fields = ['ask', 'bid', 'volume', 'timestamp']
        if any(not hasattr(market_data, f) for f in required_fields):
            logger.error("Invalid market data structure")
            return False

        if market_data.ask <= 0 or market_data.bid <= 0:
            logger.warning("Invalid price data", ask=market_data.ask, bid=market_data.bid)
            return False

        return True

    def _check_technical_conditions(self, signals: Dict, action: str) -> bool:
        indicators = signals.get("indicators", {})
        rsi = indicators.get("rsi")
        sma_fast = indicators.get("sma_fast")
        sma_slow = indicators.get("sma_slow")

        if None in (rsi, sma_fast, sma_slow):
            return False

        rsi_ok = {
            "buy": rsi <= (self.config.rsi_oversold * 0.98),
            "sell": rsi >= (self.config.rsi_overbought * 1.02)
        }[action]

        crossover = self._check_crossover(sma_fast, sma_slow)
        sma_ok = {
            "buy": crossover == "bullish",
            "sell": crossover == "bearish"
        }[action]

        return rsi_ok and sma_ok

    def _check_crossover(self, fast: float, slow: float) -> Optional[str]:
        current_state = "bullish" if fast > slow else "bearish"

        if "last_crossover_state" not in self.state:
            self.state["last_crossover_state"] = current_state
            return None

        if current_state != self.state["last_crossover_state"]:
            self.state["last_crossover_time"] = datetime.now()
            self.state["last_crossover_state"] = current_state
            logger.info(f"New SMA crossover detected: {current_state}")

        if (datetime.now() - self.state.get("last_crossover_time", datetime.now())) < timedelta(minutes=5):
            return None

        return current_state

    def _check_risk_parameters(self) -> bool:
        risk = self.config.risk

        if self._position_size and self._position_size >= risk.max_position_size:
            logger.debug("Max position size reached")
            return False

        if getattr(self, '_active_trades', 0) >= getattr(self.config, 'max_concurrent_trades', 5):
            logger.debug("Max concurrent trades reached")
            return False

        return True

    def _check_plugin_signals(self, signals: Dict, action: str) -> bool:
        plugin_data = signals.get("plugins", {})
        confidence = plugin_data.get(f"{action}_confidence", 0)

        if plugin_data.get(f"block_{action}", False):
            logger.info(f"Plugin blocking {action} action")
            return False

        return confidence >= getattr(self.config, "plugin_confidence_threshold", 0.7)

    def _check_price_confirmation(self, market_data: MarketData) -> bool:
        if market_data.close < self.state.get("sma_slow"):
            logger.debug("Price below SMA slow - rejecting buy")
            return False

        avg_volume = self.state.get("avg_volume", market_data.volume)
        if market_data.volume < (avg_volume * 1.2):
            logger.debug("Insufficient volume for entry")
            return False

        return True

    def _update_position_size(self, signals: Dict) -> None:
        recent_atr = signals.get("indicators", {}).get("atr")
        risk = self.config.risk

        if recent_atr and risk.risk_per_trade:
            self._position_size = min(
                (risk.risk_per_trade / recent_atr),
                risk.max_position_size
            )
            logger.info("Updated position size", size=self._position_size)

    def _in_cooldown_period(self) -> bool:
        if not self._last_decision_time or not self.config.risk.cool_off_period:
            return False

        elapsed = datetime.now() - self._last_decision_time
        return elapsed < self.config.risk.cool_off_period

    def _log_decision(self, action: str, signals: Dict, decision: bool) -> None:
        logger.debug(
            f"{action} Decision",
            decision=decision,
            indicators=signals.get("indicators"),
            plugins=signals.get("plugins"),
            position_size=self._position_size,
            risk_params=self.config.risk.dict(),
            state=self.state
        )

