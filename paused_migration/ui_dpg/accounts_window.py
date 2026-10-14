"""账户管理主窗口（DPG 版，对照旧 mt5_trader/ui/accounts_view.py 行为）。

布局（与旧版 AccountsView/MainWindow 语义对齐）：
  [添加账户][编辑][删除][扫描 MT5 客户端][打开控制台(禁用,批2)]
  表格：登录号|服务器|绑定终端(路径)|数据目录|连接状态|操作
  底部状态栏：扫描进度（含最新发现条目）+ 旧版 statusBar 提示文案

交互：
- 行选择 = 每行首格 selectable(span_columns=True)（DPG 平替 Qt 行选中）；
- 双击 = 打开控制台（本批占位，状态栏提示"批2"）；
- 操作列：控制台(禁用)/编辑/删除（删除走 confirm 模态）；
- 连接状态列：state.sessions 最新状态驱动，已连接 UP / 未连接 DOWN。

排序：DPG 的 add_table_column 无 sort 参数；用 table sortable=True 的
回调（sort_specs == [[列tag, +1/-1], ...]）把 sort_key/sort_desc
写回 state，与其它刷新共用"整表重渲"路径（见 _on_sort 与报告说明）。
"""

from __future__ import annotations

import threading
import traceback
from pathlib import Path
from typing import Any

import dearpygui.dearpygui as dpg

from ..constants import EVENT_LOG, EVENT_STATUS
from ..event_engine import Event
from ..terminals import iter_deep_scan, scan   # 只读复用扫描管线（不改 terminals.py）
from .event_bridge import DpgEventBridge
from .state import DpgState, get_state, key_of
from .theme import rgba

# 本地线程事件类型（扫描线程 -> bridge queue -> 主线程 handler）
LOCAL_SCAN_FOUND = "eDpgScanFound"
LOCAL_SCAN_FINISHED = "eDpgScanFinished"

AUTO_TERMINAL = "(自动/默认终端)"
HINT_TEXT = "添加账户→绑定终端→打开控制台；控制台内：交易规则/隔夜利息/交易机会/策略控制台"

# (列标题, 列tag, 初始列宽, 是否吸收余量, 排序字段(None=不可排序))
COLUMNS: list[tuple[str, str, int, bool, str | None]] = [
    ("登录号", "dpg_col_login", 100, False, "login"),
    ("服务器", "dpg_col_server", 170, False, "server"),
    ("绑定终端", "dpg_col_terminal", 250, True, "terminal_path"),
    ("数据目录", "dpg_col_data", 250, False, "terminal_data"),
    ("连接状态", "dpg_col_status", 110, False, "status"),
    ("操作", "dpg_col_ops", 220, False, None),
]

PFX = "dpg_acc"
W_MAIN = "dpg_accounts_main"
W_TABLE = "dpg_accounts_table"
W_CONFIRM = "dpg_confirm_delete"
W_DIALOG = "dpg_dialog_account"

T_BTN_ADD = f"{PFX}_btn_add"
T_BTN_EDIT = f"{PFX}_btn_edit"
T_BTN_DEL = f"{PFX}_btn_del"
T_BTN_SCAN = f"{PFX}_btn_scan"
T_BTN_CONSOLE = f"{PFX}_btn_console"
T_SCAN_STATUS = f"{PFX}_scan_status"
T_HINT = f"{PFX}_hint"
T_CONFIRM_TEXT = f"{PFX}_confirm_text"
T_DLG_TITLE = f"{PFX}_dlg_title"
T_DLG_LOGIN = f"{PFX}_dlg_login"
T_DLG_PASSWORD = f"{PFX}_dlg_password"
T_DLG_SERVER = f"{PFX}_dlg_server"
T_DLG_COMBO = f"{PFX}_dlg_terminal"
T_DLG_MSG = f"{PFX}_dlg_msg"
T_FILE_DIALOG = f"{PFX}_file_dialog"


# ==========================================================================
# 扫描线程：照搬旧 TerminalScanThread（Qt QThread → threading.Thread + queue）
# ==========================================================================
class TerminalScanThread(threading.Thread):
    """全盘扫描（注册表+进程+常见目录+APPDATA 快速三路 → 全盘 BFS 兜底）。

    逐条命中经 bridge.submit 回主线程（found → sync_terminals），
    结束后发 LOCAL_SCAN_FINISHED。cancel() 语义与旧版一致：跳过剩余扫描、
    仍上报已确认条数。"""

    def __init__(self, bridge: DpgEventBridge) -> None:
        super().__init__(name="TerminalScanThread-dpg", daemon=True)
        self._bridge = bridge
        self._cancel_flag = False

    def cancel(self) -> None:
        self._cancel_flag = True

    def run(self) -> None:
        count = 0
        for item in scan(deep=False):
            if self._cancel_flag:
                break
            self._bridge.submit(LOCAL_SCAN_FOUND, item)
            count += 1
        if not self._cancel_flag:
            for item in iter_deep_scan(should_cancel=lambda: self._cancel_flag):
                if self._cancel_flag:
                    break
                self._bridge.submit(LOCAL_SCAN_FOUND, item)
                count += 1
        self._bridge.submit(LOCAL_SCAN_FINISHED, {"count": count, "canceled": self._cancel_flag})


# ==========================================================================
# bridge handlers（drain() 在 DPG 主线程逐条调用）
# ==========================================================================
def _on_session_status_event(event: Event) -> None:
    state = get_state()
    data = event.data or {}
    session = str(data.get("session", ""))
    payload = dict(data.get("payload") or {})
    changed = state.set_session_status(session, bool(payload.get("ok")), str(payload.get("msg", "")))
    if not changed:
        return
    index = state.ui_index_by_key.get(session)
    if index is not None:
        _set_status_cell(index, session)
    if state.sort_key == "status":   # 状态被用作排序键时保持行序最新
        state.reload_accounts()
        refresh_table()


def _set_status_cell(index: int, session: str) -> None:
    state = get_state()
    tag = f"{PFX}_status_{index}"
    if not dpg.does_item_exist(tag):
        return
    ok = state.session_ok(session)
    dpg.set_value(tag, "已连接" if ok is True else "未连接")
    dpg.configure_item(tag, color=rgba("UP") if ok is True else rgba("DOWN"))


def _on_log_event(event: Event) -> None:
    """批1 没有控制台页，先走 stderr，批2 由控制台页接管。"""
    data = dict(event.data or {})
    payload = dict(data.get("payload") or {})
    print(
        f"[eLog][{payload.get('source', data.get('session', ''))}][{payload.get('level', 'INFO')}]"
        f" {payload.get('msg', '')}"
    )


def _on_scan_found(event: Event) -> None:
    state = get_state()
    item = dict(event.data or {})
    state.scan_found(item)
    if state.store is not None:
        try:
            state.store.sync_terminals([item])
        except Exception:
            traceback.print_exc()
    dpg.set_value(T_SCAN_STATUS, state.scan_display())


def _on_scan_finished(event: Event) -> None:
    state = get_state()
    data = dict(event.data or {})
    canceled = bool(data.get("canceled"))
    state.scan_finish(canceled)
    state.scan_thread = None
    dpg.configure_item(T_BTN_SCAN, label="扫描 MT5 客户端")
    _set_scan_label()
    refresh_table()


def _set_scan_label() -> None:
    state = get_state()
    dpg.set_value(T_SCAN_STATUS, state.scan_display() or state.scan_finished_text)


# ==========================================================================
# 工具栏动作
# ==========================================================================
def _on_add_clicked() -> None:
    _open_dialog("add")


def _on_edit_clicked(sender=None, app_data=None, user_data=None) -> None:
    state = get_state()
    key = user_data if user_data else state.selected
    if key is None:
        dpg.set_value(T_SCAN_STATUS, "请先在表格中选中账户")
        return
    acc = state.ui_acc_by_row.get(f"{PFX}_row_{state.ui_index_by_key.get(key)}")
    if acc is None:
        for a in state.accounts:
            if key_of(a) == key:
                acc = a
                break
    if acc is None:
        dpg.set_value(T_SCAN_STATUS, "选中的账户已不存在")
        return
    _open_dialog("edit", source=dict(acc))


def _on_delete_clicked() -> None:
    state = get_state()
    if state.selected is None:
        dpg.set_value(T_SCAN_STATUS, "请先在表格中选中账户")
        return
    acc = None
    for a in state.accounts:
        if key_of(a) == state.selected:
            acc = a
            break
    if acc is None:
        dpg.set_value(T_SCAN_STATUS, "选中的账户已不存在")
        return
    state.confirm_target = dict(acc)
    dpg.set_value(T_CONFIRM_TEXT, f"删除 {state.selected}？（控制台将一并关闭）")
    dpg.show_item(W_CONFIRM)


def _confirm_cancel() -> None:
    get_state().confirm_target = None
    dpg.hide_item(W_CONFIRM)


def _confirm_yes() -> None:
    state = get_state()
    acc = state.confirm_target or {}
    dpg.hide_item(W_CONFIRM)
    state.confirm_target = None
    if not acc:
        return
    if state.store is not None:
        state.store.remove(acc["login"], acc["server"])
    state.selected = None
    state.reload_accounts()
    refresh_table()


def _on_scan_clicked() -> None:
    """再点一次 = 取消（对应旧版进度对话框的[取消]）。"""
    state = get_state()
    if state.scan_running:
        thread = state.scan_thread
        if thread is not None:
            thread.cancel()
            dpg.set_value(T_SCAN_STATUS, "取消中…（等待当前扫描盘点结束）")
        return
    thread = TerminalScanThread(state.bridge)
    state.scan_thread = thread
    state.scan_begin()
    dpg.configure_item(T_BTN_SCAN, label="取消扫描")
    _set_scan_label()
    thread.start()


def _console_placeholder(*_args, **_kwargs) -> None:
    """兼容旧签名；实际打开走 _open_console_by_key。"""
    if _args and isinstance(_args[0], str):
        _open_console_by_key(_args[0])


def _open_console_by_key(key: str) -> None:
    """批2：打开/前置该账户的 DPG 控制台。"""
    from .accounts import key_of  # noqa: F401  (占位避免误导入顺序问题——直接遍历)
    from .console_window import open_console

    state = get_state()
    for acc in state.accounts:
        if f'{acc.get("login")}@{acc.get("server", "?")}' == key:
            open_console(state, acc)
            dpg.set_value(T_SCAN_STATUS, f"控制台已打开: {key}")
            return
    dpg.set_value(T_SCAN_STATUS, f"未找到账户 {key}")


# ==========================================================================
# 行选择 / 双击
# ==========================================================================
def poll_interactions() -> None:
    """主循环每帧调用。

    行选中/双击不走 span-selectable（它在 DPG 2.3.1 会占格位导致列错位），
    改为对 mvTableRow 逐帧 is_item_hovered + 鼠标事件判定：
      - 左键单击行 = 选中（同旧版单选模式）
      - 双击行 = 打开控制台（批1 占位提示）
    悬停行内按钮时 DPG 对按钮优先，行选中不会误击穿到删除/编辑。"""
    state = get_state()
    left = dpg.mvMouseButton_Left
    for index, key in enumerate(state.overlay_row_keys):
        row_tag = f"{PFX}_row_{index}"
        if not dpg.does_item_exist(row_tag):
            continue
        try:
            hovered = dpg.is_item_hovered(row_tag)
        except Exception:
            continue   # 首帧渲染前 table_row 状态字典还没有 hovered 位（get_item_state KeyError）
        if hovered and dpg.is_mouse_button_double_clicked(left):
            _open_console_by_key(key)
            break
        if hovered and dpg.is_mouse_button_released(left) and state.selected != key:
            state.selected = key
            refresh_table()   # 重渲以便选中行高亮主题生效
            break


# ==========================================================================
# 表格渲染（state-first：读 state.sorted_accounts() 整表重渲）
# ==========================================================================
def refresh_table() -> None:
    state = get_state()
    # 旧行全部清掉（列头不删）
    for row_tag in list(state.ui_rows):
        if dpg.does_item_exist(row_tag):
            dpg.delete_item(row_tag)
    state.ui_rows = []

    accounts = state.sorted_accounts()
    state.overlay_row_keys = [key_of(a) for a in accounts]   # 行序快照，供逐帧双击轮询
    state.ui_index_by_key = {key_of(a): i for i, a in enumerate(accounts)}

    for index, acc in enumerate(accounts):
        row_tag = f"{PFX}_row_{index}"
        key = key_of(acc)
        state.ui_rows.append(row_tag)
        with dpg.table_row(parent=W_TABLE, tag=row_tag):
            dpg.add_text(str(acc["login"]), tag=f"{PFX}_login_{index}")
            dpg.add_text(str(acc.get("server", "")))
            # 绑定终端：完整绝对路径；失效的用红字标注（同旧版）
            terminal_path = acc.get("terminal_path") or AUTO_TERMINAL
            exists = Path(terminal_path).exists() if acc.get("terminal_path") else True
            if acc.get("terminal_path") and not exists:
                dpg.add_text(terminal_path + "  ← 已失效（文件不存在）", color=rgba("STALE"))
            else:
                dpg.add_text(terminal_path)
            dpg.add_text(str(acc.get("terminal_data") or "-"))
            # 连接状态
            ok = state.session_ok(key)
            dpg.add_text(
                tag=f"{PFX}_status_{index}",
                default_value="已连接" if ok is True else "未连接",
                color=rgba("UP") if ok is True else rgba("DOWN"),
            )  # 颜色待 bridge eSessionStatus 事件驱动 _set_status_cell
            # 选中行高亮（Table RowBg 覆盖为选中色；行随刷新重建，高亮绑在行上）
            if state.selected == key and state.ui_themes.get("row_selected"):
                dpg.bind_item_theme(row_tag, state.ui_themes["row_selected"])
            # 操作列：控制台(禁用)/编辑/删除
            with dpg.group(horizontal=True):
                btn_console = dpg.add_button(label="控制台", small=True,
                                             callback=_open_console_by_key, user_data=key)
                btn_edit = dpg.add_button(label="编辑", small=True,
                                          callback=_on_edit_clicked, user_data=key)
                del_theme = state.ui_themes.get("danger")
                btn_del = dpg.add_button(label="删除", small=True,
                                         callback=_on_delete_row_clicked, user_data=key)
                if del_theme:
                    dpg.bind_item_theme(btn_del, del_theme)

    state.ui_acc_by_row = {row_tag: acc for row_tag, acc in zip(state.ui_rows, accounts)}


def _on_delete_row_clicked(sender, app_data, user_data) -> None:
    """行内删除按钮：user_data=账户 key → 复用 confirm 模态。"""
    get_state().selected = user_data
    _on_delete_clicked()


def _on_sort(sender, sort_specs, user_data) -> None:
    """DPG 表格排序回调：[[列tag, +1|-1], ...] → state.sort_* → 整表按序重渲。"""
    state = get_state()
    if not sort_specs:
        return
    if sort_specs == state.ui_sort_specs:
        return   # 同一排序重复回调跳过，防重渲循环
    state.ui_sort_specs = list(map(list, sort_specs))
    # sort_specs 给的是列的内部 id，需 resolve 回我们的字符串别名
    col_field = {tag: field for _label, tag, _w, _s, field in COLUMNS if field}
    col_alias = dpg.get_item_alias(sort_specs[0][0])
    field = col_field.get(col_alias)
    if field is None:
        return
    state.sort_key = field
    state.sort_desc = sort_specs[0][1] < 0
    state.reload_accounts()
    refresh_table()


# ==========================================================================
# 添加/编辑账户对话框（对照旧 ui/account_dialog.py）
# ==========================================================================
def _living_terminal_items(state: DpgState) -> tuple[list[str], dict[str, Any]]:
    """仅列出真实存在的终端（旧版幽灵过滤）+ 末项 (自动/默认终端)。"""
    items: list[str] = []
    ids: dict[str, Any] = {}
    for terminal in (state.store.terminals() if state.store else []):
        path = terminal.get("path") or ""
        if not path or not Path(path).exists():
            continue
        items.append(path)
        ids[path] = terminal["id"]
    items.append(AUTO_TERMINAL)
    ids[AUTO_TERMINAL] = None
    return items, ids


def _open_dialog(mode: str, source: dict | None = None) -> None:
    state = get_state()
    state.dialog_mode = mode
    state.dialog_source = dict(source) if source else None
    items, ids = _living_terminal_items(state)
    state.dialog_terminal_ids = ids
    dpg.configure_item(T_DLG_COMBO, items=items)
    if mode == "edit" and state.dialog_source:
        source = state.dialog_source
        dpg.configure_item(T_DLG_TITLE, label="账户信息（编辑）")
        dpg.set_value(T_DLG_LOGIN, int(source.get("login", 1) or 1))
        dpg.set_value(T_DLG_PASSWORD, source.get("password", ""))
        dpg.set_value(T_DLG_SERVER, source.get("server", ""))
        path = source.get("terminal_path") or ""
        chosen = path if path in items else AUTO_TERMINAL   # 失效路径回落自动（同旧版）
        dpg.set_value(T_DLG_COMBO, chosen)
    else:
        state.dialog_source = None
        dpg.configure_item(T_DLG_TITLE, label="账户信息（添加）")
        dpg.set_value(T_DLG_LOGIN, 1)
        dpg.set_value(T_DLG_PASSWORD, "")
        dpg.set_value(T_DLG_SERVER, "")
        dpg.set_value(T_DLG_COMBO, AUTO_TERMINAL)
    dpg.set_value(T_DLG_MSG, "")
    dpg.show_item(W_DIALOG)


def _dialog_cancel() -> None:
    state = get_state()
    state.dialog_mode = None
    dpg.hide_item(W_DIALOG)


def _on_pick_terminal(sender, app_data, user_data) -> None:
    state = get_state()
    selections = (app_data or {}).get("selections") or {}
    path = next((v for v in selections.values()
                 if Path(v).name.lower() == "terminal64.exe"), None)
    if path is None:
        path = next(iter(selections.values()), None)
    if not path:
        return
    items, ids = _living_terminal_items(state)
    if path not in items:
        items = [path] + items   # 插最前（同旧版 insertItem(0)）
        ids[path] = None
    state.dialog_terminal_ids = ids
    dpg.configure_item(T_DLG_COMBO, items=items)
    dpg.set_value(T_DLG_COMBO, path)


def _dialog_ok() -> None:
    state = get_state()
    if state.store is None:
        return
    login = int(dpg.get_value(T_DLG_LOGIN) or 0)
    password = str(dpg.get_value(T_DLG_PASSWORD) or "")
    server = str(dpg.get_value(T_DLG_SERVER) or "").strip()
    if login < 1:
        dpg.set_value(T_DLG_MSG, "请输入有效账户号（≥1）")
        return
    if not server:
        dpg.set_value(T_DLG_MSG, "请输入服务器名称")
        return
    if not password:
        dpg.set_value(T_DLG_MSG, "请输入密码")
        return
    text = str(dpg.get_value(T_DLG_COMBO) or AUTO_TERMINAL)
    terminal_id = state.dialog_terminal_ids.get(text, None)
    creds = {
        "login": login,
        "password": password,
        "server": server,
        "symbols": [],
        "terminal_id": terminal_id,
        "terminal_path": None if AUTO_TERMINAL in text else text,
    }   # cred 字段与旧 dialog.credentials() 完全一致
    state.store.add(creds)
    state.dialog_mode = None
    state.selected = key_of(creds)
    dpg.hide_item(W_DIALOG)
    state.reload_accounts()
    refresh_table()


# ==========================================================================
# 主窗口
# ==========================================================================
def build() -> str:
    """创建主窗口（账户管理）；由 app_dpg.main() 调用并 set_primary_window。"""
    state = get_state()
    bridge = state.bridge

    # ---- 桥接 handler（主线程 drain 逐条执行）----
    bridge.register(EVENT_STATUS, _on_session_status_event)
    bridge.register(EVENT_LOG, _on_log_event)
    bridge.register(LOCAL_SCAN_FOUND, _on_scan_found)
    bridge.register(LOCAL_SCAN_FINISHED, _on_scan_finished)

    with dpg.window(tag=W_MAIN, label="MT5 Light Trader — 账户管理",
                    no_close=True, no_move=False, no_resize=False) as window:
        # ---- 工具栏（对照旧 AccountsView 工具栏 + 决策要求的[打开控制台]占位）----
        with dpg.group(horizontal=True):
            primary_theme = state.ui_themes.get("primary")
            del_theme = state.ui_themes.get("danger")
            btn_add = dpg.add_button(tag=T_BTN_ADD, label="添加账户", callback=_on_add_clicked)
            if primary_theme:
                dpg.bind_item_theme(btn_add, primary_theme)
            dpg.add_button(tag=T_BTN_EDIT, label="编辑", callback=_on_edit_clicked)
            btn_del = dpg.add_button(tag=T_BTN_DEL, label="删除", callback=_on_delete_clicked)
            if del_theme:
                dpg.bind_item_theme(btn_del, del_theme)
            dpg.add_button(tag=T_BTN_SCAN, label="扫描 MT5 客户端", callback=_on_scan_clicked)
            dpg.add_button(tag=T_BTN_CONSOLE, label="打开控制台", callback=_open_console_by_key,
                           user_data=None)

        # ---- 账户表 ----
        with dpg.table(tag=W_TABLE, header_row=True, resizable=True, sortable=True,
                       callback=_on_sort, row_background=True,
                       borders_innerH=True, borders_outerH=True,
                       borders_innerV=True, borders_outerV=True,
                       scrollY=True, height=470, no_saved_settings=True):
            for label, tag, width, stretch, field in COLUMNS:
                kwargs: dict = dict(tag=tag, init_width_or_weight=float(width))
                if stretch:
                    kwargs["width_stretch"] = True
                if field is None:
                    kwargs["no_sort"] = True
                dpg.add_table_column(label=label, **kwargs)

        # ---- 状态栏：扫描进度（含发现条目）+ 旧 statusBar 提示 ----
        dpg.add_text(tag=T_SCAN_STATUS, default_value="", color=rgba("WARN"))
        dpg.add_text(tag=T_HINT, default_value=HINT_TEXT, color=rgba("TXT_MUTED"))

    # ---- 删除 confirm 模态 ----
    with dpg.window(tag=W_CONFIRM, label="删除账户", modal=True, popup=True, show=False,
                    no_resize=True, autosize=True, no_saved_settings=True, pos=(260, 160)):
        dpg.add_text(tag=T_CONFIRM_TEXT, default_value="删除该账户？")
        with dpg.group(horizontal=True):
            dpg.add_button(label="取消", callback=_confirm_cancel)
            btn_del2 = dpg.add_button(label="删除", callback=_confirm_yes)
            if del_theme:
                dpg.bind_item_theme(btn_del2, del_theme)

    # ---- 添加/编辑账户 模态对话框 ----
    with dpg.window(tag=W_DIALOG, label="账户信息", modal=True, popup=True,
                    show=False, no_resize=True, width=520, height=310,
                    no_saved_settings=True, pos=(180, 80)):
        dpg.add_text(tag=T_DLG_TITLE, default_value="账户信息")
        dpg.add_input_int(tag=T_DLG_LOGIN, label="账户", default_value=1,
                          min_value=1, max_value=999_999_999,
                          min_clamped=True, max_clamped=True, width=200)
        dpg.add_input_text(tag=T_DLG_PASSWORD, label="密码", password=True, width=300)
        dpg.add_input_text(tag=T_DLG_SERVER, label="服务器", width=300, hint="如 MetaQuotes-Demo")
        with dpg.group(horizontal=True):
            dpg.add_combo(tag=T_DLG_COMBO, items=[AUTO_TERMINAL], default_value=AUTO_TERMINAL,
                          width=360)
            dpg.add_button(label="浏览…", callback=lambda: dpg.show_item(T_FILE_DIALOG))
        dpg.add_text(tag=T_DLG_MSG, default_value="", color=rgba("DOWN"))
        with dpg.group(horizontal=True):
            ok = dpg.add_button(label="确定", callback=_dialog_ok)
            if primary_theme:
                dpg.bind_item_theme(ok, primary_theme)
            dpg.add_button(label="取消", callback=_dialog_cancel)

    with dpg.file_dialog(tag=T_FILE_DIALOG, directory_selector=False, show=False,
                         callback=_on_pick_terminal,
                         width=700, height=400):   # 过滤用子项 add_file_extension
        dpg.add_file_extension(".exe", color=rgba("ACCENT"))

    state.reload_accounts()
    refresh_table()
    return window
