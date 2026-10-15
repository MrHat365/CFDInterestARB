"""账户连接状态胶囊。"""

from __future__ import annotations

from PySide6 import QtWidgets

from .theme import COLORS

# 状态胶囊各状态对应的文案与颜色。
_PILL_STATES: dict[str, tuple[str, str]] = {
    "connected": ("已连接", COLORS["UP"]),
    "waiting": ("等待登录", COLORS["WARN"]),
    "retrying": ("重试中", COLORS["WARN"]),
    "disconnected": ("断开", COLORS["DOWN"]),
}

class StatusPill(QtWidgets.QLabel):
    """连接状态胶囊：圆角底 + 状态点 + 文案，颜色随状态切换。"""

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.set_state("disconnected")

    def set_state(self, state: str) -> None:
        """设置状态，state∈("connected","waiting","retrying","disconnected")。"""
        text, color = _PILL_STATES[state]
        self.setText(f"● {text}")
        self.setStyleSheet(
            f"color: {color}; background-color: {COLORS['BG_PANEL']};"
            "border: 1px solid"
            f" {COLORS['BORDER']}; border-radius: 10px; padding: 3px 12px;"
        )
