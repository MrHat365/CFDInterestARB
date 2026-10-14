"""MT5 Light Trader 入口。

启动即账户管理界面；每个已连接账户一个独立子进程（直连各自 MT5 终端），
SQLite 负责账户/终端绑定/swap 快照/策略记录/价差缓存持久化。
"""

from __future__ import annotations

import atexit
import sys
import threading
import time

from PySide6 import QtWidgets

from mt5_trader.accounts import AccountsStore
from mt5_trader.database import Database
from mt5_trader.event_engine import EventEngine
from mt5_trader.services.db_service import SwapSnapshotService
from mt5_trader.ui.main_window import MainWindow
from mt5_trader.ui.theme import apply_theme


def _join_pending_session_stops(total_timeout: float = 10.0) -> None:
    """atexit 兜底（安全下调子进程）：主进程退出前等待已在运行的 session stop 线程。

    工程没有中央 session registry；AccountConsole.stop_session_and_close
    （mt5_trader/ui/console.py）以固定前缀 "stop-<session>" 命名其后台
    stop 线程（daemon，内部 join worker 子进程至多 8s 后 terminate），
    据此用 threading.enumerate（Python 自带的线程登记表）识别并 join。
    共享一个总时长预算，控制台再多也不会拖死退出。"""
    deadline = time.monotonic() + total_timeout
    for thread in threading.enumerate():
        if thread is threading.current_thread():
            continue
        if not thread.name.startswith("stop-") or not thread.is_alive():
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        try:
            thread.join(timeout=remaining)
        except Exception:
            pass


def main() -> None:
    # atexit 后进先出：此处 register 最晚 → 进程退出码前最先跑，先于
    # event_engine.stop 与 db.close，给 worker 子进程收尾机会，
    # 避免退出瞬间 daemon stop 线程被硬杀。
    atexit.register(_join_pending_session_stops)
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
