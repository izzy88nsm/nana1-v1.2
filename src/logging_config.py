import logging
import sys
from logging.handlers import RotatingFileHandler
from typing import Optional
from enum import Enum


class LogFormat(str, Enum):
    SIMPLE = "simple"
    JSON = "json"
    STRUCTURED = "structured"


def get_logger(
    name: str = "NANA1",
    level: int = logging.DEBUG,
    enable_console: bool = True,
    console_level: Optional[int] = None,
    console_formatter: Optional[logging.Formatter] = None,
    console_stream: str = "stderr",
    enable_file: bool = False,
    file_level: Optional[int] = None,
    file_formatter: Optional[logging.Formatter] = None,
    log_file: str = "app.log",
    file_mode: str = "a",
    max_bytes: Optional[int] = None,
    backup_count: int = 5,
    encoding: str = "utf-8",
    delay: bool = False,
    propagate: bool = False,
    console_format: Optional[LogFormat] = LogFormat.SIMPLE,
    file_format: Optional[LogFormat] = LogFormat.SIMPLE,
) -> logging.Logger:
    """
    Enhanced logger setup with console and file logging capabilities.

    Args:
        name: Logger name (default: "NANA1")
        level: Base logging level (default: DEBUG)
        enable_console: Enable console logging (default: True)
        console_level: Console handler level (default: None, uses logger level)
        console_formatter: Custom console formatter
        console_stream: Console output stream ("stderr" or "stdout") (default: "stderr")
        enable_file: Enable file logging (default: False)
        file_level: File handler level (default: None, uses logger level)
        file_formatter: Custom file formatter
        log_file: Log file path (default: "app.log")
        file_mode: File mode (default: "a")
        max_bytes: Max file size for rotation (default: None - no rotation)
        backup_count: Number of backup files (default: 5)
        encoding: File encoding (default: "utf-8")
        delay: Delay file opening until first write (default: False)
        propagate: Propagate logs to ancestor loggers (default: False)
        console_format: LogFormat enum for console logs
        file_format: LogFormat enum for file logs

    Returns:
        Configured logger instance
    """
    logger = logging.getLogger(name)
    logger.propagate = propagate
    logger.setLevel(level)

    existing_handlers = {type(h) for h in logger.handlers}

    # Console handler
    if enable_console and logging.StreamHandler not in existing_handlers:
        stream = sys.stderr if console_stream == "stderr" else sys.stdout
        console_handler = logging.StreamHandler(stream)

        if not console_formatter:
            if console_format == LogFormat.STRUCTURED:
                console_formatter = logging.Formatter(
                    fmt="%(asctime)s | %(levelname)s | %(name)s | %(filename)s:%(lineno)d | %(message)s",
                    datefmt="%Y-%m-%dT%H:%M:%S"
                )
            else:
                console_formatter = logging.Formatter(
                    "[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S"
                )

        console_handler.setFormatter(console_formatter)
        console_handler.setLevel(console_level or level)
        logger.addHandler(console_handler)
        existing_handlers.add(type(console_handler))

    # File handler
    if enable_file:
        if max_bytes:
            file_handler = RotatingFileHandler(
                log_file,
                maxBytes=max_bytes,
                backupCount=backup_count,
                encoding=encoding,
                delay=delay
            )
        else:
            file_handler = logging.FileHandler(
                log_file,
                mode=file_mode,
                encoding=encoding,
                delay=delay
            )

        if not any(isinstance(h, type(file_handler)) for h in logger.handlers):
            if not file_formatter:
                if file_format == LogFormat.STRUCTURED:
                    file_formatter = logging.Formatter(
                        fmt="%(asctime)s | %(levelname)s | %(name)s | %(filename)s:%(lineno)d | %(message)s",
                        datefmt="%Y-%m-%dT%H:%M:%S"
                    )
                else:
                    file_formatter = logging.Formatter(
                        "[%(asctime)s] [%(levelname)s] %(name)s "
                        "[%(filename)s:%(lineno)d] - %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S"
                    )
            file_handler.setFormatter(file_formatter)
            file_handler.setLevel(file_level or level)
            logger.addHandler(file_handler)

    handler_levels = [h.level for h in logger.handlers]
    if handler_levels:
        logger.setLevel(min([level] + handler_levels))

    return logger

