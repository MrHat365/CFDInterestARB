"""UI 基础件：事件总线 -> Qt 主线程 的信号桥接。"""

from __future__ import annotations

from PySide6 import QtCore

from ..event_engine import Event, EventEngine


class EventBridge(QtCore.QObject):
    """把 EventEngine 的回调（后台线程）转发为 Qt Signal。

    Qt 跨线程 emit 自动走 QueuedConnection，槽函数在主线程执行，
    因此 widget 的更新代码可以放心写在槽函数里。
    """

    event_received = QtCore.Signal(object)

    def __init__(self, event_engine: EventEngine, event_type: str) -> None:
        super().__init__()
        self.event_type = event_type
        self._engine = event_engine
        self._alive = True
        self._handler = event_engine.register(event_type, self._on_event)

    def destroy(self) -> None:
        """事件引擎侧反注册 + 关停发信（窗口销毁后仍投递会触碰已删 Signal）。"""
        self._alive = False
        try:
            self._engine.unregister(self.event_type, self._on_event)
        except Exception:
            pass

    def _on_event(self, event: Event) -> None:
        if not self._alive:
            return
        try:
            self.event_received.emit(event)
        except RuntimeError:
            self._alive = False   # C++ 对象已删，停止发信


from ..event_engine import Event
