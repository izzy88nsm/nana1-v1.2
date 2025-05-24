import importlib
import inspect
import logging
from types import ModuleType
from typing import Any, Dict, List, Optional, Type, TypeVar

logger = logging.getLogger(__name__)

DEFAULT_STRATEGY_CLASS = "AIStrategy"
DEFAULT_MODULE_PATH = "strategies.ai_strategy"

class StrategyReloadError(Exception):
    """Raised when strategy reload or initialization fails"""

T = TypeVar('T')

class StrategyLoader:
    """
    Dynamically loads strategy classes with hot-swap support and validation.

    Example config:
    {
        "type": "strategies.momentum_strategy.MomentumStrategy",
        "parameters": { "window": 5, "threshold": 0.2 }
    }

    :param config: Strategy configuration dictionary
    :param strategy_base: Optional base class for strategy validation
    :param required_methods: List of method names the strategy must implement
    """

    def __init__(
        self,
        config: Dict[str, Any],
        strategy_base: Optional[Type[T]] = None,
        required_methods: Optional[List[str]] = None
    ):
        self._original_config = config
        self._current_class: Optional[Type[T]] = None
        self._current_instance: Optional[T] = None
        self._strategy_base = strategy_base
        self._required_methods = required_methods or []

    def load(self, reload_module: bool = False, **kwargs) -> T:
        """Load and instantiate strategy from config with optional hot-reload"""
        try:
            class_ref = self._resolve_class(self._original_config, reload_module)
            self._validate_strategy_class(class_ref)
            
            parameters = self._original_config.get("parameters", {})
            if not isinstance(parameters, dict):
                raise StrategyReloadError(
                    f"Parameters must be a dictionary, got {type(parameters).__name__}"
                )

            # Merge configuration parameters with additional dependencies
            init_kwargs = {"config": parameters}
            init_kwargs.update(kwargs)

            instance = class_ref(**init_kwargs)
            self._current_instance = instance
            self._current_class = class_ref
            logger.info(f"Successfully loaded strategy: {self.describe()}")
            return instance

        except Exception as e:
            logger.error(
                f"Strategy load failed for {self._original_config.get('type', 'unknown')}: {str(e)}",
                exc_info=True
            )
            raise StrategyReloadError(str(e)) from e

    def _resolve_class(self, config: Dict[str, Any], reload_module: bool) -> Type[T]:
        """Resolve strategy class from config string with optional module reload"""
        class_path = config.get("type", f"{DEFAULT_MODULE_PATH}.{DEFAULT_STRATEGY_CLASS}")

        if not isinstance(class_path, str):
            raise StrategyReloadError("Strategy type must be a string path")

        try:
            module_path, class_name = class_path.rsplit(".", 1)
        except ValueError as e:
            raise StrategyReloadError(
                f"Invalid class path '{class_path}'. Expected format: 'module.path.ClassName'"
            ) from e

        try:
            module = importlib.import_module(module_path)
            if reload_module:
                module = importlib.reload(module)
            class_ref = getattr(module, class_name)
            return class_ref
        except (ImportError, AttributeError) as e:
            raise StrategyReloadError(
                f"Could not load strategy '{class_path}': {str(e)}"
            ) from e

    def _validate_strategy_class(self, class_ref: Type[T]) -> None:
        """Validate that the loaded class meets requirements"""
        if not inspect.isclass(class_ref):
            raise StrategyReloadError(f"Invalid strategy type: {class_ref} is not a class")

        if self._strategy_base and not issubclass(class_ref, self._strategy_base):
            raise StrategyReloadError(
                f"Strategy {class_ref.__name__} must inherit from {self._strategy_base.__name__}"
            )

        for method in self._required_methods:
            if not hasattr(class_ref, method):
                raise StrategyReloadError(
                    f"Strategy class {class_ref.__name__} missing required method: {method}"
                )

    def update_config(self, new_config: Dict[str, Any]) -> None:
        """Update configuration and reset internal state"""
        self._original_config = new_config
        self._current_instance = None
        self._current_class = None
        logger.debug("Configuration updated, previous strategy instance cleared")

    def describe(self) -> str:
        """Return descriptive string about currently loaded strategy"""
        if not self._current_class:
            return "No strategy currently loaded"
        return (
            f"{self._current_class.__module__}.{self._current_class.__name__} "
            f"(parameters: {self._original_config.get('parameters', {})})"
        )

    @property
    def current_instance(self) -> Optional[T]:
        """Get currently loaded strategy instance"""
        return self._current_instance
