import logging
import sys
from logging.handlers import RotatingFileHandler, SysLogHandler
from typing import Optional, Dict, Any, Union, IO
from enum import Enum
import json
import platform
from dataclasses import dataclass
from pathlib import Path

class LogFormat(str, Enum):
    SIMPLE = "simple"
    VERBOSE = "verbose"
    JSON = "json"

@dataclass
class LoggerConfiguration:
    name: str = "NANA1"
    level: int = logging.DEBUG
    enable_console: bool = True
    console_level: Optional[int] = None
    console_stream: Union[str, IO] = sys.stderr
    console_format: LogFormat = LogFormat.SIMPLE
    enable_file: bool = False
    file_level: Optional[int] = None
    log_file: Path = Path("app.log")
    max_bytes: int = 5 * 1024 * 1024  # 5 MB
    backup_count: int = 3
    file_format: LogFormat = LogFormat.VERBOSE
    enable_syslog: bool = False
    syslog_address: Optional[str] = None
    syslog_format: LogFormat = LogFormat.SIMPLE
    propagate: bool = False
    structured_data: Optional[Dict[str, Any]] = None
    colors: bool = sys.stderr.isatty()

def get_logger(config: LoggerConfiguration) -> logging.Logger:
    """Create and configure a logger with advanced features.
    
    Features:
    - Dual console/file logging with different formats
    - JSON structured logging support
    - Syslog integration
    - Colored console output
    - Environment-aware defaults
    - Thread-safe operations
    - Configurable rotation policy
    - Structured data support
    """
    
    logger = logging.getLogger(config.name)
    if logger.handlers:
        return logger  # Avoid duplicate handlers

    logger.setLevel(config.level)
    logger.propagate = config.propagate

    # Create formatters
    formatters = {
        LogFormat.SIMPLE: logging.Formatter(
            "%(levelname)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        ),
        LogFormat.VERBOSE: logging.Formatter(
            "[%(asctime)s] [%(levelname)s] %(name)s "
            "(%(filename)s:%(lineno)d): %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"
        ),
        LogFormat.JSON: _JSONFormatter(structured_data=config.structured_data)
    }

    # Console handler
    if config.enable_console:
        console_handler = logging.StreamHandler(config.console_stream)
        console_handler.setLevel(config.console_level or config.level)
        
        if config.colors:
            console_handler.setFormatter(_ColorFormatter(
                formatters[config.console_format]))
        else:
            console_handler.setFormatter(formatters[config.console_format])
            
        logger.addHandler(console_handler)

    # File handler
    if config.enable_file:
        try:
            file_handler = RotatingFileHandler(
                filename=config.log_file,
                maxBytes=config.max_bytes,
                backupCount=config.backup_count,
                encoding='utf-8',
                delay=True
            )
            file_handler.setLevel(config.file_level or config.level)
            file_handler.setFormatter(formatters[config.file_format])
            logger.addHandler(file_handler)
        except (PermissionError, OSError) as e:
            logger.error(f"Failed to create file handler: {e}", exc_info=True)

    # Syslog handler
    if config.enable_syslog:
        syslog_handler = SysLogHandler(
            address=config.syslog_address or _default_syslog_address(),
            facility='user'
        )
        syslog_handler.setFormatter(formatters[config.syslog_format])
        logger.addHandler(syslog_handler)

    return logger

class _JSONFormatter(logging.Formatter):
    def __init__(self, structured_data: Optional[Dict[str, Any]] = None):
        super().__init__()
        self.structured_data = structured_data or {}

    def format(self, record: logging.LogRecord) -> str:
        log_data = {
            "timestamp": datetime.now().isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "source": f"{record.pathname}:{record.lineno}",
            **self.structured_data
        }
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_data)

class _ColorFormatter(logging.Formatter):
    COLORS = {
        logging.DEBUG: '\033[94m',    # Blue
        logging.INFO: '\033[92m',     # Green
        logging.WARNING: '\033[93m',  # Yellow
        logging.ERROR: '\033[91m',    # Red
        logging.CRITICAL: '\033[95m'  # Magenta
    }
    RESET = '\033[0m'

    def __init__(self, formatter: logging.Formatter):
        super().__init__()
        self._formatter = formatter

    def format(self, record: logging.LogRecord) -> str:
        message = self._formatter.format(record)
        return f"{self.COLORS.get(record.levelno, '')}{message}{self.RESET}"

def _default_syslog_address() -> str:
    return "/dev/log" if platform.system() == "Linux" else ("localhost", 514)

# Usage example
if __name__ == "__main__":
    config = LoggerConfiguration(
        name="my_app",
        level=logging.DEBUG,
        console_format=LogFormat.SIMPLE,
        file_format=LogFormat.JSON,
        enable_file=True,
        structured_data={"app_version": "1.0.0"},
        colors=True
    )
    
    logger = get_logger(config)
    logger.info("Application initialized")
