"""MT5 Light Trader 入口。

启动即账户管理界面；每个已连接账户一个独立子进程（直连各自 MT5 终端），
SQLite 负责账户/终端绑定/swap 快照/策略记录/价差缓存持久化。
"""

from __future__ import annotations

import sys

from PySide6 import QtWidgets

from mt5_trader.accounts import AccountsStore
from mt5_trader.database import Database
from mt5_trader.event_engine import EventEngine
from mt5_trader.services.db_service import SwapSnapshotService, ExecutionJournalService
from mt5_trader.ui.main_window import MainWindow
from mt5_trader.ui.theme import apply_theme
from mt5_trader.session_registry import SessionRegistry


def main() -> None:
    app = QtWidgets.QApplication(sys.argv)
    app.setStyle("Fusion")
    apply_theme(app)   # 全局暗色 QSS（含 KPI 卡/表格/导航/命令条主题）

    # 1. 数据层 + 持久化服务
    db = Database()
    swap_service = SwapSnapshotService(db)
    execution_service = ExecutionJournalService(db)

    # 2. 事件引擎
    event_engine = EventEngine()
    event_engine.start()
    swap_service.start(event_engine)
    execution_service.start(event_engine)

    # 3. 主窗口（账户管理）
    store = AccountsStore(db)
    sessions = SessionRegistry(event_engine, str(db.path.resolve()))
    try:
        window = MainWindow(event_engine, store, db, sessions)
        window.show()
        result = app.exec()
    finally:
        sessions.close()
        event_engine.stop()  # drain persistence events before closing SQLite
        swap_service.stop(event_engine)
        execution_service.stop(event_engine)
        db.close()
    sys.exit(result)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--worker":
        from mt5_trader.worker_pipe import main as worker_pipe_main
        worker_pipe_main(int(sys.argv[2]))
    else:
        main()
