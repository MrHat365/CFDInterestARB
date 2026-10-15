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
import time
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
    10024: "请求过于频繁",
    10025: "无变更",
    10026: "服务器禁止自动交易",
    10027: "客户端禁止自动交易",
    10028: "请求被锁定",
    10029: "订单或持仓冻结",
    10030: "不支持的成交方式",
    10031: "无连接",
    10032: "仅真实账户可用",
    10033: "挂单数量达到上限",
    10034: "手数达到上限",
    10036: "持仓已关闭",
    10038: "平仓量超过持仓量",
    10010: "部分成交",
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
        else:
            body_label = None
        self._body_label: QtWidgets.QLabel | None = body_label
        self._title_label: QtWidgets.QLabel = title_label

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

    def _update_text(self, title: str, body: str) -> None:
        """复用现存 Toast：更新标题/正文本（用于同类合并计次入文本）。"""
        try:
            self._title_label.setText(title)
            if self._body_label is None:
                if body:
                    label = QtWidgets.QLabel(body, self)
                    label.setWordWrap(True)
                    label.setStyleSheet(
                        "color: #d7dee6; background: transparent; border: none;"
                    )
                    self.layout().addWidget(label)
                    self._body_label = label
            else:
                self._body_label.setText(body)
        except Exception:
            pass

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
    # 屏幕同时可见的 Toast 数上限。调到 6：右下角仍留视野给主界面控件，
    # 又足够覆盖不同 session/level 的分类结果；同类重复已被去重合并吸收。
    MAX_TOASTS = 6

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
        # 同类合并限流：key = (session_name, level, title) ->
        # (最近一次弹出的单调钟时刻, 总累计条数)。
        # 在 DUP_WINDOW_SEC 内同 key 重复只弹一次，后续仅累加计
        # 数并把计数合并进文本，避免事件风暴刷屏。
        self._dup_window_sec: float = 5.0
        self._last_shown: dict[
            tuple[str | None, str, str], tuple[float, int]
        ] = {}

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
        """新增一条 Toast；同类（同 session/level/标题）在去重窗口内
        只弹一次，后续重复仅合并计次入文本；超过 MAX_TOASTS 立即挤掉
        最旧的，超时（ms）后淡出销毁。
        """
        key = (self._session_name, level, title)
        now = time.monotonic()
        last_ts, _last_count = self._last_shown.get(key, (None, 0))
        count = 1
        if last_ts is not None and (now - last_ts) < self._dup_window_sec:
            count = _last_count + 1
        self._last_shown[key] = (now, count)
        if count > 1:
            # 只更新现存 Toast 的文案（合并计次），不重复弹窗
            toast = self._find_toast(key)
            if toast is not None:
                self._update_merged_body(toast, title, body, level, count, now, ms)
                return
        try:
            toast = Toast(title, body, level, on_click)
        except Exception:
            return
        toast._t_last = now
        toast._t_key = key
        toast._t_count = count
        self._toasts.append(toast)
        self._layout.addWidget(toast)
        toast.show()
        toast.fade_in()

        while len(self._toasts) > self.MAX_TOASTS:
            self._dismiss_toast(self._toasts[0])

        self.adjustSize()
        self.show()
        self._reposition()

        # 超龄自动淡出销毁；若期间已被挤掉则 _toasts 中查不到，直接跳过。
        # 计时器挂在 toast 上：同类合并（_update_merged_body）时用同一个
        # 计时器滚动重置，避免旧 singleShot 在合并窗口内把 still-live
        # 的新计次中途淡出掉。
        timer = QtCore.QTimer(toast)
        timer.setSingleShot(True)
        timer.timeout.connect(lambda: self._expire_toast(toast))
        toast._t_expire_timer = timer
        timer.start(max(int(ms), 0))

    def _find_toast(
        self, key: tuple[str | None, str, str]
    ) -> Toast | None:
        """按 key 找当前仍存活的最新一条 Toast（合并计次写入它）。"""
        for toast in reversed(self._toasts):
            if getattr(toast, "_t_key", None) == key:
                return toast
        return None

    def _update_merged_body(
        self,
        toast: Toast,
        title: str,
        body: str,
        level: str,
        count: int,
        now: float,
        ms: int,
    ) -> None:
        """把同类重复计次合并进现存 Toast 文本，并滚动重置其超时。"""
        try:
            toast.stop_fade()
            merged_body = f"{body}\n（同类已合并 {count} 条）"
            toast._update_text(title, merged_body)
            toast._t_last = now
            toast._t_count = count
            toast.fade_in()
            toast.update()
            # 计次合并算「同类有活动」：滚动重置过期时刻（同一个计时器
            # 上 start() 会取消尚未触发的计时），保证在合并窗口内持续重复
            # 不会中途被旧的计时淡出。
            timer = getattr(toast, "_t_expire_timer", None)
            if timer is not None:
                timer.start(max(int(ms), 0))
            self.adjustSize()
            self._reposition()
        except Exception:
            pass

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
    def dispose(self) -> None:
        if getattr(self, "_engine", None):
            for event_type, handler in self._handlers:
                self._engine.unregister(event_type, handler)
            self._handlers.clear()
            self._engine = None
        self._disposed = True
        for toast in list(self._toasts):
            self._dismiss_toast(toast)

    def notify_hook(self, event_engine: Any, session_name: str | None = None, accept_event=None) -> None:
        """订阅 eOrderResult / eLog；session_name 非 None 时只响应该会话。

        先反注册上一轮 handler，允许重复调用而不产生重复 toast。
        """
        if self._engine is not None:
            for event_type, handler in self._handlers:
                try:
                    self._engine.unregister(event_type, handler)
                except Exception:
                    pass
        self._disposed = False
        self._accept_event = accept_event
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
        if not getattr(self, "_slots_bound", False):
            self.order_result_event.connect(self._on_order_result)
            self.log_event.connect(self._on_log)
            self._slots_bound = True

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
        if getattr(self, "_disposed", False):
            return False
        predicate = getattr(self, "_accept_event", None)
        if predicate and not predicate(Event("", data)):
            return False
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
