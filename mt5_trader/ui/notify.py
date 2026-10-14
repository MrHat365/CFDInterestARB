"""Toast 浮层通知：订单结果 / 错误日志的右下角弹窗提示。

- Toast: 单条弹窗（标题 + 可选正文 + 级别配色 + 可点击 + 淡入淡出）。
- ToastManager: 挂在父窗口右下角的浮层容器，负责堆叠（最多 3 条）、
  超时自动淡出销毁，以及通过事件总线订阅 eOrderResult / eLog。

事件桥接：EventEngine 的回调运行在其后台分发线程（见 mt5_trader/event_engine.py），
GUI 侧禁止在该线程直接操作 widget，必须经 Qt Signal 转发（同 ui/base.py 的
EventBridge 模式，此处自建 Signal 以便重复 hook 时先反注册旧 handler）。

RETCODE_NAME 未收录的 retcode 一律兜底为 "未知 retcode NNNNN"（NNNNN 为
补零后的五位数字），保证任何 retcode 都能渲染出中文文案。
"""

from __future__ import annotations

import re
from typing import Any, Callable

from PySide6 import QtCore, QtWidgets

from ..constants import EVENT_LOG, EVENT_ORDER_RESULT
from ..event_engine import Event

# MT5 常见交易返回码 -> 中文说明（未知码由 ToastManager 兜底为 "未知 retcode NNNNN"）
RETCODE_NAME: dict[int, str] = {
    10004: "重新报价/需重试",
    10006: "请求被拒绝",
    10009: "已完成",
    10013: "无效请求",
    10014: "无效手数",
    10015: "无效价格",
    10016: "无效止损止盈",
    10018: "市场关闭",
    10019: "资金不足",
    10020: "价格已变化",
    10021: "无有效报价",
    10022: "无效到期日",
    10023: "订单状态不允许",
    10024: "无连接",
    10025: "仅允许平仓",
    10026: "交易被禁用",
    10027: "订单数超限",
    10028: "多头禁用",
    10029: "空头禁用",
    10030: "单品种持仓超限",
    10031: "平仓量超持仓",
    10032: "关闭全部失败",
    10033: "已全部平仓",
}

# 级别 -> (底色, 边框/标题色)
_LEVELS: dict[str, tuple[str, str]] = {
    "up": ("#16241c", "#16c784"),
    "down": ("#241518", "#ea3943"),
    "warn": ("#2a2612", "#f0b90b"),
    "info": ("#141c2b", "#4d8df0"),
}

RETCODE_RE = re.compile(r"retcode[:=]?\s*(\d+)")


class Toast(QtWidgets.QFrame):
    """单条弹窗：标题 + 可选正文，按级别配色（底色/边框），可点击。"""

    FADE_MS = 150

    def __init__(
        self,
        title: str,
        body: str = "",
        level: str = "info",
        on_click: Callable[[], None] | None = None,
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        bg, accent = _LEVELS.get(level, _LEVELS["info"])
        self._on_click = on_click

        self.setObjectName("arbidToast")
        self.setStyleSheet(
            f"QFrame#arbidToast {{ background: {bg};"
            f" border: 1px solid {accent}; border-radius: 6px; }}"
        )

        box = QtWidgets.QVBoxLayout(self)
        box.setContentsMargins(10, 8, 10, 8)
        box.setSpacing(4)

        title_label = QtWidgets.QLabel(title, self)
        title_label.setStyleSheet(
            f"color: {accent}; background: transparent;"
            " border: none; font-weight: 600;"
        )
        box.addWidget(title_label)

        if body:
            body_label = QtWidgets.QLabel(body, self)
            body_label.setWordWrap(True)
            body_label.setStyleSheet(
                "color: #d7dee6; background: transparent; border: none;"
            )
            box.addWidget(body_label)

        if on_click is not None:
            self.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
            self.setToolTip("点击查看详情")

        # 用透明度效果实现淡入 / 淡出（Toast 不是顶层窗口，不能改 windowOpacity）
        self._opacity = QtWidgets.QGraphicsOpacityEffect(self)
        self._opacity.setOpacity(0.0)
        self.setGraphicsEffect(self._opacity)
        self._fade = QtCore.QPropertyAnimation(self._opacity, b"opacity")
        self._fade.setDuration(self.FADE_MS)
        self._fade.setLoopCount(1)

    def fade_in(self) -> None:
        """淡入到完全不透明。"""
        self._fade.stop()
        self._fade.setStartValue(0.0)
        self._fade.setEndValue(1.0)
        self._fade.start()

    def fade_out(self, on_done: Callable[[], None] | None = None) -> None:
        """淡出后执行 on_done（由调用方负责销毁）。"""
        self._fade.stop()
        self._fade.finished.connect(on_done)
        self._fade.setStartValue(self._opacity.opacity())
        self._fade.setEndValue(0.0)
        self._fade.start()

    def stop_fade(self) -> None:
        """停止进行中的淡入/淡出动画（复用本条 Toast 前调用）。"""
        self._fade.stop()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        if event.button() == QtCore.Qt.MouseButton.LeftButton and self._on_click:
            try:
                self._on_click()
            except Exception:
                pass
        super().mouseReleaseEvent(event)


class ToastManager(QtWidgets.QWidget):
    """贴在父窗口右下角的 Toast 浮层容器。"""

    WIDTH = 280
    MARGIN = 12
    GAP = 6
    MAX_TOASTS = 3

    # EventEngine 后台线程只 emit 这些 Signal，槽在 Qt 主线程执行
    order_result_event = QtCore.Signal(object)
    log_event = QtCore.Signal(object)

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._host: QtWidgets.QWidget | None = None
        self._session_name: str | None = None
        self._engine: Any = None
        self._handlers: list[tuple[str, Any]] = []
        self._toasts: list[Toast] = []

        self._layout = QtWidgets.QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(self.GAP)
        self._layout.setAlignment(
            QtCore.Qt.AlignmentFlag.AlignBottom | QtCore.Qt.AlignmentFlag.AlignRight
        )
        self.setFixedWidth(self.WIDTH)
        self.hide()

    # ------------------------------------------------------------------
    # 挂载与布局
    # ------------------------------------------------------------------
    def attach(self, parent: QtWidgets.QWidget) -> None:
        """作为浮层贴到 parent 右下角，parent resize / move 时跟随。"""
        self._host = parent
        self.setParent(parent)
        parent.installEventFilter(self)
        self.hide()

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 (Qt 命名)
        if obj is self._host and event.type() in (
            QtCore.QEvent.Type.Resize,
            QtCore.QEvent.Type.Move,
        ):
            self._reposition()
        return super().eventFilter(obj, event)

    def _reposition(self) -> None:
        host = self._host
        if host is None:
            return
        self.adjustSize()
        self.move(
            max(self.MARGIN, host.width() - self.width() - self.MARGIN),
            max(self.MARGIN, host.height() - self.height() - self.MARGIN),
        )
        self.raise_()

    # ------------------------------------------------------------------
    # 显示 / 堆叠 / 超时
    # ------------------------------------------------------------------
    def show_toast(
        self,
        title: str,
        body: str = "",
        level: str = "info",
        ms: int = 3000,
        on_click: Callable[[], None] | None = None,
    ) -> None:
        """新增一条 Toast；超过 3 条立即挤掉最旧的，超时（ms）后淡出销毁。"""
        try:
            toast = Toast(title, body, level, on_click)
        except Exception:
            return
        self._toasts.append(toast)
        self._layout.addWidget(toast)
        toast.show()
        toast.fade_in()

        while len(self._toasts) > self.MAX_TOASTS:
            self._dismiss_toast(self._toasts[0])

        self.adjustSize()
        self.show()
        self._reposition()

        # 超龄自动淡出销毁；若期间已被挤掉则 _toasts 中查不到，直接跳过
        QtCore.QTimer.singleShot(
            max(int(ms), 0), lambda: self._expire_toast(toast)
        )

    def _expire_toast(self, toast: Toast) -> None:
        if toast not in self._toasts:
            return
        try:
            toast.fade_out(lambda: self._dismiss_toast(toast))
        except Exception:
            self._dismiss_toast(toast)

    def _dismiss_toast(self, toast: Toast) -> None:
        """立即移除并销毁一条 Toast（可能已不在列表中，幂等）。"""
        if toast in self._toasts:
            self._toasts.remove(toast)
            toast.stop_fade()
            self._layout.removeWidget(toast)
        toast.deleteLater()
        self.adjustSize()
        self._reposition()
        if not self._toasts:
            self.hide()

    # ------------------------------------------------------------------
    # 事件订阅
    # ------------------------------------------------------------------
    def notify_hook(self, event_engine: Any, session_name: str | None = None) -> None:
        """订阅 eOrderResult / eLog；session_name 非 None 时只响应该会话。

        先反注册上一轮 handler，允许重复调用而不产生重复 toast。
        """
        if self._engine is not None:
            for event_type, handler in self._handlers:
                try:
                    self._engine.unregister(event_type, handler)
                except Exception:
                    pass
        self._engine = event_engine
        self._session_name = session_name
        self._handlers = [
            (
                EVENT_ORDER_RESULT,
                event_engine.register(
                    EVENT_ORDER_RESULT, self.order_result_event.emit
                ),
            ),
            (
                EVENT_LOG,
                event_engine.register(EVENT_LOG, self.log_event.emit),
            ),
        ]
        self.order_result_event.connect(self._on_order_result)
        self.log_event.connect(self._on_log)

    # ------------------------------------------------------------------
    # 槽（Qt 主线程）
    # ------------------------------------------------------------------
    def _on_order_result(self, event: Event) -> None:
        try:
            data = event.data or {}
            if not self._accept_session(data):
                return
            payload = data.get("payload") or {}
            ok = bool(payload.get("ok"))
            msg = str(payload.get("msg") or "")
            request = payload.get("request") or {}
            if ok:
                self.show_toast("成交", msg, "up")
            else:
                body = msg
                retcode = self._resolve_retcode(request, msg)
                if retcode is not None:
                    body = f"{msg}（{self._retcode_text(retcode)}）"
                self.show_toast("拒单", body, "down")
        except Exception:
            pass  # 坏事件不得让 UI 崩溃

    def _on_log(self, event: Event) -> None:
        try:
            data = event.data or {}
            if not self._accept_session(data):
                return
            payload = data.get("payload") or {}
            if str(payload.get("level") or "") != "ERROR":
                return
            msg = str(payload.get("msg") or "")
            self.show_toast("异常", msg[:120], "warn")
        except Exception:
            pass  # 坏事件不得让 UI 崩溃

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _accept_session(self, data: Any) -> bool:
        if self._session_name is None:
            return True
        try:
            return data.get("session") == self._session_name
        except Exception:
            return False

    @staticmethod
    def _resolve_retcode(request: Any, msg: str) -> int | None:
        try:
            value = request.get("retcode")
            if value is not None:
                return int(value)
        except Exception:
            pass
        try:
            match = RETCODE_RE.search(str(msg))
            if match:
                return int(match.group(1))
        except Exception:
            pass
        return None

    @staticmethod
    def _retcode_text(code: int) -> str:
        return RETCODE_NAME.get(int(code), f"未知 retcode {int(code):05d}")
