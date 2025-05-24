# Advanced logging configuration utility with structured and JSON support

import logging
import sys
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from typing import Optional, Dict, Any, Union
from enum import Enum

try:
    from pythonjsonlogger import jsonlogger
except ImportError:
    jsonlogger = None


class LogFormat(str, Enum):
    SIMPLE = "simple"
    JSON = "json"
    STRUCTURED = "structured"


@dataclass
class ConsoleConfig:
    enabled: bool = True
    level: Optional[int] = None
    format: LogFormat = LogFormat.SIMPLE
    formatter: Optional[logging.Formatter] = None
    stream: str = "stderr"


@dataclass
class FileConfig:
    enabled: bool = False
    level: Optional[int] = None
    format: LogFormat = LogFormat.SIMPLE
    formatter: Optional[logging.Formatter] = None
    filename: str = "app.log"
    mode: str = "a"
    max_bytes: Optional[int] = None
    backup_count: int = 5
    encoding: str = "utf-8"
    delay: bool = False


def get_logger(
    name: str = __name__,
    level: int = logging.DEBUG,
    console: ConsoleConfig = ConsoleConfig(),
    file: FileConfig = FileConfig(),
    propagate: bool = False,
) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.propagate = propagate
    logger.setLevel(level)

    _configure_console_handler(logger, console, level)
    _configure_file_handler(logger, file, level)

    return logger


def _configure_console_handler(
    logger: logging.Logger,
    config: ConsoleConfig,
    base_level: int
) -> None:
    if not config.enabled:
        return

    stream = sys.stderr if config.stream == "stderr" else sys.stdout
    handler = _find_existing_handler(logger, logging.StreamHandler, stream)

    if handler:
        return

    handler = logging.StreamHandler(stream)
    formatter = config.formatter or _create_formatter(config.format, "console")

    handler.setFormatter(formatter)
    handler.setLevel(config.level or base_level)
    logger.addHandler(handler)


def _configure_file_handler(
    logger: logging.Logger,
    config: FileConfig,
    base_level: int
) -> None:
    if not config.enabled:
        return

    handler_class = RotatingFileHandler if config.max_bytes else logging.FileHandler
    handler = _find_existing_handler(logger, handler_class, config.filename)

    if handler:
        return

    handler_args = {
        'filename': config.filename,
        'mode': config.mode,
        'encoding': config.encoding,
        'delay': config.delay
    }

    if config.max_bytes:
        handler_args.update({
            'maxBytes': config.max_bytes,
            'backupCount': config.backup_count
        })

    handler = handler_class(**handler_args)
    formatter = config.formatter or _create_formatter(config.format, "file")

    handler.setFormatter(formatter)
    handler.setLevel(config.level or base_level)
    logger.addHandler(handler)


def _find_existing_handler(
    logger: logging.Logger,
    handler_type: type,
    identifier: Union[str, Any]
) -> Optional[logging.Handler]:
    for handler in logger.handlers:
        if isinstance(handler, handler_type):
            if (isinstance(handler, logging.FileHandler) and
                handler.baseFilename == str(identifier)):
                return handler
            elif (isinstance(handler, logging.StreamHandler) and
                  handler.stream == identifier):
                return handler
    return None


def _create_formatter(format_type: LogFormat, handler_type: str) -> logging.Formatter:
    datefmt = "%Y-%m-%dT%H:%M:%S"
    format_specs: Dict[str, Dict[str, str]] = {
        "console": {
            LogFormat.SIMPLE: (
                "[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
                "%Y-%m-%d %H:%M:%S"
            ),
            LogFormat.STRUCTURED: (
                "%(asctime)s | %(levelname)s | %(name)s | %(filename)s:%(lineno)d | %(message)s",
                datefmt
            ),
            LogFormat.JSON: (
                None,
                None
            )
        },
        "file": {
            LogFormat.SIMPLE: (
                "[%(asctime)s] [%(levelname)s] %(name)s [%(filename)s:%(lineno)d] - %(message)s",
                "%Y-%m-%d %H:%M:%S"
            ),
            LogFormat.STRUCTURED: (
                "%(asctime)s | %(levelname)s | %(name)s | %(filename)s:%(lineno)d | %(message)s",
                datefmt
            ),
            LogFormat.JSON: (
                None,
                None
            )
        }
    }

    if format_type == LogFormat.JSON:
        if jsonlogger is None:
            raise ImportError("python-json-logger is required for JSON formatting. "
                              "Install with 'pip install python-json-logger'")
        return jsonlogger.JsonFormatter(
            fmt="%(asctime)s %(levelname)s %(name)s %(filename)s %(lineno)d %(message)s",
            datefmt=datefmt,
            rename_fields={"levelname": "level", "filename": "file", "lineno": "line"}
        )

    fmt, datefmt = format_specs[handler_type][format_type]
    return logging.Formatter(fmt=fmt, datefmt=datefmt)

