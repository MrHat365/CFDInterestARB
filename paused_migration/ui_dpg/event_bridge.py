"""事件总线 → DPG 主线程 桥。

旧 Qt 版靠 QtCore.Signal 的 QueuedConnection 把后台线程切换到主线程；
DPG 无这类机制，改为：所有跨线程事件统一进入 queue.Queue，
主循环每帧调 drain()（在 DPG 主线程内）逐条派发到注册的 handler 表。

- EventEngine 事件（eSessionStatus/eLog/…）：构造时按类型 subscribe（避免把
  高频 eTick 也压进来）；
- 本地线程事件（终端扫描线程）：submit(event_type, data) 走同一个 queue ——
  线程→UI 只有一条通路（DRY）。

app_dpg 主循环形态：
    while dpg.is_dearpygui_running():
        bridge.drain()                      # 每帧排空
        dpg.render_dearpygui_frame()
"""

from __future__ import annotations

import queue
import traceback
from typing import Any, Callable

from ..event_engine import Event, EventEngine

Handler = Callable[[Event], None]


class DpgEventBridge:
    def __init__(self, engine: EventEngine | None = None, subscribe: tuple[str, ...] = ()) -> None:
        self._queue: "queue.Queue[Event]" = queue.Queue()
        self._handlers: dict[str, list[Handler]] = {}
        self._engine = engine
        self._forward_handlers: list[Handler] = []   # EventEngine 侧的转发句柄
        if engine is not None:
            self.attach_engine(engine, subscribe=subscribe)

    # ------------------------------------------------------------------
    # 注册
    # ------------------------------------------------------------------
    def attach_engine(self, engine: EventEngine, subscribe: tuple[str, ...] = ()) -> None:
        self._engine = engine
        for event_type in subscribe:
            self._forward_handlers.append(engine.register(event_type, self._forward_from_engine))

    def _forward_from_engine(self, event: Event) -> None:
        self._queue.put(event)

    def register(self, event_type: str, handler: Handler) -> Handler:
        self._handlers.setdefault(event_type, []).append(handler)
        return handler

    def unregister(self, event_type: str, handler: Handler) -> None:
        handlers = self._handlers.get(event_type)
        if handlers:
            handlers.remove(handler)

    # ------------------------------------------------------------------
    # 本地线程 → UI（扫描线程等使用；主线程回调里也可直接发给自己）
    # ------------------------------------------------------------------
    def submit(self, event_type: str, data: Any) -> None:
        self._queue.put(Event(event_type, data))

    # ------------------------------------------------------------------
    # 每帧排空（必须在 DPG 主线程调用）
    # ------------------------------------------------------------------
    def drain(self) -> int:
        count = 0
        while True:
            try:
                event = self._queue.get_nowait()
            except queue.Empty:
                break
            self._dispatch(event)
            count += 1
        return count

    def _dispatch(self, event: Event) -> None:
        for handler in list(self._handlers.get(event.type, ())):
            try:
                handler(event)
            except Exception:   # 单个 handler 异常不允许击穿主循环
                traceback.print_exc()
