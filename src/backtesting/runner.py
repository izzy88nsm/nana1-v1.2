
import csv
from pathlib import Path
from typing import Callable, Optional, List, Dict, Any
from datetime import datetime
from decimal import Decimal
import logging
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../')))


from strategies.momentum_strategy import MomentumStrategy
from config.strategy_config_schema import StrategyConfig
from market_data.models import MarketData

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class Trade:
    def __init__(self, entry_action: str, entry_price: Decimal, entry_time: datetime):
        self.entry_action = entry_action
        self.entry_price = entry_price
        self.entry_time = entry_time
        self.exit_price: Optional[Decimal] = None
        self.exit_time: Optional[datetime] = None
        self.pnl: Optional[Decimal] = None
        self.result: Optional[str] = None
        self.duration: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "entry_action": self.entry_action,
            "entry_price": str(self.entry_price),
            "exit_price": str(self.exit_price) if self.exit_price else None,
            "pnl": str(self.pnl) if self.pnl else None,
            "result": self.result,
            "duration": self.duration,
            "entry_time": self.entry_time.isoformat(),
            "exit_time": self.exit_time.isoformat() if self.exit_time else None
        }


class BacktestResult:
    def __init__(self, initial_balance: Decimal = Decimal("10000")):
        self.trades: List[Trade] = []
        self.open_trade: Optional[Trade] = None
        self.initial_balance = initial_balance
        self.equity = initial_balance
        self.equity_curve: List[Decimal] = [initial_balance]
        self.max_drawdown = Decimal("0")
        self.peak_equity = initial_balance

    def log_entry(self, action: str, price: Decimal, timestamp: datetime):
        if self.open_trade:
            self._force_close_trade(price, timestamp)
        self.open_trade = Trade(action, price, timestamp)
        logger.debug(f"Entered {action} trade at {price}")

    def log_exit(self, exit_price: Decimal, timestamp: datetime, reason: str):
        if not self.open_trade:
            return

        trade = self.open_trade
        trade.exit_price = exit_price
        trade.exit_time = timestamp
        trade.pnl = exit_price - trade.entry_price if trade.entry_action == "buy" else trade.entry_price - exit_price
        trade.result = "win" if trade.pnl > 0 else "loss"
        trade.duration = (trade.exit_time - trade.entry_time).total_seconds()

        self.equity += trade.pnl
        self.equity_curve.append(self.equity)

        self.peak_equity = max(self.peak_equity, self.equity)
        drawdown = self.peak_equity - self.equity
        if drawdown > self.max_drawdown:
            self.max_drawdown = drawdown

        self.trades.append(trade)
        self.open_trade = None
        logger.debug(f"Exited trade with P&L: {trade.pnl}")

    def _force_close_trade(self, price: Decimal, timestamp: datetime):
        logger.warning("Forcing close of previous trade")
        self.log_exit(price, timestamp, "forced_close")

    def summary(self) -> Dict[str, Any]:
        if not self.trades:
            return {"message": "No trades executed"}
        total_profit = sum(t.pnl for t in self.trades)
        win_count = sum(1 for t in self.trades if t.result == "win")
        loss_count = len(self.trades) - win_count
        win_rate = win_count / len(self.trades) if self.trades else 0

        return {
            "total_trades": len(self.trades),
            "win_rate": round(win_rate, 4),
            "total_profit": str(total_profit),
            "max_drawdown": str(self.max_drawdown),
            "final_equity": str(self.equity),
            "return_pct": str((self.equity - self.initial_balance) / self.initial_balance * 100),
            "avg_trade_duration": sum(t.duration for t in self.trades) / len(self.trades) if self.trades else 0,
            "best_trade": str(max(t.pnl for t in self.trades)) if self.trades else None,
            "worst_trade": str(min(t.pnl for t in self.trades)) if self.trades else None,
        }


def run_backtest(
    data_file: Path,
    strategy_factory: Callable[[], MomentumStrategy],
    initial_balance: Decimal = Decimal("10000")
) -> BacktestResult:
    result = BacktestResult(initial_balance=initial_balance)
    data_points = []

    try:
        with open(data_file, "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                data_points.append(MarketData(
                    bid=Decimal(row["bid"]),
                    ask=Decimal(row["ask"]),
                    timestamp=datetime.fromisoformat(row["timestamp"]),
                    symbol="BTC/USDT", exchange="sim", interval="1m",
                    close=Decimal(row["close"]), open=Decimal(row["open"]),
                    high=Decimal(row["high"]), low=Decimal(row["low"]),
                    last_price=Decimal(row["close"]), volume_base=Decimal(row["volume_base"]),
                    volume_quote=Decimal(row["volume_quote"]), trade_count=int(row["trade_count"]),
                    bids=[], asks=[], latency_ms=Decimal("0"), data_quality=Decimal("1.0"),
                    asset_class="spot", source="simulated"
                ))
    except Exception as e:
        logger.error(f"Error loading data: {e}")
        return result

    data_points.sort(key=lambda x: x.timestamp)
    strategy = strategy_factory()

    for data in data_points:
        strategy.update_state(data)
        if strategy.should_sell(data) and result.open_trade:
            result.log_exit(data.bid, data.timestamp, "sell_signal")
        if strategy.should_buy(data) and not result.open_trade:
            result.log_entry("buy", data.ask, data.timestamp)

    if result.open_trade and data_points:
        last_data = data_points[-1]
        result.log_exit(last_data.bid, last_data.timestamp, "end_of_backtest")

    logger.info("Backtest completed")
    return result


def export_results(result: BacktestResult, output_dir: Path):
    output_dir.mkdir(parents=True, exist_ok=True)
    with open(output_dir / "trades.csv", "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "entry_action", "entry_price", "exit_price",
            "pnl", "result", "duration", "entry_time", "exit_time"
        ])
        writer.writeheader()
        for trade in result.trades:
            writer.writerow(trade.to_dict())

    with open(output_dir / "equity.csv", "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["equity"])
        for value in result.equity_curve:
            writer.writerow([str(value)])


if __name__ == "__main__":
    config = StrategyConfig(
        rsi_period=14,
        sma_fast_period=5,
        sma_slow_period=20,
        rsi_oversold=30,
        rsi_overbought=70,
        risk={"max_position_size": 1.0, "risk_per_trade": 0.02, "cool_off_period": 60},
        plugin_confidence_threshold=0.7
    )

    def strategy_factory():
        return MomentumStrategy(config=config)

    result = run_backtest(
        data_file=Path("data/advanced_backtest_data.csv"),
        strategy_factory=strategy_factory,
        initial_balance=Decimal("10000")
    )

    print("Backtest Summary:")
    for k, v in result.summary().items():
        print(f"{k:>20}: {v}")

    export_results(result, Path("results/"))