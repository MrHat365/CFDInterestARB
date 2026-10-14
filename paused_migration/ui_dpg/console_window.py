"""DearPyGui 控制台窗口（迁移批2，完整重写版）。

结构对照 Qt 版回归骨架：
    顶栏：状态灯 + 账户摘要 + 纸面/实盘 + [断开]
    Tab1 规则·隔夜利息：合并一表（排序下拉+降序选+过滤）
    Tab2 交易机会：生成机会 + 行尾 [载入]
    Tab3 策略控制台：上 = 左图（dpg.plot：价差线 + 原生 GPU add_candle_series
        叠加 + 均值/±kσ 参考线）/ 右功能操作区；下 = 持仓/成交/信号/日志 四子Tab。

DPG 2.3 API 硬约束（本文件统一遵守）：
  1) 除 series/plot_axis 外，所有 add_* 一律 keyword-only；
  2) 每个非窗口容器都用 `with dpg.xxx(...) as tag:` 栈式创建，杜绝
     "Parent could not be deduced"；
  3) series 颜色通过 item-theme 绑（无 color 形参）；
  4) set_value(candle_series, data) 的 data = [(time, open, close, low, high), ...]。
数据在 DpgConsole 实例持有（state-first），UI 只是投影；
算法执行走 eAlgoRequest(eAlgoRequest) → worker（vnpy SpreadTakerAlgo 两阶段闭环）。
"""

from __future__ import annotations

import collections
import datetime as dt
import math
import threading
import time

import dearpygui.dearpygui as dpg

from ..account_session import AccountSession
from ..constants import (
    EVENT_ACCOUNT,
    EVENT_ALGO_REQUEST,
    EVENT_LOG,
    EVENT_POSITION,
    EVENT_RULES,
    EVENT_SPREAD_ALGO,
    EVENT_SPREAD_HISTORY,
    EVENT_STATUS,
    EVENT_SWAPS,
    EVENT_SYMBOL_SUBSCRIBE,
    EVENT_TICK,
)
from ..event_engine import Event
from ..opportunities import generate
from ..spreads import build_legs_from_opportunity
from ..strategy import StrategyRunner
from . import theme as th
from .state import get_state

_H1 = 3600.0

DATA_COLUMNS = [
    ("symbol", "品种", 100), ("digits", "小数位", 56), ("point", "最小变动点", 80),
    ("spread", "当前点差", 70), ("trade_mode", "交易模式", 80),
    ("volume_min", "最小手", 60), ("volume_step", "步长", 60),
    ("volume_max", "最大手", 70), ("margin_initial", "初始保证金", 90),
    ("swap_long", "买入库存费", 90), ("swap_short", "卖出库存费", 90),
    ("swap_rollover3days", "三倍息日", 76), ("swap_mode", "库存费机制", 110),
    ("update_time", "最近更新", 122),
]
OPP_COLUMNS = [
    ("net_swap", "净息/天(USD·组合)", 130), ("legs_count", "腿数", 56),
    ("legs_text", "组合", 360),
]
LEG_COLUMNS = [
    ("symbol", "品种", 94), ("side", "方向", 44), ("weight", "权重", 62),
    ("swap", "该腿息(USD/天)", 104), ("bid", "Bid", 84), ("ask", "Ask", 84),
    ("spread_pts", "点差", 70),
]
POS_COLUMNS = [
    ("symbol", "品种", 104), ("net_volume", "手数", 74),
    ("direction", "方向", 66), ("avg_price", "开仓均价", 94),
    ("price_current", "现价", 94), ("profit", "盈亏", 94), ("swap", "库存费", 84),
]
DEAL_COLUMNS = [
    ("time", "时间", 122), ("symbol", "品种", 104), ("direction", "方向", 54),
    ("entry", "开/平", 54), ("volume", "手数", 74), ("price", "成交价", 94),
    ("profit", "盈亏", 94), ("commission", "手续费", 84), ("swap", "库存费", 84),
    ("comment", "备注", 130),
]
SIGNAL_COLUMNS = [
    ("ts", "时间", 84), ("action", "动作", 66), ("side", "方向", 54),
    ("z", "Z", 64), ("count", "层数", 52), ("mode", "模式", 66),
    ("remark", "说明", 210),
]

open_consoles: dict[str, "DpgConsole"] = {}


def _f(v, default: float = 0.0) -> float:
    try:
        if v is None or isinstance(v, bool):
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _fmt(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        text = f"{v:.10f}".rstrip("0").rstrip(".")
        return text if text else "0"
    return str(v)


def _decorate_rule_row(row: dict) -> dict:
    from ..constants import SWAP_MODE_NAME, TRADE_MODE_NAME, WEEKDAY_NAME
    out = dict(row)
    out["symbol"] = out.get("symbol") or out.get("name", "")
    out["trade_mode"] = (TRADE_MODE_NAME.get(out.get("trade_mode"),
                                             _fmt(out.get("trade_mode"))))
    out["swap_mode"] = (SWAP_MODE_NAME.get(out.get("swap_mode"),
                                           _fmt(out.get("swap_mode"))))
    out["swap_rollover3days"] = WEEKDAY_NAME.get(
        out.get("swap_rollover3days"), _fmt(out.get("swap_rollover3days")))
    return out


def open_console(state, account: dict) -> None:
    key = f'{account["login"]}@{account["server"]}'
    view = open_consoles.get(key)
    if view is not None:
        dpg.show_item(view.tag_win)
        dpg.focus_item(view.tag_win)
        return
    open_consoles[key] = DpgConsole(state, account)


def close_console(key: str) -> None:
    view = open_consoles.pop(key, None)
    if view is not None:
        view.close()


def poll_all() -> None:
    now = time.time()
    for view in list(open_consoles.values()):
        view.poll(now)


class DpgConsole:
    """单账户控制台（state-first + DPG 2.3 全显式 parent/keyword）。"""

    def __init__(self, state, account: dict) -> None:
        self.state = state
        self.account = dict(account)
        self.session_name = f'{account["login"]}@{account["server"]}'
        self.session: AccountSession | None = None
        self.account_currency = "USD"

        self.rules_rows: list[dict] = []
        self.swaps_raw: list[dict] = []
        self.swaps_fetched_at: str | None = None
        self.swaps_fx: dict | None = None
        self.leg_meta: dict[str, dict] = {}
        self.opps: list[dict] = []
        self.legs: list[dict] = []
        self.runner = None
        self.hist_ready = False
        self.monitoring = False
        self.paper = True
        self.signals_rows: list[dict] = []
        self.last_ticks: dict[str, dict] = {}
        self.last_tick_at: float | None = None
        self.last_account: dict = {}
        self._line_t: list[float] = []
        self._line_v: list[float] = []
        self._c_t: list[float] = []
        self._c_o: list[float] = []
        self._c_h: list[float] = []
        self._c_l: list[float] = []
        self._c_c: list[float] = []
        self._candle_dirty_at = 0.0
        self._armed_until = 0.0
        self._log_lines: list[str] = []
        self._leg_row_tags: list = []
        self._bridges: list[tuple[str, object]] = []

        # 所有 dpg tag 走 defaultdict 自动生成（KeyError 免疫）
        self.T = collections.defaultdict(dpg.generate_uuid)

        with dpg.window(tag=self.T["win"],
                        label=f"控制台 — {self.session_name}",
                        width=1180, height=780,
                        on_close=self._on_window_close, pos=(40, 40)) as self.tag_win:
            self._build_inside_window()

        self._start_session()

    # ==================================================================
    # 构建（全部 on栈）
    # ==================================================================
    def _build_inside_window(self) -> None:
        T = self.T
        T["status"] = dpg.add_text(default_value="● 连接中…", color=th.C("WARN"))
        with dpg.group(horizontal=True):
            T["summary"] = dpg.add_text(default_value="-")
            dpg.add_spacer(width=18)
            T["mode"] = dpg.add_text(default_value="纸面", color=th.C("UP"))
            T["btn_session"] = dpg.add_button(label="断开会话", small=True,
                                              callback=self._toggle_session)

        T["tabbar"] = dpg.add_tab_bar()
        with dpg.tab(parent=T["tabbar"], label="规则 · 隔夜利息"):
            self._build_tab_data()
        with dpg.tab(parent=T["tabbar"], label="交易机会"):
            self._build_tab_opp()
        with dpg.tab(parent=T["tabbar"], label="策略控制台"):
            self._build_tab_strategy()

        # 事件桥注册
        self._bridges = []
        for etype, handler in (
            (EVENT_STATUS, self._on_status),
            (EVENT_ACCOUNT, self._on_account),
            (EVENT_RULES, self._on_rules),
            (EVENT_SWAPS, self._on_swaps),
            (EVENT_SPREAD_HISTORY, self._on_spread_history),
            (EVENT_POSITION, self._on_positions),
            (EVENT_TICK, self._on_tick),
            (EVENT_SPREAD_ALGO, self._on_algo),
            (EVENT_LOG, self._on_log),
        ):
            self.state.bridge.register(etype, handler)
            self._bridges.append((etype, handler))

    def _build_tab_data(self) -> None:
        T = self.T
        with dpg.group(horizontal=True):
            dpg.add_button(label="加载全部交易规则", small=True,
                           callback=lambda: self._sess_repeat(
                               lambda s: s.request_rules()))
            T["chk_watch"] = dpg.add_checkbox(label="自动检测利息变化",
                                              default_value=True,
                                              callback=self._toggle_watch)
            dpg.add_text(default_value="间隔")
            T["spin_interval"] = dpg.add_input_int(
                default_value=60, width=80, min_value=10, max_value=3600,
                callback=self._change_interval)
            dpg.add_text(default_value=th.format_ts(self.state.db.last_swap_change_time(self.session_name) if self.state.db else None, prefix="最近入库变更: "), color=th.C("TXT_MUTED"), tag=T["last_swap"])
            dpg.add_spacer(width=14)
            dpg.add_text(default_value="排序")
            self._sort_key = dpg.add_combo(
                items=("品种", "点差", "买入库存费", "最近更新"),
                default_value="品种", width=110, callback=self._refresh_data_table)
            T["sort_desc"] = dpg.add_checkbox(label="降序", default_value=True,
                                              callback=self._refresh_data_table)
            dpg.add_text(default_value="过滤")
            T["filter"] = dpg.add_input_text(width=150, hint="品种名…",
                                             callback=self._refresh_data_table)
        with dpg.table(tag=T["tbl_data"], header_row=True, resizable=True,
                       borders_innerH=True, borders_outerH=True,
                       row_background=True, height=-36):
            for _k, title, w in DATA_COLUMNS:
                dpg.add_table_column(label=title, init_width_or_weight=float(w),
                                     width_fixed=False)

    def _build_tab_opp(self) -> None:
        T = self.T
        with dpg.group(horizontal=True):
            dpg.add_button(label="由隔夜利息生成交易机会", small=True,
                           callback=self._generate_opportunities)
            dpg.add_text(default_value="最大腿数")
            T["spin_max_legs"] = dpg.add_input_int(default_value=5, width=70,
                                                   min_value=2, max_value=5)
            dpg.add_text(default_value="行尾 [载入] → 策略台",
                         color=th.C("TXT_MUTED"))
        with dpg.table(tag=T["tbl_opp"], header_row=True, resizable=True,
                       borders_innerH=True, borders_outerH=True,
                       row_background=True, height=-30):
            for _k, title, w in OPP_COLUMNS:
                dpg.add_table_column(label=title, init_width_or_weight=float(w),
                                     width_fixed=False)
            dpg.add_table_column(label="操作", init_width_or_weight=70,
                                 width_fixed=True)

    def _build_tab_strategy(self) -> None:
        T = self.T
        # ---- 上 Secretary：左图 + 右面板（7:3）----
        with dpg.group(horizontal=True):
            with dpg.child_window(autosize_x=True, height=400, border=False):
                dpg.add_text(default_value="价差（线 + 原生 GPU K 线叠加）",
                             color=th.C("TXT_MUTED"))
                with dpg.plot(tag=T["plot"], height=-1, width=-1):
                    T["xaxis"] = dpg.add_plot_axis(dpg.mvXAxis, label="时间")
                    with dpg.plot_axis(dpg.mvYAxis, tag=T["yaxis"]):
                        # 哨兵数据：空序列在渲染期存在段错误风险，预置 [0]point 保证非空
                        T["series_line"] = dpg.add_line_series(
                            [0], [0], label="价差", parent=T["yaxis"])
                        T["series_candle"] = dpg.add_candle_series(
                            [0], [0], [0], [0], [0], label="价差K线(H1)",
                            parent=T["yaxis"])
                        T["series_mean"] = dpg.add_line_series(
                            [0, 1], [0, 0], label="均值", parent=T["yaxis"])
                        T["series_upper"] = dpg.add_line_series(
                            [0, 1], [0, 0], label="+kσ", parent=T["yaxis"])
                        T["series_lower"] = dpg.add_line_series(
                            [0, 1], [0, 0], label="-kσ", parent=T["yaxis"])
            with dpg.child_window(width=self.state.th_right_width(), height=400,
                                  border=True):
                self._build_right_panel()

        # ---- 底部四子Tab ----
        T["subtab"] = dpg.add_tab_bar()
        with dpg.tab(parent=T["subtab"], label="持仓"):
            with dpg.table(tag=T["tbl_pos"], header_row=True, resizable=True,
                           borders_innerH=True, borders_outerH=True,
                           row_background=True, height=210):
                for _k, title, w in POS_COLUMNS:
                    dpg.add_table_column(label=title,
                                         init_width_or_weight=float(w),
                                         width_fixed=False)
        with dpg.tab(parent=T["subtab"], label="成交记录"):
            with dpg.group(horizontal=True):
                dpg.add_text(default_value="最近")
                T["deal_days"] = dpg.add_input_int(default_value=7, width=80,
                                                   min_value=1, max_value=90)
                dpg.add_button(label="加载成交记录", small=True,
                               callback=self._request_deals)
            with dpg.table(tag=T["tbl_deals"], header_row=True, resizable=True,
                           borders_innerH=True, borders_outerH=True,
                           row_background=True, height=170):
                for _k, title, w in DEAL_COLUMNS:
                    dpg.add_table_column(label=title,
                                         init_width_or_weight=float(w),
                                         width_fixed=False)
        with dpg.tab(parent=T["subtab"], label="信号"):
            with dpg.table(tag=T["tbl_signals"], header_row=True, resizable=True,
                           borders_innerH=True, borders_outerH=True,
                           row_background=True, height=210):
                for _k, title, w in SIGNAL_COLUMNS:
                    dpg.add_table_column(label=title,
                                         init_width_or_weight=float(w),
                                         width_fixed=False)
        with dpg.tab(parent=T["subtab"], label="日志"):
            T["log_view"] = dpg.add_input_text(multiline=True, readonly=True,
                                               default_value="", height=200,
                                               width=-1)

    def _build_right_panel(self) -> None:
        T = self.T
        T["lbl_opp"] = dpg.add_text(default_value="未加载交易机会（到【交易机会】页生成）")
        with dpg.table(tag=T["tbl_legs"], header_row=True, resizable=True,
                       borders_innerH=True, row_background=True, height=150):
            for _k, title, w in LEG_COLUMNS:
                dpg.add_table_column(label=title, init_width_or_weight=float(w),
                                     width_fixed=False)
        dpg.add_text(default_value="统计与网格", color=th.C("TXT_MUTEd") if False else th.C("TXT_MUTED"))
        with dpg.group(horizontal=True):
            with dpg.group():
                T["spin_window"] = dpg.add_input_int(label="Z窗口", default_value=500,
                                                     width=140, callback=self._on_params)
                T["spin_k"] = dpg.add_input_double(label="入场阈值 k",
                                                   default_value=2.0, width=140,
                                                   format="%.2f", callback=self._on_params)
                T["spin_grid"] = dpg.add_input_double(label="网格步进 σ",
                                                      default_value=0.5, width=140,
                                                      format="%.2f", callback=self._on_params)
            with dpg.group():
                T["spin_levels"] = dpg.add_input_int(label="网格层数", default_value=3,
                                                     width=140, min_value=1,
                                                     max_value=10, callback=self._on_params)
                T["spin_maxpos"] = dpg.add_input_int(label="最大持仓/方向",
                                                     default_value=10, width=140,
                                                     callback=self._on_params)
                T["spin_volume"] = dpg.add_input_double(label="每层手数(逐步小单)",
                                                        default_value=0.01, width=140,
                                                        format="%.2f", callback=self._on_params)
        self._chk_paper = dpg.add_checkbox(label="纸面模式（只记录不下单）",
                                           default_value=True,
                                           callback=self._on_paper_toggled)
        dpg.add_spacer(height=4)
        with dpg.group(horizontal=True):
            T["btn_start"] = dpg.add_button(label="▶ 开始监测", small=True,
                                            enabled=False, callback=self._on_start_clicked)
            dpg.add_button(label="重新加载历史", small=True, callback=self._load_history)
        with dpg.group(horizontal=True):
            T["btn_arm"] = dpg.add_button(label="💥 手动进场", small=True,
                                          enabled=False, callback=self._on_manual_click)
            dpg.add_text(default_value="待机 · 纸面 · 信号 0", tag=T["lbl_run"])
        dpg.add_text(default_value="待机", tag=T["lbl_stats"], color=th.C("TXT_MUTED"))

    # ==================================================================
    # 表格填充辅助
    # ==================================================================
    def _fill_leg_table(self, tag: str) -> None:
        """腿表填充 + 缓存行 tag（供 live 报价按行单元刷新）。"""
        dpg.clear_table(tag)
        self._leg_row_tags = []
        for leg in self.legs:
            row_tag = dpg.add_table_row(parent=tag)
            dpg.add_text(default_value=_fmt(leg["symbol"]), parent=row_tag)
            dpg.add_text(default_value="买" if leg.get("side", "buy") == "buy" else "卖",
                         parent=row_tag)
            dpg.add_text(default_value=_fmt(leg.get("weight")), parent=row_tag)
            dpg.add_text(default_value=_fmt(float(leg.get("swap", 0.0))), parent=row_tag)
            dpg.add_text(default_value="-", parent=row_tag)
            dpg.add_text(default_value="-", parent=row_tag)
            dpg.add_text(default_value="-", parent=row_tag)
            self._leg_row_tags.append(row_tag)

    def _fill_key_table(self, tag: str, columns, rows: list[dict],
                        with_button: bool = False) -> None:
        dpg.clear_table(tag)
        for row in rows:
            with dpg.table_row(parent=tag):
                for key, _t, _w in columns:
                    dpg.add_text(default_value=_fmt(row.get(key)))
                if with_button:
                    with dpg.group(horizontal=True):
                        dpg.add_button(label="载入", small=True,
                                       user_data={"legs_text": row.get("legs_text", "")},
                                       callback=self._opportunity_load)

    def _inject_table_rows(self, tag: str, columns, rows: list[dict]) -> None:
        dpg.clear_table(tag)
        for row in rows:
            with dpg.table_row(parent=tag):
                for key, _t, _w in columns:
                    dpg.add_text(default_value=_fmt(row.get(key)))

    # ==================================================================
    # 会话生命周期（与 Qt 版一致）
    # ==================================================================
    def _on_window_close(self, *_a, **_k) -> None:
        self.close()

    def close(self) -> None:
        self._session_stop()
        for etype, handler in getattr(self, "_bridges", []):
            self.state.bridge.unregister(etype, handler)
        if open_consoles.get(self.session_name) is self:
            open_consoles.pop(self.session_name, None)

    def _session_stop(self) -> None:
        session = self.session
        self.session = None
        if session is not None:
            def _stop_bg():
                try:
                    session.stop(close_terminal=False)
                except Exception:
                    pass
            threading.Thread(target=_stop_bg, name=f"stop-{self.session_name}",
                             daemon=True).start()
        dpg.set_value(self.T["status"], "● 断开")
        dpg.configure_item(self.T["status"], color=th.C("DOWN"))
        dpg.set_value(self.T["btn_session"], "连接账户")

    def _start_session(self) -> None:
        if self.session is not None:
            return
        self.session = AccountSession(self.state.event_engine, self.account)
        self.session.start()
        dpg.set_value(self.T["status"], "● 连接中…")
        dpg.configure_item(self.T["status"], color=th.C("WARN"))

    def _toggle_session(self, *_a) -> None:
        if self.session is not None:
            self._session_stop()
        else:
            self._start_session()

    def _sess_repeat(self, fn) -> None:
        if self.session is None:
            return
        fn(self.session)

    def _change_interval(self, *_a) -> None:
        if self.session is not None and dpg.get_value(self.T["chk_watch"]):
            self.session.watch_swaps(dpg.get_value(self.T["spin_interval"]))

    def _toggle_watch(self, *_a) -> None:
        if self.session is None:
            return
        if dpg.get_value(self.T["chk_watch"]):
            self.session.watch_swaps(dpg.get_value(self.T["spin_interval"]))
        else:
            self.session.unwatch_swaps()

    def _subscribe_legs(self) -> None:
        for leg in self.legs:
            self.state.event_engine.put(Event(
                EVENT_SYMBOL_SUBSCRIBE,
                {"session": self.session_name, "symbol": leg["symbol"]},
            ))

    # ==================================================================
    # 数据槽
    # ==================================================================
    def _is_self(self, event) -> bool:
        data = event.data or {}
        return isinstance(data, dict) and data.get("session") == self.session_name

    def _on_status(self, event) -> None:
        if not self._is_self(event):
            return
        payload = event.data.get("payload") or {}
        self.state.set_session_status(self.session_name, bool(payload.get("ok")),
                                      payload.get("msg", ""))
        if payload.get("ok"):
            dpg.set_value(self.T["status"], "● 已连接")
            dpg.configure_item(self.T["status"], color=th.C("UP"))
            dpg.set_value(self.T["btn_session"], "断开会话")
            if self.legs:
                self._subscribe_legs()
            if dpg.get_value(self.T["chk_watch"]):
                self._sess_repeat(
                    lambda s: s.watch_swaps(dpg.get_value(self.T["spin_interval"])))
        else:
            dpg.set_value(self.T["status"],
                          f"● 等待/重试 ({str(payload.get('msg', ''))[:24]})")
            dpg.configure_item(self.T["status"], color=th.C("WARN"))

    def _on_account(self, event) -> None:
        if not self._is_self(event):
            return
        self.last_account = event.data.get("payload") or {}
        currency = self.last_account.get("currency")
        if currency:
            self.account_currency = currency
        equity = _f(self.last_account.get("equity"))
        margin, margin_free = (_f(self.last_account.get("margin")),
                               _f(self.last_account.get("margin_free")))
        denom = margin + margin_free
        usage = margin / denom if denom > 0 else 0.0
        dpg.set_value(self.T["summary"],
                      f"净值 {equity:,.2f} · 使用率 {usage:.0%} · {currency}")

    def _on_rules(self, event) -> None:
        if not self._is_self(event):
            return
        self.rules_rows = list(event.data.get("payload", {}).get("items", []))
        self.leg_meta = {
            r.get("symbol", ""): {
                "min_volume": _f(r.get("volume_min"), 0.01) or 0.01,
                "volume_step": _f(r.get("volume_step"), 0.01) or 0.01,
            }
            for r in self.rules_rows if r.get("symbol")
        }
        self._refresh_data_table()

    def _on_swaps(self, event) -> None:
        if not self._is_self(event):
            return
        payload = event.data.get("payload") or {}
        self.swaps_raw = list(payload.get("all", []))
        self.swaps_fetched_at = payload.get("fetched_at")
        self.swaps_fx = payload.get("fx_rates")
        changed = (self.state.db.last_swap_change_time(self.session_name)
                   if self.state.db else None)
        dpg.set_value(self.T["last_swap"],
                      th.format_ts(changed, prefix="最近入库变更: "))
        self._refresh_data_table()

    def _refresh_data_table(self, *_a) -> None:
        from ..constants import SWAP_MODE_NAME, WEEKDAY_NAME
        rows = []
        swap_by = {s.get("symbol", ""): s for s in self.swaps_raw}
        known = set()
        text = str(dpg.get_value(self.T["filter"])).upper()
        for rule in self.rules_rows:
            out = _decorate_rule_row(rule)
            sym = out.get("symbol", "")
            known.add(sym)
            sw = swap_by.get(sym)
            if sw is not None:
                out["swap_long"] = _f(sw.get("swap_long"))
                out["swap_short"] = _f(sw.get("swap_short"))
                mode = sw.get("swap_mode")
                out["swap_mode"] = (SWAP_MODE_NAME.get(mode, "-")
                                    if isinstance(mode, int) else "-")
                roll = sw.get("rollover3days")
                out["swap_rollover3days"] = (WEEKDAY_NAME.get(roll, "-")
                                             if isinstance(roll, int) else "-")
                ut = self.state.db.last_swap_time(self.session_name, sym) if self.state.db else None
                out["update_time"] = ut or self.swaps_fetched_at or "-"
            else:
                out["update_time"] = "-"
            if text and text not in sym:
                continue
            rows.append(out)
        for sym, sw in swap_by.items():
            if sym in known:
                continue
            mode = sw.get("swap_mode")
            roll = sw.get("rollover3days")
            ut = self.state.db.last_swap_time(self.session_name, sym) if self.state.db else None
            rows.append({
                "symbol": sym, "swap_long": _f(sw.get("swap_long")),
                "swap_short": _f(sw.get("swap_short")),
                "swap_mode": SWAP_MODE_NAME.get(mode, "-") if isinstance(mode, int) else "-",
                "swap_rollover3days": (WEEKDAY_NAME.get(roll, "-")
                                       if isinstance(roll, int) else "-"),
                "update_time": ut or self.swaps_fetched_at or "-",
                "digits": None, "point": None, "spread": None, "trade_mode": None,
                "volume_min": None, "volume_step": None, "volume_max": None,
                "margin_initial": None,
            })
        keyname = str(dpg.get_value(self._sort_key))
        kmap = {"品种": "symbol", "点差": "spread",
                "买入库存费": "swap_long", "最近更新": "update_time"}
        field = kmap.get(keyname, "symbol")
        desc = bool(dpg.get_value(self.T["sort_desc"]))

        def sk(row):
            v = row.get(field)
            if field == "update_time":
                return str(v or "")
            if isinstance(v, (int, float)):
                return float(v)
            try:
                return float(v)
            except (TypeError, ValueError):
                return str(v or "")

        rows.sort(key=sk, reverse=desc)
        self._inject_table_rows(self.T["tbl_data"], DATA_COLUMNS, rows)

    # ------------------------------------------------------------------
    def _generate_opportunities(self, *_a) -> None:
        if not self.swaps_raw:
            return
        self.opps = generate(
            self.swaps_raw,
            fx_rates=self.swaps_fx,
            max_legs=dpg.get_value(self.T["spin_max_legs"]),
            cap=100,
            account_currency=self.account_currency,
        )
        rows = [{
            "net_swap": _f(o["net_swap"]),
            "legs_count": o["legs_count"],
            "legs_text": o["legs_text"],
        } for o in self.opps]
        self._fill_key_table(self.T["tbl_opp"], OPP_COLUMNS, rows,
                             with_button=True)
        if not self.opps:
            dpg.set_value(self.T["lbl_opp"],
                          "无可中性化组合（品种/汇率过滤后无候选）")

    def _opportunity_load(self, sender, app_data, user_data) -> None:
        text = (user_data or {}).get("legs_text", "")
        opp = next((o for o in self.opps if o["legs_text"] == text), None)
        if opp is None:
            return
        self._set_opportunity(opp)

    def _set_opportunity(self, opp: dict) -> None:
        self.legs = [dict(m) for m in opp["legs"]]
        self.runner = None
        self.hist_ready = False
        self._line_t = []
        self._line_v = []
        self._c_t = self._c_o = self._c_h = self._c_l = self._c_c = []
        dpg.set_value(self.T["series_line"], [0], [0])
        dpg.set_value(self.T["series_candle"], [0], [0], [0], [0], [0])
        dpg.set_value(self.T["series_mean"], [0, 1], [0, 0])
        dpg.set_value(self.T["series_upper"], [0, 1], [0, 0])
        dpg.set_value(self.T["series_lower"], [0, 1], [0, 0])
        dpg.set_value(self.T["lbl_opp"],
                      (f'净息估 {opp["net_swap"]:+.2f} USD/天 | '
                       f'{opp["legs_count"]} 腿 | {opp["legs_text"]}'))
        self._fill_leg_table(self.T["tbl_legs"])
        self._fill_leg_table(self.T["tbl_legs_exec"])
        self._subscribe_legs()
        self._ensure_runner()
        cleaned = [{"symbol": l["symbol"], "side": l.get("side", "buy"),
                    "weight": float(l.get("weight", 1.0))} for l in self.legs]
        self._sess_repeat(lambda s: s.request_spread_history(cleaned))
        dpg.configure_item(self.T["btn_start"], enabled=False)
        dpg.set_value(self.T["btn_start"], "▶ 开始监测")
        dpg.bind_item_enabled(self.T["btn_arm"], False)
        self._append_log(f"已载入组合并订阅 {len(self.legs)} 腿（等待历史价差…）")

    def _on_spread_history(self, event) -> None:
        if not self._is_self(event):
            return
        payload = event.data.get("payload") or {}
        datasets = payload.get("datasets", {})
        h1 = datasets.get("h1", {})
        if self.runner is None:
            return
        self.runner.set_history(h1)
        self.hist_ready = True
        tt, vv = self.runner.series.arrays()
        self._line_t = list(tt)
        self._line_v = list(vv)
        dpg.set_value(self.T["series_line"], tt, vv)
        candles = datasets.get("h1_candles")
        if candles:
            self._c_t = list(candles["t"])
            self._c_o = list(candles["o"])
            self._c_h = list(candles["h"])
            self._c_l = list(candles["l"])
            self._c_c = list(candles["c"])
            dpg.set_value(self.T["series_candle"],
                          self._c_t, self._c_o, self._c_c, self._c_l, self._c_h)
        dpg.configure_item(self.T["btn_start"], enabled=True)
        dpg.bind_item_enabled(self.T["btn_arm"], True)
        self._append_log(f"已加载历史价差 H1 × {len(vv)} 根（线+K线就绪）")

    def _on_positions(self, event) -> None:
        if not self._is_self(event):
            return
        raw = event.data.get("payload") or []
        merged: dict[str, dict] = {}
        for p in raw:
            sign = 1 if p.get("type") == 0 else -1     # POSITION_TYPE_BUY=0
            vol = _f(p.get("volume"))
            m = merged.setdefault(p["symbol"], {
                "symbol": p["symbol"], "_s": 0.0, "_a": 0.0, "_pv": 0.0,
                "price_current": _f(p.get("price_current")),
                "profit": 0.0, "swap": 0.0,
            })
            m["_s"] += sign * vol
            m["_a"] += vol
            m["_pv"] += _f(p.get("price_open")) * vol
            m["price_current"] = _f(p.get("price_current"))
            m["profit"] += _f(p.get("profit"))
            m["swap"] += _f(p.get("swap"))
        rows = []
        for m in merged.values():
            net = abs(m["_s"])
            direction = ("多" if m["_s"] > 0 else "空") if net > 1e-12 else "对冲"
            rows.append({
                "symbol": m["symbol"], "net_volume": net, "direction": direction,
                "avg_price": m["_pv"] / m["_a"] if m["_a"] else 0.0,
                "price_current": m["price_current"],
                "profit": m["profit"], "swap": m["swap"],
            })
        self._inject_table_rows(self.T["tbl_pos"], POS_COLUMNS, rows)

    def _request_deals(self, *_a) -> None:
        self._sess_repeat(
            lambda s: s.request_deals(dpg.get_value(self.T["deal_days"])))

    def _on_deals(self, event) -> None:
        if not self._is_self(event):
            return
        raw = event.data.get("payload") or []
        entry_name = {0: "开仓", 1: "平仓", 2: "翻转", 3: "对冲平仓"}
        rows = []
        for d in raw:
            ts = d.get("time")
            rows.append({
                "time": time.strftime("%m-%d %H:%M:%S", time.localtime(ts)) if ts else "-",
                "symbol": d.get("symbol", ""),
                "direction": "买" if d.get("type") == 0 else "卖",
                "entry": entry_name.get(d.get("entry"), "-"),
                "volume": _f(d.get("volume")), "price": _f(d.get("price")),
                "profit": _f(d.get("profit")),
                "commission": _f(d.get("commission")),
                "swap": _f(d.get("swap")),
                "comment": str(d.get("comment", "")),
            })
        self._inject_table_rows(self.T["tbl_deals"], DEAL_COLUMNS, rows)
        self._append_log(f"成交记录更新 {len(rows)} 笔")

    # ------------------------------------------------------------------
    def _on_tick(self, event) -> None:
        if not self._is_self(event):
            return
        for item in event.data.get("payload") or []:
            self.last_ticks[item["symbol"]] = item
        self.last_tick_at = time.time()
        self._update_leg_quotes()
        now = time.time()
        if now - self._candle_dirty_at >= 0.5:   # 2Hz 图表限频
            self._candle_dirty_at = now
            self._flush_chart()

    def _update_leg_quotes(self) -> None:
        if not self._leg_row_tags:
            return
        col = {c[0]: i for i, c in enumerate(LEG_COLUMNS)}
        for idx, leg in enumerate(self.legs):
            tick = self.last_ticks.get(leg["symbol"])
            if tick is None or idx >= len(self._leg_row_tags):
                continue
            cells = dpg.get_item_children(self._leg_row_tags[idx], 1)
            if not cells or len(cells) < len(LEG_COLUMNS):
                continue
            bid, ask = _f(tick.get("bid")), _f(tick.get("ask"))
            dpg.set_value(cells[col["bid"]], f"{bid:.5f}")
            dpg.set_value(cells[col["ask"]], f"{ask:.5f}")
            dpg.set_value(cells[col["spread_pts"]], f"{ask - bid:.5f}")

    def _flush_chart(self) -> None:
        if not self._line_t:
            return
        if self._line_t:
            n = min(len(self._line_t), 4000)
            dpg.set_value(self.T["series_line"],
                          self._line_t[-n:], self._line_v[-n:])
        if self._c_t:
            n = min(len(self._c_t), 4000)
            dpg.set_value(self.T["series_candle"],
                          self._c_t[-n:], self._c_o[-n:], self._c_c[-n:],
                          self._c_l[-n:], self._c_h[-n:])
        self._push_thresholds()

    def _on_algo(self, event) -> None:
        if not self._is_self(event):
            return
        p = event.data.get("payload") or {}
        self._append_log(
            f"[价差算法 {p.get('algoid')}] [{p.get('stage')}] {p.get('msg')}")
        if p.get("stage") == "failed":
            self._append_log("⚠ 裸腿暴露风险——请立即人工检查持仓")

    def _on_log(self, event) -> None:
        if not self._is_self(event):
            return
        p = event.data.get("payload") or {}
        self._append_log(f"[{p.get('level')}] {p.get('source')} | {p.get('msg')}")

    def _append_log(self, msg: str) -> None:
        self._log_lines.append(f"{time.strftime('%H:%M:%S')} {msg}")
        self._log_lines = self._log_lines[-500:]
        dpg.set_value(self.T["log_view"], "\n".join(self._log_lines))

    # ------------------------------------------------------------------
    # 策略闭环
    # ------------------------------------------------------------------
    def _ensure_runner(self) -> bool:
        if not self.legs:
            return False
        if self.runner is None:
            self.runner = StrategyRunner(
                self.session_name, self.legs, {
                    "window": dpg.get_value(self.T["spin_window"]),
                    "k": dpg.get_value(self.T["spin_k"]),
                    "grid_step": dpg.get_value(self.T["spin_grid"]),
                    "levels": dpg.get_value(self.T["spin_levels"]),
                    "max_positions": dpg.get_value(self.T["spin_maxpos"]),
                })
        return True

    def _load_history(self, *_a) -> None:
        if not self._ensure_runner():
            return
        cleaned = [{"symbol": l["symbol"], "side": l.get("side", "buy"),
                    "weight": float(l.get("weight", 1.0))} for l in self.legs]
        self._sess_repeat(lambda s: s.request_spread_history(cleaned))

    def _on_params(self, *_a) -> None:
        if self.runner is None:
            return
        self.runner.params.update({
            "window": dpg.get_value(self.T["spin_window"]),
            "k": dpg.get_value(self.T["spin_k"]),
            "grid_step": dpg.get_value(self.T["spin_grid"]),
            "levels": dpg.get_value(self.T["spin_levels"]),
            "max_positions": dpg.get_value(self.T["spin_maxpos"]),
        })
        self.runner.series.window = dpg.get_value(self.T["spin_window"])
        sig = self.runner.signal
        sig.k = dpg.get_value(self.T["spin_k"])
        sig.grid_step = dpg.get_value(self.T["spin_grid"])
        sig.levels = dpg.get_value(self.T["spin_levels"])
        sig.max_positions = dpg.get_value(self.T["spin_maxpos"])
        self._push_thresholds()

    def _on_paper_toggled(self, *_a) -> None:
        self.paper = bool(dpg.get_value(self.T["chk_paper"]))
        dpg.set_value(self.T["mode"],
                      "纸面" if self.paper else "实盘")
        dpg.configure_item(self.T["mode"],
                           color=th.C("UP") if self.paper else th.C("DOWN"))

    def _push_thresholds(self) -> None:
        if self.runner is None:
            return
        series = self.runner.series
        if series.live_points() < 10 and self.runner.prior:
            stats = {"mean": self.runner.prior["mean"],
                     "std": self.runner.prior["std"]}
        else:
            stats = series.stats() or {}
        if not stats:
            return
        mean, std = stats["mean"], stats["std"]
        k = dpg.get_value(self.T["spin_k"])
        up = mean + k * std
        lo = mean - k * std
        if self._line_t:
            t0, t1 = self._line_t[0], self._line_t[-1] + 60
        else:
            t0, t1 = 0, 1
        dpg.set_value(self.T["series_mean"], [t0, t1], [mean, mean])
        dpg.set_value(self.T["series_upper"], [t0, t1], [up, up])
        dpg.set_value(self.T["series_lower"], [t0, t1], [lo, lo])

    def _candle_aggregate(self, ts: float, price: float) -> None:
        """live tick → 当前 H1 蜡烛聚合（open 保留，h/l/c 刷新，跨小时新增）。"""
        if self._c_t:
            last = self._c_t[-1]
            if math.floor(ts / _H1) * _H1 > last:
                self._c_t.append(math.floor(ts / _H1) * _H1)
                self._c_o.append(price)
                self._c_h.append(price)
                self._c_l.append(price)
                self._c_c.append(price)
            else:
                self._c_h[-1] = max(self._c_h[-1], price)
                self._c_l[-1] = min(self._c_l[-1], price)
                self._c_c[-1] = price
        self._candle_dirty_at = 0.0

    def poll(self, now: float) -> None:
        """主循环帧回调：报价刷新（5Hz）+ 武装复位 + live 策略消费。"""
        if self.legs and getattr(self, "_leg_refresh_at", 0.0) + 0.2 < now:
            self._leg_refresh_at = now
            self._update_leg_quotes()
        if getattr(self, "_armed_until", 0.0) and now >= self._armed_until:
            self._armed_until = 0.0
            dpg.set_value(self.T["btn_arm"], "💥 手动进场")
            self._append_log("手动进场已复位（未执行）")
        if self.monitoring and self.runner is not None:
            symbols = {l["symbol"] for l in self.legs}
            if self.last_tick_at and all(s in self.last_ticks for s in symbols):
                result = self.runner.on_tick({s: self.last_ticks[s] for s in symbols})
                if result:
                    self._handle_strategy(result)

    def _handle_strategy(self, result: dict) -> None:
        self._line_t.append(result["ts"])
        self._line_v.append(result["spread"])
        n = min(len(self._line_t), 4000)
        dpg.set_value(self.T["series_line"],
                      self._line_t[-n:], self._line_v[-n:])
        self._candle_aggregate(result["ts"], result["spread"])
        n = min(len(self._c_t), 4000)
        if n:
            dpg.set_value(self.T["series_candle"],
                          self._c_t[-n:], self._c_o[-n:], self._c_c[-n:],
                          self._c_l[-n:], self._c_h[-n:])
        self._push_thresholds()
        ext = result.get("extremes") or {}
        stats = result.get("stats") or {}

        def f5(v):
            return "-" if v is None else f"{float(v):+.5f}"

        keys = ("max_1y", "min_1y", "max_6m", "min_6m")
        ext_line = ("极值待数据"
                    if all(ext.get(k) is None for k in keys) else
                    (f"1y [{f5(ext.get('max_1y'))},{f5(ext.get('min_1y'))}] "
                     f"6m [{f5(ext.get('max_6m'))},{f5(ext.get('min_6m'))}]"))
        prior_mark = " (先验)" if stats.get("prior") else ""
        dpg.set_value(self.T["lbl_stats"],
                      (f"价差 {f5(result['spread'])} | z {f5(stats.get('z'))} | "
                       f"mean {f5(stats.get('mean'))} ± {f5(stats.get('std'))}"
                       f"{prior_mark}\n{ext_line}"))
        signal = result.get("signal")
        if signal:
            ts = dt.datetime.fromtimestamp(result["ts"]).strftime("%H:%M:%S")
            self._append_signal(ts, signal, result.get("hedge"))

    def _append_signal(self, ts, signal, hedge) -> None:
        mode = "paper" if self.paper else "LIVE"
        vol_per_level = dpg.get_value(self.T["spin_volume"])
        if signal["action"] == "open":
            remark = f"第{signal.get('count', 1)}层 · 本层 {vol_per_level:.2f} 手/腿"
        elif signal["action"] == "close":
            remark = f"平 {signal.get('count', 0)} 层（z 回归）"
        else:
            remark = "仓位已满"
        self.signals_rows.append({
            "ts": ts, "action": signal["action"], "side": signal.get("side", ""),
            "z": signal.get("z", ""), "count": signal.get("count", ""),
            "mode": mode, "remark": f"{remark} | {signal.get('remark', '')}",
        })
        self._inject_table_rows(self.T["tbl_signals"], SIGNAL_COLUMNS,
                                list(reversed(self.signals_rows)))
        dpg.set_value(self.T["lbl_run"],
                      (f"{'监测中' if self.monitoring else '待机'} · "
                       f"{'纸面' if self.paper else '实盘'} · "
                       f"信号 {len(self.signals_rows)}"))
        if signal["action"] in ("open", "close") and self.state.db:
            legs_desc = [{"symbol": h["symbol"], "side": h["side"],
                          "weight": h["weight"]} for h in hedge]
            self.state.db.insert_trade(
                self.session_name, time.strftime("%Y-%m-%d %H:%M:%S"), mode,
                signal["action"], self.session_name, legs_desc,
                {"z": signal.get("z"), "count": signal.get("count")},
            )
        if not self.paper and self.monitoring and signal["action"] == "open":
            self._dispatch_algo(signal)

    def _dispatch_algo(self, signal: dict) -> None:
        """实盘：自动价差算法（主动腿先吃→被动腿对冲；每层一单一算法）。"""
        quotes = {
            sym: {"bid": _f(t.get("bid")), "ask": _f(t.get("ask"))}
            for sym, t in self.last_ticks.items()
        }
        elegs = build_legs_from_opportunity(
            {"legs": self.legs}, quotes,
            dpg.get_value(self.T["spin_volume"]),
            getattr(self, "leg_meta", None),
        )
        payload = [{"symbol": l.symbol, "side": l.side,
                    "volume": l.multiplier} for l in elegs]
        self.state.event_engine.put(Event(EVENT_ALGO_REQUEST, {
            "session": self.session_name, "legs": payload,
            "comment": f"strat-{signal['action']}",
        }))
        self._append_log(
            "价差算法已下发: "
            + " | ".join(f"{l.side}:{l.symbol}×{l.multiplier}" for l in elegs))

    # ------------------------------------------------------------------
    def _on_start_clicked(self, *_a) -> None:
        if self.monitoring:
            self.monitoring = False
            dpg.set_value(self.T["btn_start"], "▶ 开始监测")
            dpg.set_value(self.T["lbl_run"],
                          f"待机 · {'纸面' if self.paper else '实盘'} · "
                          f"信号 {len(self.signals_rows)}")
            self._append_log("监测已停止")
            return
        if not self._ensure_runner() or not self.hist_ready:
            return
        self.monitoring = True
        dpg.set_value(self.T["btn_start"], "■ 停止监测")
        dpg.set_value(self.T["lbl_run"],
                      f"监测中 · {'纸面' if self.paper else '实盘'} · "
                      f"信号 {len(self.signals_rows)}")
        self._append_log("自动网格监测已启动（"
                         + ("纸面·分层小单" if self.paper else "实盘! 每层即发单")
                         + "）")

    def _on_manual_click(self, *_a) -> None:
        now = time.time()
        if self._armed_until and now < self._armed_until:
            self._armed_until = 0.0
            dpg.set_value(self.T["btn_arm"], "💥 手动进场")
            signal = {"action": "open", "side": "buy", "count": 1,
                      "level": 0, "z": 0}
            self._append_signal(time.strftime("%H:%M:%S"), signal, [])
        else:
            self._armed_until = now + 3.0
            dpg.set_value(self.T["btn_arm"], "确认进场(3s)")
