"""MT5 Light Trader 入口。

启动即账户管理界面；每个已连接账户一个独立子进程（直连各自 MT5 终端），
SQLite 负责账户/终端绑定/swap 快照/策略记录/价差缓存持久化。
"""

from __future__ import annotations

import atexit
import sys

from PySide6 import QtWidgets

from mt5_trader.accounts import AccountsStore
from mt5_trader.database import Database
from mt5_trader.event_engine import EventEngine
from mt5_trader.services.db_service import SwapSnapshotService
from mt5_trader.ui.main_window import MainWindow
from mt5_trader.ui.theme import apply_theme


def main() -> None:
    app = QtWidgets.QApplication(sys.argv)
    app.setStyle("Fusion")
    apply_theme(app)   # 全局暗色 QSS（含 KPI 卡/表格/导航/命令条主题）

    # 1. 数据层 + 持久化服务
    db = Database()
    atexit.register(db.close)
    swap_service = SwapSnapshotService(db)

    # 2. 事件引擎
    event_engine = EventEngine()
    event_engine.start()
    atexit.register(event_engine.stop)
    swap_service.start(event_engine)

    # 3. 主窗口（账户管理）
    store = AccountsStore(db)
    window = MainWindow(event_engine, store, db)
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
