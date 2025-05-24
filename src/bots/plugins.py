import asyncio
import inspect
import logging
from typing import Any, Callable, Dict, List, Optional, Union
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# ---------- Context Object for Plugin Hooks ---------- #
@dataclass
class PluginContext:
    bot: Any
    data: Optional[Any] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

# ---------- Plugin Manager Core ---------- #
class PluginManager:
    def __init__(self):
        self._hooks: Dict[str, List[Callable]] = {}
        self._filters: Dict[str, List[Callable]] = {}

    # ---------- Hook Registration ---------- #
    def register(self, hook_name: str, fn: Callable, prepend: bool = False):
        """Register a plugin hook"""
        if hook_name not in self._hooks:
            self._hooks[hook_name] = []
        if prepend:
            self._hooks[hook_name].insert(0, fn)
        else:
            self._hooks[hook_name].append(fn)

    def register_filter(self, filter_name: str, fn: Callable, prepend: bool = False):
        """Register a plugin filter"""
        if filter_name not in self._filters:
            self._filters[filter_name] = []
        if prepend:
            self._filters[filter_name].insert(0, fn)
        else:
            self._filters[filter_name].append(fn)

    # ---------- Hook Execution ---------- #
    async def apply_chain(self, hook: str, value: Any, context: Optional[PluginContext] = None) -> Any:
        """Apply a series of plugin hooks to a value"""
        for fn in self._hooks.get(hook, []):
            try:
                value = await self._call(fn, value, context)
            except Exception as e:
                logger.warning(f"Plugin error in hook '{hook}': {str(e)}", exc_info=True)
        return value

    async def apply_filter(self, name: str, value: Any, **kwargs) -> Any:
        """Apply registered filters for validation or transformation"""
        for fn in self._filters.get(name, []):
            try:
                # ✅ Corrected: unpack keyword arguments
                value = await self._call(fn, value, **kwargs)
            except Exception as e:
                logger.warning(f"Plugin error in filter '{name}': {str(e)}", exc_info=True)
        return value

    async def _call(self, fn: Callable, *args, **kwargs) -> Any:
        if inspect.iscoroutinefunction(fn):
            return await fn(*args, **kwargs)
        return fn(*args, **kwargs)

    async def cleanup(self):
        """Call any cleanup hooks (shutdown, cache flush, etc)"""
        for hook in self._hooks.get("cleanup", []):
            try:
                await self._call(hook)
            except Exception as e:
                logger.error(f"Plugin cleanup error: {str(e)}", exc_info=True)

    def should_skip(self, data: Any) -> bool:
        """Check if execution should be skipped (e.g., filtered out by a plugin)"""
        for fn in self._filters.get("skip_execution", []):
            try:
                if fn(data):
                    return True
            except Exception as e:
                logger.warning(f"Plugin skip check failed: {str(e)}", exc_info=True)
        return False

