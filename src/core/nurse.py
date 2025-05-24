import asyncio
import logging
import os
import psutil
import hashlib
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple
from functools import wraps
import backoff
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from src.config.config_schema import AppConfig
from src.bots.plugins import PluginManager
from src.utils.healthcheck import HealthCheckManager as DependencyManager
from src.utils.exceptions import CriticalError
from src.utils.metrics import SystemHealthMetrics

logger = logging.getLogger(__name__)

def healthcheck_handler(max_retries: int = 3, timeout: int = 30):
    def decorator(func):
        @wraps(func)
        async def wrapper(self, *args, **kwargs):
            try:
                return await asyncio.wait_for(
                    func(self, *args, **kwargs),
                    timeout=self.config.nurse.timeouts.get(func.__name__, timeout)
                )
            except asyncio.TimeoutError:
                logger.error(f"⌛ Timeout during {func.__name__} check")
                SystemHealthMetrics.record_timeout(func.__name__)
            except Exception as e:
                logger.error(f"❌ Unexpected error in {func.__name__}: {str(e)}", exc_info=True)
                raise
        return wrapper
    return decorator

class ConfigFileHandler(FileSystemEventHandler):
    def __init__(self, nurse_instance):
        self.nurse = nurse_instance
    
    def on_modified(self, event):
        if event.src_path.endswith("config.yaml"):
            logger.warning("⚠️ Config file modification detected!")
            asyncio.create_task(self.nurse.check_config_signature(urgent=True))

class Nurse:
    VERSION = "1.1.0"

    def __init__(self, config: AppConfig, plugin_manager: PluginManager, dependency_manager: DependencyManager):
        self.config = config
        self.plugin_manager = plugin_manager
        self.dependency_manager = dependency_manager
        self.last_health_snapshot: Dict[str, str] = {}
        self._setup_file_watcher()
        self.circuit_breakers: Dict[str, Tuple[datetime, int]] = {}
        self.plugin_states: Dict[str, Dict] = {}

    def _setup_file_watcher(self):
        self.observer = Observer()
        event_handler = ConfigFileHandler(self)
        self.observer.schedule(event_handler, path='config/', recursive=False)
        self.observer.start()

    async def run_full_checkup(self) -> None:
        logger.info("🩺 Nurse: Beginning full system health check...")
        
        check_tasks = [
            self.check_dependencies(),
            self.check_plugins(),
            self.check_memory_and_cpu(),
            self.check_config_signature(),
            self.check_data_feeds(),
            self.check_disk_usage()
        ]
        
        try:
            await asyncio.gather(*check_tasks)
            logger.info("✅ Nurse: System check completed successfully.")
        except Exception as e:
            logger.error(f"❌ System check failed critically: {str(e)}")
            raise CriticalError("Full system check failed") from e

    @healthcheck_handler(max_retries=3, timeout=60)
    @backoff.on_exception(backoff.expo, Exception, max_tries=3)
    async def check_plugins(self) -> None:
        logger.info("🔍 Nurse: Checking plugin health...")
        status_summary = self.plugin_manager.status_summary()
        reset_tasks = []
        
        for plugin_name, status in status_summary.items():
            state = self.plugin_states.setdefault(plugin_name, {
                'failure_count': 0,
                'last_failure': None,
                'last_reset': None
            })
            
            if status == "failing":
                state['failure_count'] += 1
                state['last_failure'] = datetime.now()
                
                if state['failure_count'] >= self.config.nurse.plugin_failure_threshold:
                    logger.warning(f"⚠️ Plugin '{plugin_name}' has failed {state['failure_count']} times. Attempting reset...")
                    reset_tasks.append(self._reset_plugin_with_retry(plugin_name, state))
        
        if reset_tasks:
            await asyncio.gather(*reset_tasks)

    async def _reset_plugin_with_retry(self, plugin_name: str, state: Dict):
        try:
            await self.plugin_manager.reset_plugin(plugin_name)
            state.update({
                'failure_count': 0,
                'last_reset': datetime.now()
            })
            logger.info(f"✅ Plugin '{plugin_name}' successfully reset.")
        except Exception as e:
            logger.error(f"❌ Plugin '{plugin_name}' reset failed: {e}")
            SystemHealthMetrics.record_plugin_failure(plugin_name)

    @healthcheck_handler()
    async def check_dependencies(self) -> None:
        logger.info("🔍 Nurse: Checking critical system dependencies...")
        health = await self.dependency_manager.run_all_checks()
        for name, result in health.items():
            if not result.get("success", False):
                if self._check_circuit_breaker(name):
                    logger.debug(f"⚠️ Dependency check failed for {name} (circuit breaker active)")
                    continue
                logger.warning(f"⚠️ Dependency check failed: {name}")
                SystemHealthMetrics.record_failure(name)
                self._update_circuit_breaker(name)

    def _check_circuit_breaker(self, name: str) -> bool:
        if name not in self.circuit_breakers:
            return False
        last_failure, count = self.circuit_breakers[name]
        cooldown = min(2 ** count, 300)
        return datetime.now() < last_failure + timedelta(seconds=cooldown)

    def _update_circuit_breaker(self, name: str):
        current_count = self.circuit_breakers.get(name, (datetime.now(), 0))[1]
        self.circuit_breakers[name] = (datetime.now(), current_count + 1)

    @healthcheck_handler()
    async def check_memory_and_cpu(self) -> None:
        logger.info("🔍 Nurse: Checking system resource usage...")
        samples = 3
        cpu_readings, mem_readings = [], []
        
        for _ in range(samples):
            mem = psutil.virtual_memory()
            cpu = psutil.cpu_percent(interval=0.5)
            cpu_readings.append(cpu)
            mem_readings.append(mem.percent)
            await asyncio.sleep(0.5)
        
        avg_cpu = sum(cpu_readings) / samples
        avg_mem = sum(mem_readings) / samples
        
        thresholds = self.config.nurse.resource_thresholds
        if avg_mem > thresholds.memory:
            logger.warning(f"⚠️ High memory usage: {avg_mem:.1f}%")
        if avg_cpu > thresholds.cpu:
            logger.warning(f"⚠️ High CPU usage: {avg_cpu:.1f}%")
        SystemHealthMetrics.record_resource_usage(cpu=avg_cpu, memory=avg_mem)

    @healthcheck_handler()
    async def check_config_signature(self, urgent: bool = False) -> None:
        log_method = logger.warning if urgent else logger.info
        log_method("🔍 Nurse: Validating config.yaml integrity...")
        
        config_path = os.path.join("config", "config.yaml")
        try:
            hasher = hashlib.sha256()
            with open(config_path, "rb") as f:
                while chunk := f.read(4096):
                    hasher.update(chunk)
            checksum = hasher.hexdigest()
            if self.config.system.expected_config_hash:
                if checksum != self.config.system.expected_config_hash:
                    raise CriticalError(f"⚠️ Config hash mismatch. Expected {self.config.system.expected_config_hash}, got {checksum}")
            logger.info("✅ Config file integrity verified.")
        except FileNotFoundError:
            logger.error("❌ config.yaml not found.")
            raise
        except Exception as e:
            logger.error(f"❌ Config signature check failed: {e}")
            raise

    @healthcheck_handler()
    async def check_data_feeds(self) -> None:
        logger.info("🔍 Nurse: Checking market data feeds...")
        timeout = self.config.nurse.data_feed_timeout
        feeds = self.config.data_feeds.active_feeds
        check_tasks = [self._check_single_feed(feed, timeout) for feed in feeds]
        results = await asyncio.gather(*check_tasks, return_exceptions=True)
        
        for feed, result in zip(feeds, results):
            if isinstance(result, Exception):
                logger.error(f"❌ Data feed check failed: {feed} – {result}")
                SystemHealthMetrics.record_data_feed_failure(feed)
            else:
                latency, success = result
                if success:
                    logger.info(f"✅ Data feed {feed} OK (latency: {latency:.2f}ms)")
                else:
                    logger.warning(f"⚠️ Data feed {feed} invalid response")

    async def _check_single_feed(self, feed: str, timeout: int) -> Tuple[float, bool]:
        start = datetime.now()
        await asyncio.sleep(0.1)  # Replace with actual check
        latency = (datetime.now() - start).total_seconds() * 1000
        return (latency, True)

    @healthcheck_handler()
    async def check_disk_usage(self) -> None:
        logger.info("🔍 Nurse: Checking disk usage...")
        partitions = self.config.nurse.monitored_partitions
        for part in partitions:
            usage = psutil.disk_usage(part)
            if usage.percent > self.config.nurse.resource_thresholds.disk:
                logger.warning(f"⚠️ Disk usage high on {part}: {usage.percent}%")
            SystemHealthMetrics.record_disk_usage(part, usage.percent)

    def __del__(self):
        if hasattr(self, 'observer'):
            self.observer.stop()
            self.observer.join()

