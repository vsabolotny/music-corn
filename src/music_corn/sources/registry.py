"""Plugin registry for source plugins."""

from typing import TypeVar

from music_corn.sources.base import SourcePlugin

T = TypeVar("T", bound=SourcePlugin)

_REGISTRY: dict[str, type[SourcePlugin]] = {}


def register_plugin(plugin_type: str):
    """Decorator to register a source plugin class."""

    def decorator(cls: type[T]) -> type[T]:
        cls.plugin_type = plugin_type
        _REGISTRY[plugin_type] = cls
        return cls

    return decorator


def get_plugin(plugin_type: str) -> SourcePlugin:
    """Get an instance of the plugin for the given type."""
    if plugin_type not in _REGISTRY:
        raise ValueError(f"Unknown plugin type: {plugin_type}. Available: {list(_REGISTRY.keys())}")
    return _REGISTRY[plugin_type]()


def list_plugins() -> list[str]:
    """List all registered plugin types."""
    return list(_REGISTRY.keys())
