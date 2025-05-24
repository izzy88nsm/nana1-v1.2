# src/strategies/ai_strategy.py

from abc import ABC, abstractmethod
from typing import Any, Deque, Dict, Optional, TypedDict
from datetime import datetime
from collections import deque
import contextlib

from knowledge.formulas import rsi, moving_average
from src.plugins.plugin_manager import PluginManager
from src.market_data.models import MarketData
from src.config.config_schema import StrategyConfig

import structlog
logger = structlog.get_logger(__name__)


class StrategyState(TypedDict, total=False):
    price_history: Deque[float]
    rsi: Optional[float]
    sma_fast: Optional[float]
    sma_slow: Optional[float]
    last_processed: Optional[datetime]


class AIStrategy(ABC):
    """
    Abstract AI Strategy class for algorithmic trading strategies.
    Implements core functionality for technical analysis, plugin integration,
    and state management.

    Attributes:
        config: Strategy configuration parameters
        plugin_manager: Optional plugin manager for external integrations
        state: Dictionary containing strategy indicators and historical data
        last_decision_time: Timestamp of last trade decision

    Methods:
        should_buy: Determine buy signal (abstract)
        should_sell: Determine sell signal (abstract)
        update_state: Update technical indicators and internal state
        run_plugins: Execute registered plugins for signal enhancement
        evaluate_signals: Combine indicators and external signals
        reset_state: Clear strategy state and history
        describe: Generate strategy status report
    """

    def __init__(
        self,
        config: StrategyConfig,
        plugin_manager: Optional[PluginManager] = None,
    ):
        self.config = config
        self.plugin_manager = plugin_manager
        self.state: StrategyState = {
            "price_history": deque(maxlen=config.history_window),
            "rsi": None,
            "sma_fast": None,
            "sma_slow": None,
            "last_processed": None,
        }
        self.last_decision_time: Optional[datetime] = None

    @abstractmethod
    def should_buy(self, market_data: MarketData) -> bool:
        """Determine if current conditions warrant a buy order."""
        raise NotImplementedError

    @abstractmethod
    def should_sell(self, market_data: MarketData) -> bool:
        """Determine if current conditions warrant a sell order."""
        raise NotImplementedError

    def update_state(self, market_data: MarketData) -> None:
        """Update technical indicators and internal state with new market data."""
        try:
            # Update price history with efficient deque structure
            self.state["price_history"].append(market_data.ask)
            self.state["last_processed"] = market_data.timestamp

            # Calculate indicators only when sufficient data exists
            if len(self.state["price_history"]) >= self.config.rsi_period:
                self.state["rsi"] = rsi(
                    self.state["price_history"], 
                    period=self.config.rsi_period
                )

            if len(self.state["price_history"]) >= self.config.sma_fast_period:
                self.state["sma_fast"] = moving_average(
                    self.state["price_history"],
                    period=self.config.sma_fast_period
                )

            if len(self.state["price_history"]) >= self.config.sma_slow_period:
                self.state["sma_slow"] = moving_average(
                    self.state["price_history"],
                    period=self.config.sma_slow_period
                )

        except Exception as e:
            logger.error("State update failed", error=str(e))
            raise

    def run_plugins(self, market_data: MarketData) -> Dict[str, Any]:
        """Execute registered plugins and return aggregated results."""
        if not self.plugin_manager:
            logger.debug("No plugin manager configured")
            return {}

        context = {
            "symbol": market_data.symbol,
            "timestamp": market_data.timestamp,
            "indicators": {
                "rsi": self.state.get("rsi"),
                "sma_fast": self.state.get("sma_fast"),
                "sma_slow": self.state.get("sma_slow"),
            }
        }

        try:
            return self.plugin_manager.run_all(
                "signal",
                context=context,
                data=market_data,
                strategy_state=self.state
            )
        except Exception as e:
            logger.error("Plugin execution failed", error=str(e))
            return {}

    def evaluate_signals(self, market_data: MarketData) -> Dict[str, Any]:
        """Analyze market data and return comprehensive trading signals."""
        self.update_state(market_data)
        
        signals = {
            "timestamp": market_data.timestamp,
            "indicators": {
                "rsi": self.state.get("rsi"),
                "sma_fast": self.state.get("sma_fast"),
                "sma_slow": self.state.get("sma_slow"),
            },
            "plugins": self.run_plugins(market_data),
            "decision": None
        }

        # Add basic crossover signal
        with contextlib.suppress(TypeError):
            if signals["indicators"]["sma_fast"] > signals["indicators"]["sma_slow"]:
                signals["decision"] = "bullish"
            else:
                signals["decision"] = "bearish"

        logger.debug("Signals evaluated", signals=signals)
        return signals

    def reset_state(self) -> None:
        """Clear strategy state and historical data."""
        self.state["price_history"].clear()
        self.state.update({
            "rsi": None,
            "sma_fast": None,
            "sma_slow": None,
            "last_processed": None
        })
        logger.info("Strategy state reset")

    def describe(self) -> Dict[str, Any]:
        """Generate strategy status report with key metrics."""
        return {
            "strategy": self.__class__.__name__,
            "config": self.config.dict(),
            "state_summary": {
                "history_size": len(self.state["price_history"]),
                "latest_rsi": self.state.get("rsi"),
                "latest_sma_fast": self.state.get("sma_fast"),
                "latest_sma_slow": self.state.get("sma_slow"),
                "last_processed": self.state.get("last_processed"),
            },
            "last_decision_time": self.last_decision_time,
            "plugin_count": len(self.plugin_manager) if self.plugin_manager else 0
        }
