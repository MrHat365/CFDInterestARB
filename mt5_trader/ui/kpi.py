"""KPI 组件：指标卡、指标行、连接状态胶囊、全宽横幅。"""

from __future__ import annotations

from PySide6 import QtCore, QtWidgets

from .theme import COLORS

# set_value / set_card 的颜色KeyName -> 实际色值映射。
_VALUE_COLOR_MAP: dict[str, str] = {
    "up": COLORS["UP"],
    "down": COLORS["DOWN"],
    "warn": COLORS["WARN"],
    "neutral": COLORS["TXT_PRIMARY"],
}

# 状态胶囊各状态对应的文案与颜色。
_PILL_STATES: dict[str, tuple[str, str]] = {
    "connected": ("已连接", COLORS["UP"]),
    "waiting": ("等待登录", COLORS["WARN"]),
    "retrying": ("重试中", COLORS["WARN"]),
    "disconnected": ("断开", COLORS["DOWN"]),
}

# 横幅级别 -> (底色, 文字色)。
_BANNER_STYLES: dict[str, tuple[str, str]] = {
    "warn": ("#3a3120", COLORS["WARN"]),
    "error": ("#3a1d22", COLORS["DOWN"]),
    "info": ("#1d3a6b", COLORS["ACCENT"]),
}

# KpiRow 固定覆盖的六张卡名称。
KPI_NAMES: tuple[str, ...] = (
    "equity", "balance", "exposure", "margin", "swap", "connection",
)


class KpiCard(QtWidgets.QWidget):
    """单个 KPI 卡：11px 灰色标题 + 20px Consolas 加粗数值 + 单位小字。"""

    def __init__(
        self,
        title: str,
        unit: str = "",
        parent: QtWidgets.QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("KpiCard")
        self.setMinimumHeight(72)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(2)

        self._title_label = QtWidgets.QLabel(title, self)
        self._title_label.setObjectName("KpiTitle")

        value_row = QtWidgets.QHBoxLayout()
        value_row.setContentsMargins(0, 0, 0, 0)
        value_row.setSpacing(4)
        self._value_label = QtWidgets.QLabel("--", self)
        self._value_label.setObjectName("KpiValue")
        value_row.addWidget(self._value_label)
        value_row.addStretch(1)
        self._unit_label = QtWidgets.QLabel(unit, self)
        self._unit_label.setStyleSheet(
            f"color: {COLORS['TXT_MUTED']}; font-size: 10px;"
        )
        self._unit_label.setVisible(bool(unit))
        value_row.addWidget(self._unit_label, 0, QtCore.Qt.AlignBottom)

        layout.addWidget(self._title_label)
        layout.addLayout(value_row)

        # flash() 用：恢复正常前先把边框临时改为高亮蓝。
        self._flash_timer = QtCore.QTimer(self)
        self._flash_timer.setSingleShot(True)
        self._flash_timer.setInterval(300)
        self._flash_timer.timeout.connect(self._end_flash)

    def set_value(self, text: str, color_key: str | None = None) -> None:
        """设置数值文本与颜色。

        color_key∈("up","down","warn","neutral")映射 COLORS 中对应色值；
        None 使用中性色（TXT_PRIMARY）。
        """
        if color_key is None:
            color = COLORS["TXT_PRIMARY"]
        else:
            color = _VALUE_COLOR_MAP[color_key]
        self._value_label.setText(text)
        self._value_label.setStyleSheet(f"color: {color};")

    def flash(self) -> None:
        """边框短暂变 #2b4a6f，300ms 后恢复；QTimer 实现，可重复触发。"""
        self.setStyleSheet("QLabel#KpiCard { border: 1px solid #2b4a6f; }")
        self._flash_timer.start()

    def _end_flash(self) -> None:
        """flash 超时后清除临时样式，回到主题默认边框。"""
        self.setStyleSheet("")


class KpiRow(QtWidgets.QWidget):
    """水平排列 6 张 KPI 卡，通过名称访问：row.card("equity")。"""

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        self._cards: dict[str, KpiCard] = {}
        titles = {
            "equity": "净值",
            "balance": "余额",
            "exposure": "敞口",
            "margin": "占用保证金",
            "swap": "库存费",
            "connection": "连接",
        }
        for name in KPI_NAMES:
            card = KpiCard(titles[name], parent=self)
            self._cards[name] = card
            layout.addWidget(card, 1)

    def card(self, name: str) -> KpiCard:
        """按名称取卡，名称不在固定列表时抛 KeyError。"""
        return self._cards[name]

    def set_card(
        self, name: str, text: str, color_key: str | None = None
    ) -> None:
        """按名称设置指定卡的数值。"""
        self.card(name).set_value(text, color_key)


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


class Banner(QtWidgets.QFrame):
    """全宽提示横幅：show_banner 设置级别底色与文字，clear 隐藏复原。"""

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Banner")
        self.setFrameShape(QtWidgets.QFrame.NoFrame)
        self._label = QtWidgets.QLabel("", self)
        self._label.setWordWrap(True)
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(12, 6, 12, 6)
        layout.addWidget(self._label)
        self.clear()

    def show_banner(self, level: str, text: str) -> None:
        """按级别 ("warn"|"error"|"info") 显示横幅文字与配色。"""
        bg, fg = _BANNER_STYLES[level]
        self._label.setText(text)
        self.setStyleSheet(
            f"QFrame#Banner {{ background-color: {bg}; border-radius: 4px; }}"
            f"QFrame#Banner QLabel {{ color: {fg}; background: transparent; }}"
        )
        self.setVisible(True)

    def clear(self) -> None:
        """清空文字并隐藏横幅。"""
        self._label.setText("")
        self.setStyleSheet("")
        self.setVisible(False)
