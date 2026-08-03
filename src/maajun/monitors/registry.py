from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from maajun.monitors.base import Monitor


class MonitorRegistry:
    _registry: dict[str, type[Monitor]] = {}

    @classmethod
    def register(cls, name: str):
        def wrapper(monitor_cls: type[Monitor]) -> type[Monitor]:
            cls._registry[name] = monitor_cls
            return monitor_cls
        return wrapper

    @classmethod
    def create(cls, name: str, **kwargs) -> Monitor:
        if name not in cls._registry:
            raise ValueError(
                f"Unknown monitor type: '{name}'. "
                f"Known types: {', '.join(cls._registry)}"
            )
        return cls._registry[name](**kwargs)

    @classmethod
    def known_types(cls) -> list[str]:
        return list(cls._registry)
