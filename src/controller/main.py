# src/controller/main.py

import asyncio
import signal
import sys
import uuid
import logging
from datetime import datetime, timedelta
from functools import partial
from collections import defaultdict, deque
from contextlib import AsyncExitStack, suppress
from typing import List, Optional, Dict, Any, AsyncGenerator

import psutil
from dotenv import load_dotenv
from pydantic import ValidationError
from prometheus_client import start_http_server, Counter, Gauge, Histogram

from config.config_schema import get_config, AppConfig, reload_config
from config.logging_config import get_logger, LoggerConfiguration, LogFormat
from src.datacenter.market_observer import MarketObserver
from src.datacenter.base_datacenter import BaseDataCenter
from src.bots.base_bot import BaseBot
from src.strategies.ai_strategy import AIStrategy
from src.market_data.data_feed import MarketDataFeed, DataFeedError
from src.risk.risk_manager import RiskManager
from src.utils.metrics import ExecutionMetrics
from src.utils.exceptions import CriticalError, TransientError
from src.utils.circuit_breaker import CircuitBreaker
from src.utils.healthcheck import HealthCheckServer
from src.utils.notifications import NotificationManager

logger = get_logger(LoggerConfiguration(
    name="trading_app",
    level=logging.INFO,
    enable_console=True,
    console_format=LogFormat.STRUCTURED
))

TRADE_COUNTER = Counter('trading_trades_total', 'Total trade executions', ['bot_id', 'status'])
LATENCY_HISTOGRAM = Histogram('trading_latency_seconds', 'Trade execution latency')
RESOURCE_GAUGE = Gauge('trading_resource_usage', 'System resource usage', ['resource_type'])

SHUTDOWN_SIGNALS = (signal.SIGINT, signal.SIGTERM)
GRACE_PERIOD = 30

class TradingMetrics:
    def __init__(self):
        self._trade_history = deque(maxlen=1000)
        self._performance_stats = defaultdict(timedelta)

    def collect(self):
        return {
            "active_trades": len(self._trade_history),
            "hourly_volume": sum(t['latency'] for t in self._trade_history),
            "success_rate": self._calculate_success_rate()
        }

    def _calculate_success_rate(self):
        total = len(self._trade_history)
        if total == 0:
            return 0.0
        success = sum(1 for t in self._trade_history if t['status'] == 'success')
        return success / total

    def track_execution(self, bot_id: str):
        start_time = datetime.now()
        trade_id = str(uuid.uuid4())

        def record_outcome(success: bool):
            latency = (datetime.now() - start_time).total_seconds()
            status = "success" if success else "failed"
            LATENCY_HISTOGRAM.observe(latency)
            TRADE_COUNTER.labels(bot_id=bot_id, status=status).inc()
            self._trade_history.append({
                "id": trade_id,
                "latency": latency,
                "status": status,
                "timestamp": datetime.now().isoformat()
            })

        return record_outcome

class ApplicationState:
    def __init__(self, config: AppConfig):
        self.shutdown_event = asyncio.Event()
        self.active_trades = defaultdict(deque)
        self.metrics = TradingMetrics()
        self.circuit_breaker = CircuitBreaker(
            max_failures=config.core.circuit_breaker["max_errors"],
            reset_timeout=config.core.circuit_breaker["reset_timeout"]
        )
        self.health_status = {
            "data_feed": False,
            "risk_manager": False,
            "bots": defaultdict(bool)
        }

async def managed_data_feed(config: AppConfig) -> AsyncGenerator[MarketDataFeed, None]:
    retry_policy = {"max_retries": 5, "backoff_factor": 1.5, "timeout": 10}
    feed = MarketDataFeed(config)
    try:
        for attempt in range(retry_policy["max_retries"]):
            try:
                await feed.connect()
                logger.info("Data feed connection established")
                yield feed
                return
            except DataFeedError as e:
                if attempt == retry_policy["max_retries"] - 1:
                    logger.critical("Data feed connection failed after retries")
                    raise CriticalError("Data feed failure") from e
                delay = retry_policy["backoff_factor"] ** attempt
                logger.warning(f"Data feed connection failed, retrying in {delay:.1f} seconds")
                await asyncio.sleep(delay)
    finally:
        await feed.disconnect()

