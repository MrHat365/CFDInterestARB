"""集中主题 Token 与暗色 QSS：颜色常量、全局样式表、应用入口。"""

from __future__ import annotations

from PySide6 import QtWidgets

# 调色板 Token：key 按语义命名（背景/文字/涨跌/告警/强调等）。
COLORS: dict[str, str] = {
    "BG_BASE": "#0e1116",      # 主窗口底色
    "BG_PANEL": "#151a21",     # 面板/卡片底色
    "BG_TABLE": "#12161d",     # 表格底色
    "BG_RAISED": "#1c222c",    # 浮起控件（按钮/输入框）底色
    "BORDER": "#2a3140",       # 常规边框
    "TXT_PRIMARY": "#dde3ec",  # 主文字
    "TXT_MUTED": "#8b93a3",    # 次要文字
    "UP": "#16c784",           # 涨 / 正值
    "DOWN": "#ea3943",         # 跌 / 负值
    "WARN": "#f0b90b",         # 告警
    "ACCENT": "#4d8df0",       # 强调色（焦点/选中）
    "GRID": "#202836",         # 表格网格线
    "ZEBRA": "#171c25",        # 斑马纹交替行
}

# 全局暗色 QSS 模板，覆盖 KPI 卡、表格、表单控件、按钮、导航、标签页、滚动条等。
_DARK_QSS_TEMPLATE: str = """
* {
    color: %(TXT_PRIMARY)s;
    font-family: "Microsoft YaHei", "Segoe UI", sans-serif;
    font-size: 12px;
}
QWidget {
    background-color: %(BG_BASE)s;
}
QLabel {
    background: transparent;
}
QLabel#KpiCard {
    background-color: %(BG_PANEL)s;
    border: 1px solid %(BORDER)s;
    border-radius: 8px;
}
QLabel#KpiValue {
    background: transparent;
    font-family: Consolas, monospace;
    font-size: 20px;
    font-weight: bold;
}
QLabel#KpiTitle {
    background: transparent;
    color: %(TXT_MUTED)s;
    font-size: 11px;
}
QTableWidget {
    background-color: %(BG_TABLE)s;
    alternate-background-color: %(ZEBRA)s;
    gridline-color: %(GRID)s;
    border: 1px solid %(BORDER)s;
    selection-background-color: #24344d;
    selection-color: %(TXT_PRIMARY)s;
}
QTableWidget::item:hover {
    background-color: #1c2736;
}
QTableWidget QHeaderView {
    border: none;
}
QHeaderView::section {
    background-color: %(BG_RAISED)s;
    color: %(TXT_MUTED)s;
    padding: 4px 8px;
    border: none;
    border-right: 1px solid %(BORDER)s;
    border-bottom: 1px solid %(BORDER)s;
}
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {
    background-color: %(BG_RAISED)s;
    border: 1px solid %(BORDER)s;
    border-radius: 4px;
    padding: 4px 8px;
}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {
    border: 1px solid %(ACCENT)s;
}
QComboBox QAbstractItemView {
    background-color: %(BG_RAISED)s;
    border: 1px solid %(BORDER)s;
    selection-background-color: #24344d;
}
QPushButton {
    background-color: %(BG_RAISED)s;
    border: 1px solid %(BORDER)s;
    border-radius: 4px;
    padding: 5px 14px;
}
QPushButton:hover {
    background-color: #242c38;
    border-color: #3c4557;
}
QPushButton:pressed {
    background-color: #1a2029;
}
QPushButton#primary {
    background-color: %(ACCENT)s;
    border: 1px solid %(ACCENT)s;
    color: #ffffff;
    font-weight: bold;
}
QPushButton#primary:hover {
    background-color: #6199f3;
}
QPushButton#danger {
    background-color: %(DOWN)s;
    border: 1px solid %(DOWN)s;
    color: #ffffff;
    font-weight: bold;
}
QPushButton#danger:hover {
    background-color: #ef555e;
}
QPushButton:disabled {
    background-color: %(BG_RAISED)s;
    border: 1px solid %(BORDER)s;
    color: #5a6272;
}
QListWidget {
    background-color: %(BG_PANEL)s;
    border: 1px solid %(BORDER)s;
    border-radius: 4px;
    outline: none;
}
QListWidget::item {
    padding: 6px 10px;
    border-radius: 4px;
}
QListWidget::item:hover {
    background-color: #1c2736;
}
QListWidget::item:selected {
    background-color: #24344d;
    color: %(TXT_PRIMARY)s;
}
QTabWidget::pane {
    background-color: %(BG_PANEL)s;
    border: 1px solid %(BORDER)s;
    border-radius: 4px;
}
QTabBar::tab {
    background: transparent;
    color: %(TXT_MUTED)s;
    padding: 6px 14px;
    border: 1px solid %(BORDER)s;
    border-bottom: none;
    border-top-left-radius: 4px;
    border-top-right-radius: 4px;
    margin-right: 2px;
}
QTabBar::tab:selected {
    background-color: %(BG_RAISED)s;
    color: %(TXT_PRIMARY)s;
    border-bottom: 1px solid %(BG_RAISED)s;
}
QTabBar::tab:hover {
    color: %(TXT_PRIMARY)s;
}
QScrollBar:vertical {
    background: %(BG_BASE)s;
    width: 10px;
    border: none;
}
QScrollBar::handle:vertical {
    background: #333c4d;
    border-radius: 5px;
    min-height: 30px;
}
QScrollBar::handle:vertical:hover {
    background: #414c60;
}
QScrollBar:horizontal {
    background: %(BG_BASE)s;
    height: 10px;
    border: none;
}
QScrollBar::handle:horizontal {
    background: #333c4d;
    border-radius: 5px;
    min-width: 30px;
}
QScrollBar::handle:horizontal:hover {
    background: #414c60;
}
QScrollBar::add-line, QScrollBar::sub-line {
    height: 0;
    width: 0;
}
QScrollBar::add-page, QScrollBar::sub-page {
    background: none;
}
QPlainTextEdit {
    background-color: %(BG_TABLE)s;
    border: 1px solid %(BORDER)s;
    border-radius: 4px;
    selection-background-color: #24344d;
}
QSplitter::handle {
    background: %(BORDER)s;
    width: 2px;
    height: 2px;
}
QSplitter::handle:hover {
    background: %(ACCENT)s;
}
QMenu {
    background-color: %(BG_RAISED)s;
    border: 1px solid %(BORDER)s;
    padding: 4px;
}
QMenu::item {
    padding: 5px 24px 5px 12px;
    border-radius: 3px;
}
QMenu::item:selected {
    background-color: #24344d;
}
QMenu::separator {
    height: 1px;
    background: %(BORDER)s;
    margin: 4px 6px;
}
QToolTip {
    background-color: %(BG_RAISED)s;
    color: %(TXT_PRIMARY)s;
    border: 1px solid %(BORDER)s;
    padding: 4px 8px;
}
"""

# 用 COLORS 渲染出的最终样式表。
DARK_QSS: str = _DARK_QSS_TEMPLATE % COLORS


def apply_theme(app: QtWidgets.QApplication) -> None:
    """应用 Fusion 风格与全局暗色 QSS 到 QApplication。"""
    app.setStyle("Fusion")
    app.setStyleSheet(DARK_QSS)
