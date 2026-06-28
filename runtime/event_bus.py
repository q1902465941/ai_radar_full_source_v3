from __future__ import annotations

import asyncio
import inspect
from collections import defaultdict
from collections.abc import Callable
from typing import Any


class EventBus:
    def __init__(self):
        self._handlers: dict[str, list[Callable[[Any], Any]]] = defaultdict(list)

    def on(self, event: str, handler: Callable[[Any], Any]) -> None:
        self._handlers[event].append(handler)

    def off(self, event: str, handler: Callable[[Any], Any]) -> None:
        handlers = self._handlers.get(event, [])
        self._handlers[event] = [candidate for candidate in handlers if candidate is not handler]

    def emit(self, event: str, payload: Any) -> None:
        for handler in list(self._handlers.get(event, [])):
            result = handler(payload)
            if inspect.isawaitable(result):
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    asyncio.run(result)
                else:
                    loop.create_task(result)

    async def emit_async(self, event: str, payload: Any) -> None:
        for handler in list(self._handlers.get(event, [])):
            result = handler(payload)
            if inspect.isawaitable(result):
                await result
