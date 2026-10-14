"""主窗口：启动即账户管理页（增删改查 + 控制台入口）。"""

from __future__ import annotations

from PySide6 import QtWidgets

from ..accounts import AccountsStore
from ..database import Database
from ..event_engine import EventEngine
from .accounts_view import AccountsView


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, event_engine: EventEngine, store: AccountsStore, db: Database) -> None:
        super().__init__()
        self.setWindowTitle("MT5 Light Trader — 账户管理")
        self.resize(1100, 560)

        self._view = AccountsView(event_engine, store, db)
        self.setCentralWidget(self._view)
        self.statusBar().showMessage(
            "添加账户→绑定终端→打开控制台；控制台内：交易规则/隔夜利息/交易机会/策略控制台"
        )

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt 约定)
        # 账户管理是应用根：关闭 = 所有控制台一并关闭 + 应用退出。
        # 控制台不允许独立存在（隐藏的顶屉窗口不会再把进程挂着）。
        self._view.close_all_consoles()
        QtWidgets.QApplication.instance().quit()
        super().closeEvent(event)
