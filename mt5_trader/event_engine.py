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

from .event_queue import EventQueue


class Event(NamedTuple):
    type: str
    data: Any = None


Handler = Callable[[Event], None]


class EventEngine:
    def __init__(self) -> None:
        # 有界队列（maxsize=5000）：防止慢消费者场景下内存无界增长。
        self._queue: EventQueue = EventQueue(maxsize=5000)
        # Snapshots coalesce immediately; critical events retain backpressure.
        self._lock = threading.RLock()
        self._active = False
        self._thread: threading.Thread | None = None
        self._handlers: dict[str, set[Handler]] = {}
        self._general_handlers: set[Handler] = set()

    # ------------------------------------------------------------------
    # 注册
    # ------------------------------------------------------------------
    def register(self, event_type: str, handler: Handler) -> Handler:
        with self._lock:
            self._handlers.setdefault(event_type, set()).add(handler)
        return handler

    def unregister(self, event_type: str, handler: Handler) -> None:
        with self._lock:
            handlers = self._handlers.get(event_type)
            if handlers is not None:
                handlers.discard(handler)
                if not handlers:
                    self._handlers.pop(event_type, None)

    def register_general(self, handler: Handler) -> Handler:
        with self._lock:
            self._general_handlers.add(handler)
        return handler

    def unregister_general(self, handler: Handler) -> None:
        with self._lock:
            self._general_handlers.discard(handler)

    # ------------------------------------------------------------------
    # 发布与生命周期
    # ------------------------------------------------------------------
    def put(self, event: Event) -> None:
        # Reentrant handlers must not wait on their own dispatch thread.
        # They publish synchronously only at saturation, preserving critical
        # events without a queue deadlock. Normal producers apply backpressure.
        if threading.current_thread() is self._thread:
            try:
                self._queue.put_nowait(event)
            except queue.Full:
                self._dispatch(event)
        else:
            self._queue.put(event)

    @property
    def dropped(self) -> int:
        return self._queue.dropped

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
        if self._thread and self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join()
        self._thread = None

    @property
    def active(self) -> bool:
        return self._active

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _run(self) -> None:
        while self._active or not self._queue.empty():
            try:
                event = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self._dispatch(event)
            finally:
                self._queue.task_done()

    def _dispatch(self, event: Event) -> None:
        with self._lock:
            handlers = tuple(self._handlers.get(event.type, ()))
            general = tuple(self._general_handlers)
        for handler in handlers:
            self._call(handler, event)
        for handler in general:
            self._call(handler, event)

    @staticmethod
    def _call(handler: Handler, event: Event) -> None:
        try:
            handler(event)
        except Exception:  # handler 异常不允许击穿分发线程
            traceback.print_exc()
