"""State-first 数据层：DPG 没有 Qt 那种 widget 状态，UI 只是状态的渲染。

- 所有可变数据（账户、会话状态、扫描线程、表格 UI 索引）都存放在 DpgState；
- 写入走 state 方法；UI 更新走 dpg.configure_item / set_value；
- 单例 get_state()：app_dpg.main() 里 init() 注入依赖（store/db/event_engine/bridge）
  与 UI 主题 tags，之后 accounts_window 等模块直接取用，避免一层层传句柄。
"""

from __future__ import annotations

import threading
import traceback
from typing import Any, Callable, Optional

import dearpygui.dearpygui as dpg

from ..accounts import AccountsStore
from ..database import Database
from .event_bridge import DpgEventBridge


def key_of(account: dict) -> str:
    """账户唯一键，与旧 accounts_view / account_session 的按钮映射一致。"""
    return f"{account.get('login', '?')}@{account.get('server', '?')}"


    # ------------------------------------------------------------------
    # plot series 颜色绑定（DPG 2.3 series 无 color 形参）

class DpgState:
    """应用级可变状态（见模块 docstring）。"""

    def __init__(self) -> None:
        self.store: AccountsStore | None = None
        self.db: Database | None = None
        self.event_engine: Any = None
        self.bridge: DpgEventBridge | None = None
        self.ui_themes: dict[str, Any] = {}
        self.ui_fonts: dict[str, Any] = {}
        self.running: bool = True   # 主循环守卫（退出置 False）

        # ------------------------------------------------------------ 账户
        self.accounts: list[dict] = []
        self.sessions: dict[str, dict] = {}   # key -> {"ok": bool, "msg": str}

        # ------------------------------------------------------------ 表格 UI 索引
        self.ui_rows: list[str] = []          # 表内行 tag（刷新时逐个删除）
        self.ui_index_by_key: dict[str, int] = {}
        self.ui_acc_by_row: dict[str, dict] = {}
        self.selected: str | None = None      # 选中账户 key（单选，同旧版）
        self.overlay_row_keys: list[str] = []  # 行序（排序后的 key 序列）

        # ------------------------------------------------------------ 排序
        # DPG 表格排序要点：add_table_column 没有 sort 参数，用 sortable=True +
        # table callback 收 sort_specs（[[列 tag, +1/-1], ...]），对 state.accounts
        # 排序后整表重渲（见 accounts_window._on_sort / sorted_accounts）。
        # 默认 None = 库序（与旧版 Qt 表格行为一致）。
        self.sort_key: Optional[str] = None
        self.sort_desc: bool = False
        self.ui_sort_specs: Any = None

        # ------------------------------------------------------------ 扫描
        self.scan_thread: Any = None
        self.scan_lock = threading.Lock()
        self.scan_running: bool = False
        self.scan_count: int = 0
        self.scan_last_path: str = ""
        self.scan_finished_text: str = ""

        # ------------------------------------------------------------ 对话框
        self.dialog_mode: str | None = None   # None / "add" / "edit"
        self.dialog_source: dict | None = None
        self.dialog_terminal_ids: dict[str, Any] = {}   # 下拉文本 -> terminal_id(None=自动)
        self.confirm_target: dict | None = None

    # ------------------------------------------------------------------
    def init(
        self,
        store: AccountsStore,
        db: Database,
        event_engine: Any,
        bridge: DpgEventBridge,
        ui_themes: dict[str, Any],
        ui_fonts: dict[str, Any],
    ) -> None:
        self.store = store
        self.db = db
        self.event_engine = event_engine
        self.bridge = bridge
        self.ui_themes = ui_themes
        self.ui_fonts = ui_fonts
        self.reload_accounts()

    # ------------------------------------------------------------------
    def reload_accounts(self) -> None:
        """重读 accounts 并保持当前排序。"""
        self.accounts = list(self.store.all()) if self.store else []
        self._overlay_key_index()

    # ------------------------------------------------------------------
    # plot series 颜色绑定（DPG 2.3 series 无 color 形参）
    # ------------------------------------------------------------------
    def bind_series_theme(self, series_tag, color_tag: str, kind: str = "line") -> None:
        """给 series 绑色（item-theme 缓存在 state）。"""
        from . import theme as th_mod
        self._series_themes = getattr(self, "_series_themes", {})
        key = f"theme_{color_tag}_{kind}"
        tag = self._series_themes.get(key)
        if tag is None or not dpg.does_item_exist(tag):
            tag = th_mod.series_item_theme(color_tag, kind)
            self._series_themes[key] = tag
        dpg.bind_item_theme(series_tag, tag)

    def th_right_width(self) -> int:
        """右面板宽度（7:3 —— 视口宽 * 0.28）。"""
        try:
            return int(dpg.get_viewport_width() * 0.28) - 10
        except Exception:
            return 320


    def sorted_accounts(self) -> list[dict]:
        """按当前排序返回渲染顺序（None = 库序）。"""
        field = self.sort_key
        if field is None:
            return list(self.accounts)

        def _sk(acc: dict):
            if field == "login":
                value = acc.get("login", -1)
                value = int(value) if isinstance(value, (int, float)) else -1
                return value
            if field == "status":
                ok = self.session_ok(key_of(acc))
                # 已连接 < 未连接 < 未知
                return 0 if ok is True else (1 if ok is False else 2)
            value = acc.get(field, "")
            return str(value or "")

        return sorted(self.accounts, key=_sk, reverse=self.sort_desc)

    def _overlay_key_index(self) -> None:
        keys = [key_of(a) for a in self.sorted_accounts()]
        keys_set = set(keys)
        if self.selected is not None and self.selected not in keys_set:
            self.selected = None
        self.overlay_row_keys = keys
        self.ui_index_by_key = {k: i for i, k in enumerate(keys)}

    # ------------------------------------------------------------------
    # 会话状态（EVENT_STATUS handler）
    # ------------------------------------------------------------------
    def set_session_status(self, session: str, ok: bool, msg: str) -> bool:
        """记录最新状态；返回是否有变化（供 bridge 去抖渲染）。"""
        prev = self.sessions.get(session)
        self.sessions[session] = {"ok": bool(ok), "msg": msg}
        return prev is None or prev.get("ok") != bool(ok) or prev.get("msg") != msg

    def session_ok(self, session: str) -> bool | None:
        info = self.sessions.get(session)
        if info is None:
            return None    # 尚未收到任何状态事件
        return bool(info.get("ok"))

    def session_msg(self, session: str) -> str:
        info = self.sessions.get(session)
        return str(info.get("msg", "")) if info else ""

    # ------------------------------------------------------------------
    # 扫描
    # ------------------------------------------------------------------
    def scan_begin(self) -> None:
        self.scan_running = True
        self.scan_count = 0
        self.scan_last_path = ""
        self.scan_finished_text = ""

    def scan_found(self, item: dict) -> str:
        """记录一条扫描命中；返回展示文本（最新一条）。"""
        self.scan_count += 1
        self.scan_last_path = item.get("path") or item.get("data_folder") or ""
        return self.scan_last_path

    def scan_finish(self, canceled: bool) -> str:
        """扫描结束；返回总结文本。"""
        total = len(self.store.terminals()) if self.store else 0
        self.scan_running = False
        self.scan_finished_text = (
            f"{'已取消，已确认 ' if canceled else '扫描完成：新增/确认 '}"
            f"{self.scan_count} 个终端，去重后本机累计 {total} 个"
        )
        self.reload_accounts()
        return self.scan_finished_text

    def scan_display(self) -> str:
        if self.scan_finished_text:
            return self.scan_finished_text
        if self.scan_running:
            if self.scan_last_path:
                return f"发现 {self.scan_count} 个:\n{self.scan_last_path}\n…继续扫描"
            return "正在扫描本机 MT5 客户端（注册表 → 运行中进程 → 已知目录 → 全盘 BFS）…"
        return ""


# ----------------------------------------------------------------------
_STATE: DpgState | None = None


def get_state() -> DpgState:
    global _STATE
    if _STATE is None:
        _STATE = DpgState()
    return _STATE


def safe_call(fn: Callable[[], Any]) -> None:
    """UI 侧异常兜底：任何回调异常不允许击穿主循环。"""
    try:
        fn()
    except Exception:
        traceback.print_exc()
