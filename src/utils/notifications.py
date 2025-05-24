# src/utils/notifications.py

import logging
import asyncio
from abc import ABC, abstractmethod
from enum import Enum
from typing import (
    Any, 
    Dict, 
    List, 
    Optional, 
    Union,
    Callable,
    AsyncGenerator
)
from dataclasses import dataclass
from contextlib import asynccontextmanager

# Setup logging
logger = logging.getLogger(__name__)

class NotificationLevel(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"
    DEBUG = "debug"

class NotificationFormat(str, Enum):
    PLAIN_TEXT = "text"
    HTML = "html"
    MARKDOWN = "markdown"
    JSON = "json"

@dataclass
class NotificationMessage:
    content: str
    level: NotificationLevel = NotificationLevel.INFO
    format: NotificationFormat = NotificationFormat.PLAIN_TEXT
    metadata: Dict[str, Any] = None
    priority: int = 0

class NotificationProvider(ABC):
    """Base class for notification providers"""

    @abstractmethod
    async def send(self, message: NotificationMessage) -> bool:
        pass

    @abstractmethod
    def validate_configuration(self) -> bool:
        pass

class NotificationChannel(Enum):
    EMAIL = "email"
    SMS = "sms"
    WEBHOOK = "webhook"
    PUSH = "push"
    LOG = "log"
    CONSOLE = "console"

class NotificationManager:
    """Advanced notification system with multiple delivery channels and features"""

    def __init__(
        self,
        default_level: NotificationLevel = NotificationLevel.INFO,
        retry_attempts: int = 3,
        retry_delay: float = 1.0,
        queue_max_size: int = 1000,
        rate_limit: Optional[float] = None,
    ):
        self.providers: Dict[NotificationChannel, List[NotificationProvider]] = {}
        self.default_level = default_level
        self.retry_attempts = retry_attempts
        self.retry_delay = retry_delay
        self.message_queue = asyncio.Queue(maxsize=queue_max_size)
        self.rate_limit = rate_limit
        self._is_processing = False
        self._metrics: Dict[str, Any] = {
            'sent': 0,
            'failed': 0,
            'queued': 0,
        }

        # Add default console provider
        self.add_provider(NotificationChannel.CONSOLE, ConsoleNotificationProvider())

    def add_provider(
        self, 
        channel: NotificationChannel,
        provider: NotificationProvider,
        validate: bool = True
    ) -> None:
        """Add a notification provider for a specific channel"""
        if validate and not provider.validate_configuration():
            raise ValueError(f"Invalid configuration for {channel.value} provider")

        if channel not in self.providers:
            self.providers[channel] = []
        self.providers[channel].append(provider)
        logger.info(f"Added {provider.__class__.__name__} for {channel.value}")

    async def send(
        self,
        message: Union[str, NotificationMessage],
        channels: List[NotificationChannel] = None,
        level: NotificationLevel = None,
        format: NotificationFormat = None,
        immediate: bool = False,
        **metadata
    ) -> None:
        """Send a notification through specified channels"""
        if not isinstance(message, NotificationMessage):
            message = NotificationMessage(
                content=message,
                level=level or self.default_level,
                format=format or NotificationFormat.PLAIN_TEXT,
                metadata=metadata
            )

        if immediate:
            await self._send_immediately(message, channels)
        else:
            await self._enqueue_message(message, channels)

    async def _enqueue_message(
        self,
        message: NotificationMessage,
        channels: Optional[List[NotificationChannel]]
    ) -> None:
        """Add message to processing queue"""
        try:
            self.message_queue.put_nowait((message, channels))
            self._metrics['queued'] += 1
        except asyncio.QueueFull:
            logger.warning("Notification queue full, message dropped")
            self._metrics['failed'] += 1

    async def _send_immediately(
        self,
        message: NotificationMessage,
        channels: Optional[List[NotificationChannel]]
    ) -> None:
        """Send message immediately bypassing the queue"""
        await self._process_message(message, channels)

    async def start_processing(self) -> None:
        """Start background message processing"""
        if self._is_processing:
            return

        self._is_processing = True
        logger.info("Starting notification processing")
        while self._is_processing or not self.message_queue.empty():
            try:
                message, channels = await self.message_queue.get()
                await self._process_message(message, channels)
                self.message_queue.task_done()
            except Exception as e:
                logger.error(f"Error processing message: {str(e)}")
            finally:
                if self.rate_limit:
                    await asyncio.sleep(1 / self.rate_limit)

    async def stop_processing(self) -> None:
        """Stop background message processing"""
        self._is_processing = False
        logger.info("Stopping notification processing")

    async def _process_message(
        self,
        message: NotificationMessage,
        channels: Optional[List[NotificationChannel]]
    ) -> None:
        """Process a single message through specified channels"""
        channels = channels or list(self.providers.keys())

        for channel in channels:
            if channel not in self.providers:
                logger.warning(f"No providers configured for {channel.value}")
                continue

            for provider in self.providers[channel]:
                for attempt in range(self.retry_attempts):
                    try:
                        success = await provider.send(message)
                        if success:
                            self._metrics['sent'] += 1
                            break
                        else:
                            self._metrics['failed'] += 1
                    except Exception as e:
                        logger.error(f"Notification failed (attempt {attempt+1}): {str(e)}")
                        if attempt < self.retry_attempts - 1:
                            await asyncio.sleep(self.retry_delay * (attempt + 1))
                        else:
                            self._metrics['failed'] += 1

    def get_metrics(self) -> Dict[str, Any]:
        """Get current notification metrics"""
        return self._metrics.copy()

    @asynccontextmanager
    async def batch_mode(self) -> AsyncGenerator[Callable[[], None], None]:
        """Context manager for batch notifications"""
        original_immediate = self._is_processing
        self._is_processing = False

        try:
            yield lambda: self.start_processing()
        finally:
            self._is_processing = original_immediate

class ConsoleNotificationProvider(NotificationProvider):
    """Default console notification provider"""

    def validate_configuration(self) -> bool:
        return True

    async def send(self, message: NotificationMessage) -> bool:
        log_level = message.level.value.upper()
        logger.log(
            getattr(logging, log_level, logging.INFO),
            f"Console Notification: {message.content}",
            extra=message.metadata
        )
        return True

class EmailNotificationProvider(NotificationProvider):
    """Example email provider (implement with actual email service)"""

    def __init__(self, smtp_config: Dict[str, Any]):
        self.smtp_config = smtp_config

    def validate_configuration(self) -> bool:
        required = ['host', 'port', 'username', 'password']
        return all(key in self.smtp_config for key in required)

    async def send(self, message: NotificationMessage) -> bool:
        # Implement actual email sending logic
        logger.info(f"Would send email: {message.content}")
        return True

