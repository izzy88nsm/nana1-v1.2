import asyncio
import gzip
import json
import logging
import threading
from collections import deque
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Union, Deque, Callable, Set
from pydantic import BaseModel, Field, field_validator, ValidationError

logger = logging.getLogger(__name__)

class EntryType(str, Enum):
    TRADE = "TRADE"
    ERROR = "ERROR"
    STRATEGY_UPDATE = "STRATEGY_UPDATE"
    STATE_ERROR = "STATE_ERROR"
    SYSTEM = "SYSTEM"
    HEALTH_WARNING = "HEALTH_WARNING"

class AuditEntry(BaseModel):
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    entry_type: EntryType
    message: str
    details: Dict[str, Any] = Field(default_factory=dict)
    tags: Set[str] = Field(default_factory=set)
    priority: int = Field(default=0, ge=0, le=10)

    @field_validator('details')
    def validate_details(cls, v):
        if 'password' in v or 'secret' in v:
            raise ValueError("Sensitive fields detected in details")
        return v

    def model_dump_json(self, **kwargs) -> str:
        return json.dumps({
            **super().model_dump(**kwargs),
            'timestamp': self.timestamp.isoformat()
        }, ensure_ascii=False)

class JournalConfig(BaseModel):
    max_file_size: int = 10 * 1024 * 1024  # 10MB
    rotation_interval: Optional[timedelta] = timedelta(days=1)
    backup_count: int = 30
    compress_backups: bool = True
    cache_size: int = 2000
    write_batch_size: int = 50
    write_interval: float = 1.0  # seconds

class TradeJournal:
    def __init__(
        self,
        name: str,
        log_dir: Union[str, Path] = "data/journal",
        config: JournalConfig = JournalConfig()
    ):
        self.name = name
        self.config = config
        self._base_path = Path(log_dir)
        self._current_file: Optional[Path] = None
        self._current_size = 0
        self._rotation_lock = threading.Lock()
        self._write_queue: Deque[AuditEntry] = deque(maxlen=config.cache_size)
        self._active = True
        self._last_rotation = datetime.now()
        
        self._setup_paths()
        self._start_background_writer()
        
        self._before_write_hooks: List[Callable[[AuditEntry], None]] = []
        self._after_write_hooks: List[Callable[[AuditEntry], None]] = []

    def _setup_paths(self):
        self._base_path.mkdir(parents=True, exist_ok=True)
        self._current_file = self._get_current_file_path()

    def _get_current_file_path(self) -> Path:
        return self._base_path / f"{self.name}_audit.jsonl"

    def _start_background_writer(self):
        def writer_loop():
            while self._active:
                self._process_write_queue()
                time.sleep(self.config.write_interval)

        self._writer_thread = threading.Thread(
            target=writer_loop, 
            daemon=True,
            name=f"JournalWriter-{self.name}"
        )
        self._writer_thread.start()

    def _process_write_queue(self):
        batch = []
        while len(batch) < self.config.write_batch_size and self._write_queue:
            batch.append(self._write_queue.popleft())

        if batch:
            try:
                self._write_batch(batch)
            except Exception as e:
                logger.error(f"Batch write failed: {e}")
                self._write_queue.extendleft(reversed(batch))

    def _write_batch(self, entries: List[AuditEntry]):
        with self._rotation_lock:
            self._check_rotation()
            try:
                entries_json = [e.model_dump_json() + "\n" for e in entries]
                content = "".join(entries_json)
                
                with open(self._current_file, "a", encoding="utf-8") as f:
                    f.write(content)
                
                self._current_size += len(content)
                for entry in entries:
                    self._trigger_hooks(self._after_write_hooks, entry)
                
            except Exception as e:
                logger.error(f"Critical write failure: {e}")
                self._handle_write_failure(entries)

    def _check_rotation(self):
        now = datetime.now()
        rotate = False

        if self._current_size > self.config.max_file_size:
            rotate = True
            reason = "size"
        elif (self.config.rotation_interval and 
              (now - self._last_rotation) > self.config.rotation_interval):
            rotate = True
            reason = "time"

        if rotate:
            self._rotate_file(reason)
            self._last_rotation = now
            self._current_size = 0

    def _rotate_file(self, reason: str):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        rotated_path = self._current_file.with_name(
            f"{self.name}_audit_{reason}_{timestamp}.jsonl"
        )
        
        self._current_file.rename(rotated_path)
        
        if self.config.compress_backups:
            with open(rotated_path, "rb") as f_in:
                with gzip.open(f"{rotated_path}.gz", "wb") as f_out:
                    f_out.writelines(f_in)
            rotated_path.unlink()
            rotated_path = rotated_path.with_suffix(".jsonl.gz")

        self._current_file = self._get_current_file_path()

    def log(self, entry: AuditEntry):
        """Thread-safe logging with validation and hooks"""
        if not self._active:
            raise RuntimeError("Journal is closed")

        try:
            self._trigger_hooks(self._before_write_hooks, entry)
            self._validate_entry(entry)
            self._write_queue.append(entry)
        except ValidationError as ve:
            logger.error(f"Invalid log entry: {ve}")
        except Exception as e:
            logger.error(f"Logging failed: {e}")

    def _validate_entry(self, entry: AuditEntry):
        if entry.entry_type == EntryType.ERROR and not entry.details.get("stack_trace"):
            logger.warning("Error entry missing stack trace")

    def _trigger_hooks(self, hooks: List[Callable[[AuditEntry], None]], entry: AuditEntry):
        for hook in hooks:
            try:
                hook(entry)
            except Exception as e:
                logger.error(f"Hook {hook.__name__} failed: {e}")

    def register_hook(self, hook_type: str, hook: Callable[[AuditEntry], None]):
        if hook_type == "before_write":
            self._before_write_hooks.append(hook)
        elif hook_type == "after_write":
            self._after_write_hooks.append(hook)
        else:
            raise ValueError(f"Invalid hook type: {hook_type}")

    async def emergency_flush(self):
        """Immediate write of all pending entries"""
        with self._rotation_lock:
            entries = list(self._write_queue)
            self._write_queue.clear()
            if entries:
                self._write_batch(entries)

    def search(
        self,
        entry_type: Optional[EntryType] = None,
        min_priority: int = 0,
        tags: Optional[Set[str]] = None,
        start_time: Optional[datetime] = None,
        end_time: Optional[datetime] = None
    ) -> List[AuditEntry]:
        results = []
        for entry in self._write_queue:
            if (entry_type and entry.entry_type != entry_type):
                continue
            if entry.priority < min_priority:
                continue
            if tags and not tags.issubset(entry.tags):
                continue
            if start_time and entry.timestamp < start_time:
                continue
            if end_time and entry.timestamp > end_time:
                continue
            results.append(entry)
        return results

    def close(self):
        """Graceful shutdown"""
        self._active = False
        self._writer_thread.join()
        asyncio.run(self.emergency_flush())

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def _handle_write_failure(self, failed_entries: List[AuditEntry]):
        emergency_path = self._base_path / f"{self.name}_emergency.log"
        try:
            with open(emergency_path, "a") as f:
                for entry in failed_entries:
                    f.write(f"{entry.timestamp.isoformat()} - {entry.message}\n")
            logger.critical(f"Emergency entries saved to {emergency_path}")
        except Exception as e:
            logger.critical(f"Failed emergency save: {e}")
