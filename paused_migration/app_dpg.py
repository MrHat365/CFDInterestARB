"""MT5 Light Trader — DearPyGui 入口（批1：骨架 + 账户管理窗口）。

对照旧 app.py 的启动序列（Qt → DPG）：
  create_context → fonts → theme → 数据层/事件引擎 → state/bridge
  → 账户管理主窗口 → viewport → setup/show → 主循环（drain + render）
  退出：set_exit_callback 安全停引擎/关库。

运行：python app_dpg.py
（旧 PySide6 入口 app.py 保留不动；默认数据库/事件引擎/算法核心完全复用）
"""

from __future__ import annotations

import atexit
import sys

import dearpygui.dearpygui as dpg

from mt5_trader.accounts import AccountsStore
from mt5_trader.constants import (
    EVENT_ACCOUNT, EVENT_DEAL, EVENT_LOG, EVENT_POSITION, EVENT_RULES,
    EVENT_SPREAD_ALGO, EVENT_SPREAD_HISTORY, EVENT_STATUS, EVENT_SWAPS,
    EVENT_TICK,
)
from mt5_trader.database import Database
from mt5_trader.event_engine import EventEngine
from mt5_trader.services.db_service import SwapSnapshotService
from mt5_trader.ui_dpg import accounts_window, console_window, fonts
from mt5_trader.ui_dpg.event_bridge import DpgEventBridge
from mt5_trader.ui_dpg.state import get_state
from mt5_trader.ui_dpg.theme import apply_theme


def _shutdown() -> None:
    """退出安全停机（幂等）：engine daemon + db close 由 atexit 兜底，此处主动收。"""
    state = get_state()
    if not state.running:
        return
    state.running = False
    scan_thread = state.scan_thread
    if scan_thread is not None and scan_thread.is_alive():
        scan_thread.cancel()
    if state.event_engine is not None:
        try:
            state.event_engine.stop()
        except Exception:
            pass
    if state.db is not None:
        try:
            state.db.close()
        except Exception:
            pass


def main() -> None:
    dpg.create_context()   # 必须先于一切 DearPyGui 调用
    atexit.register(_shutdown)   # 任何退出路径的安全停机（幂等）

    # 0. 字体（中文缺字防线）+ 全局主题
    ui_fonts = fonts.setup_fonts(14)
    ui_themes = apply_theme()

    # 1. 数据层 + 持久化服务（与旧 app.py 相同序列）
    db = Database()
    swap_service = SwapSnapshotService(db)

    # 2. 事件引擎（daemon 分发线程）+ bridge 订阅
    event_engine = EventEngine()
    event_engine.start()
    swap_service.start(event_engine)

    # 3. state-first：账户/会话/线程都持有在 DpgState
    store = AccountsStore(db)
    state = get_state()
    bridge = DpgEventBridge(event_engine, subscribe=(
        EVENT_STATUS, EVENT_LOG, EVENT_ACCOUNT, EVENT_RULES, EVENT_SWAPS,
        EVENT_SPREAD_HISTORY, EVENT_POSITION, EVENT_DEAL, EVENT_TICK,
        EVENT_SPREAD_ALGO,
    ))

    # 4. 账户管理主窗口
    state.init(store=store, db=db, event_engine=event_engine, bridge=bridge,
               ui_themes=ui_themes, ui_fonts=ui_fonts)
    main_window = accounts_window.build()
    dpg.set_primary_window(main_window, True)

    # 5. viewport + 主循环
    dpg.create_viewport(title="MT5 Light Trader", width=1150, height=620)
    dpg.setup_dearpygui()
    dpg.set_exit_callback(_shutdown)
    dpg.show_viewport()

    try:
        while dpg.is_dearpygui_running():
            bridge.drain()                       # 每帧排空事件队列（主线程内）
            accounts_window.poll_interactions()  # 行双击等逐帧交互轮询
            console_window.poll_all()            # 控制台帧回调（报价/策略）
            dpg.render_dearpygui_frame()
        _shutdown()
    finally:
        dpg.destroy_context()


if __name__ == "__main__":
    main()
