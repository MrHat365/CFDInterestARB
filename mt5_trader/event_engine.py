"""精简版事件引擎（参考 vnpy EventEngine，去掉定时器与节拍器）。

- 单个后台分发线程，保证同一事件总线上 handler 顺序执行
- 支持按事件类型注册 / 全局注册
- GUI 侧禁止直接在该线程操作 widget，必须通过 Qt Signal 桥接
  （见 mt5_trader/ui/base.py 的 EventBridge）
"""

from __future__ import annotations

import queue
import sys
import threading
import traceback
from typing import Any, Callable, NamedTuple


class Event(NamedTuple):
    type: str
    data: Any = None


Handler = Callable[[Event], None]


class EventEngine:
    def __init__(self) -> None:
        # 有界队列（maxsize=5000）：防止慢消费者场景下内存无界增长。
        self._queue: "queue.Queue[Event]" = queue.Queue(maxsize=5000)
        # 满载丢最旧事件的累计计数；每丢满 50 个向 stderr 打一行告警。
        self.dropped = 0
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
        # 队列满时丢最旧保最新（后台分发线程 concurrent get 会并发取走事件，
        # get_nowait 可能撞 Empty；简单 try/except 兜底即可，不做双重加锁）。
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            try:
                self._queue.get_nowait()   # 丢弃最旧事件
            except queue.Empty:
                pass
            self.dropped += 1
            if self.dropped % 50 == 0:
                print(
                    f"[EventEngine] queue full, dropped total={self.dropped}",
                    file=sys.stderr,
                )
            try:
                self._queue.put_nowait(event)
            except queue.Full:
                # 并发消费者瞬间取空又满的极端竞态：放弃本条（仍计入 dropped）。
                self.dropped += 1

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
