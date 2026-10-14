"""精简版事件引擎（参考 vnpy EventEngine，去掉定时器与节拍器）。

- 单个后台分发线程，保证同一事件总线上 handler 顺序执行
- 支持按事件类型注册 / 全局注册
- GUI 侧禁止直接在该线程操作 widget，必须通过 Qt Signal 桥接
  （见 mt5_trader/ui/base.py 的 EventBridge）
"""

from __future__ import annotations

import queue
import threading
import traceback
from typing import Any, Callable, NamedTuple


class Event(NamedTuple):
    type: str
    data: Any = None


Handler = Callable[[Event], None]


class EventEngine:
    def __init__(self) -> None:
        self._queue: "queue.Queue[Event]" = queue.Queue()
        self._active = False
        self._thread: threading.Thread | None = None
        self._handlers: dict[str, set[Handler]] = {}
        self._general_handlers: set[Handler] = set()

    # ------------------------------------------------------------------
    # 注册
    # ------------------------------------------------------------------
    def register(self, event_type: str, handler: Handler) -> Handler:
        self._handlers.setdefault(event_type, set()).add(handler)
        return handler

    def unregister(self, event_type: str, handler: Handler) -> None:
        handlers = self._handlers.get(event_type)
        if handlers is None:
            return
        handlers.discard(handler)
        if not handlers:
            del self._handlers[event_type]

    def register_general(self, handler: Handler) -> Handler:
        self._general_handlers.add(handler)
        return handler

    def unregister_general(self, handler: Handler) -> None:
        self._general_handlers.discard(handler)

    # ------------------------------------------------------------------
    # 发布与生命周期
    # ------------------------------------------------------------------
    def put(self, event: Event) -> None:
        self._queue.put(event)

    def start(self) -> None:
        if self._active:
            return
        self._active = True
        self._thread = threading.Thread(
            target=self._run, name="EventEngine", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._active = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._thread = None

    @property
    def active(self) -> bool:
        return self._active

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _run(self) -> None:
        while self._active:
            try:
                event = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            self._dispatch(event)

    def _dispatch(self, event: Event) -> None:
        for handler in list(self._handlers.get(event.type, ())):
            self._call(handler, event)
        for handler in list(self._general_handlers):
            self._call(handler, event)

    @staticmethod
    def _call(handler: Handler, event: Event) -> None:
        try:
            handler(event)
        except Exception:  # handler 异常不允许击穿分发线程
            traceback.print_exc()
