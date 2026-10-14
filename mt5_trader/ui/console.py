"""账户控制台窗口（回归合理骨架 + 样式升级）。

顶栏：状态灯 + 账户摘要 + 断开/连接按钮
Tab1 规则·隔夜利息：交易规则与库存费合并为一张【可排序】表（表头点击排序）
Tab2 交易机会：生成候选组合（USD 中性权重 + 净息 USD/天），双击载入策略台
Tab3 策略控制台：左侧实时价差图（常驻可见，无需切页），右侧功能操作区，
     底部持仓/信号/日志 —— 与原合理布局一致；信号 = 自动网格分层小单进场。

每账户一个窗口，独立会话（子进程）。
"""

from __future__ import annotations

import datetime as dt
import threading
import time
from typing import NamedTuple

from PySide6 import QtCore, QtGui, QtWidgets

from ..account_session import AccountSession
from ..accounts import AccountsStore
from ..constants import (
    EVENT_ACCOUNT,
    EVENT_ALGO_REQUEST,
    EVENT_DEAL,
    EVENT_DEAL_REQUEST,
    EVENT_LOG,
    EVENT_ORDER_REQUEST,
    EVENT_POSITION,
    EVENT_RULES,
    EVENT_SPREAD_ALGO,
    EVENT_SPREAD_HISTORY,
    EVENT_STATUS,
    EVENT_SWAPS,
    EVENT_SYMBOL_SUBSCRIBE,
    EVENT_TICK,
)
from ..database import Database
from ..event_engine import Event
from ..opportunities import generate
from ..strategy import StrategyRunner
from .base import EventBridge
from .chart import SpreadChart
from .kpi import StatusPill
from .notify import ToastManager
from .theme import COLORS

# money 列三态配色（与 theme.UP/DOWN/TXT_MUTED 同值，独立常量避免循环依赖）
_C_MONEY_UP = "#16c784"
_C_MONEY_DOWN = "#ea3943"
_C_MONEY_ZERO = "#8b93a3"

_MONO_FONT = "Consolas"


class _CallInMain(QtCore.QObject):
    """跨线程回调桥：worker 线程 emit(call, fn)，主线程槽内执行 fn。

    Qt 自动把跨线程 emit 转为 QueuedConnection，保证 fn 在主线程执行（UI 安全）。
    """

    call = QtCore.Signal(object)


class ColumnSpec(NamedTuple):
    """MiniTable 列定义。

    :param key: 行 dict 的键
    :param title: 表头标题
    :param kind: sym/int/px/money/pct/enum/time，决定对齐/字体/着色/排序口径
    :param width: 默认列宽（Interactive，用户可拖动调整）
    """

    key: str
    title: str
    kind: str = "sym"
    width: int = 90


# ---------------------------------------------------------------------------
# 合并数据表：交易规则 + 隔夜利息（一张表，可排序）
# ---------------------------------------------------------------------------
DATA_COLUMNS = [
    ColumnSpec("symbol", "品种", "sym", 110),
    ColumnSpec("digits", "小数位", "int", 60),
    ColumnSpec("point", "最小变动点", "px", 86),
    ColumnSpec("spread", "当前点差", "px", 76),
    ColumnSpec("trade_mode", "交易模式", "enum", 88),
    ColumnSpec("volume_min", "最小手", "px", 66),
    ColumnSpec("volume_step", "步长", "px", 66),
    ColumnSpec("volume_max", "最大手", "px", 76),
    ColumnSpec("margin_initial", "初始保证金", "money", 100),
    ColumnSpec("swap_long", "买入库存费", "money", 96),
    ColumnSpec("swap_short", "卖出库存费", "money", 96),
    ColumnSpec("swap_rollover3days", "三倍息日", "enum", 80),
    ColumnSpec("swap_mode", "库存费机制", "enum", 116),
    ColumnSpec("update_time", "最近更新", "time", 132),
]

OPP_COLUMNS = [
    ColumnSpec("net_swap", "净息/天(USD·组合)", "money", 130),
    ColumnSpec("legs_count", "腿数", "int", 56),
    ColumnSpec("legs_text", "组合", "sym", 420),
]

LEG_COLUMNS = [
    ColumnSpec("symbol", "品种", "sym", 90),
    ColumnSpec("side", "方向", "enum", 44),
    ColumnSpec("weight", "权重", "px", 60),
    ColumnSpec("swap", "该腿息(USD/天)", "money", 100),
    ColumnSpec("bid", "Bid", "px", 84),
    ColumnSpec("ask", "Ask", "px", 84),
    ColumnSpec("spread_pts", "点差", "px", 70),
    ColumnSpec("fl_pnl", "浮盈(USD)", "money", 90),
]

# 持仓（按品种合并的净中仓；Tab 名就叫「持仓」）
POSITION_COLUMNS = [
    ColumnSpec("symbol", "品种", "sym", 110),
    ColumnSpec("net_volume", "手数", "px", 80),
    ColumnSpec("direction", "方向", "enum", 70),
    ColumnSpec("avg_price", "开仓均价", "px", 100),
    ColumnSpec("price_current", "现价", "px", 100),
    ColumnSpec("profit", "盈亏", "money", 100),
    ColumnSpec("swap", "库存费", "money", 90),
]

# 成交记录（history_deals_get）
DEAL_COLUMNS = [
    ColumnSpec("time", "时间", "time", 130),
    ColumnSpec("symbol", "品种", "sym", 110),
    ColumnSpec("direction", "方向", "enum", 60),
    ColumnSpec("entry", "开/平", "enum", 60),
    ColumnSpec("volume", "手数", "px", 80),
    ColumnSpec("price", "成交价", "px", 100),
    ColumnSpec("profit", "盈亏", "money", 100),
    ColumnSpec("commission", "手续费", "money", 90),
    ColumnSpec("swap", "库存费", "money", 90),
    ColumnSpec("comment", "备注", "sym", 140),
]

SIGNAL_COLUMNS = [
    ColumnSpec("ts", "时间", "time", 90),
    ColumnSpec("action", "动作", "enum", 70),
    ColumnSpec("side", "方向", "enum", 60),
    ColumnSpec("z", "Z", "px", 70),
    ColumnSpec("count", "层数", "int", 56),
    ColumnSpec("mode", "模式", "enum", 70),
    ColumnSpec("remark", "说明", "sym", 200),
]


def _fmt_value(value) -> str:
    """通用格式化：浮点禁止科学计数法。"""
    if value is None:
        return "-"
    if isinstance(value, float):
        text = f"{value:.12f}".rstrip("0").rstrip(".")
        return text if text else "0"
    return str(value)


def _f(v, default: float = 0.0) -> float:
    """payload 数值安全取值：None/非法 -> default。"""
    try:
        if v is None or isinstance(v, bool):
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


def _compact_ts(text: str | None) -> str | None:
    """'2026-02-03 12:22:07' -> '02-03 12:22'（顶栏小字）。"""
    if not text:
        return None
    return text[5:16] if len(text) >= 16 else text


def _mk(mapping: dict[int, str], flags_value) -> str:
    if not isinstance(flags_value, int):
        return _fmt_value(flags_value)
    return mapping.get(flags_value, f"代码 {flags_value}")


def _decorate_rule_row(row: dict) -> dict:
    """规则 payload 行 -> 枚举中文化展示 dict。"""
    from ..constants import (
        TRADE_MODE_NAME, WEEKDAY_NAME, SWAP_MODE_NAME,
        expiration_mode_bits,
    )
    out = dict(row)
    out.setdefault("symbol", row.get("name", ""))
    out["symbol"] = out.get("symbol") or out.get("name", "")
    out["trade_mode"] = _mk(TRADE_MODE_NAME, row.get("trade_mode"))
    out["swap_mode"] = _mk(SWAP_MODE_NAME, row.get("swap_mode"))
    out["swap_rollover3days"] = _mk(WEEKDAY_NAME, row.get("swap_rollover3days"))
    out["expiration_mode"] = (
        expiration_mode_bits(row.get("expiration_mode"))
        if isinstance(row.get("expiration_mode"), int)
        else _fmt_value(row.get("expiration_mode"))
    )
    return out


class _MoneyDelegate(QtWidgets.QStyledItemDelegate):
    """money 列三态着色：正 #16c784 / 负 #ea3943 / 零 #8b93a3。"""

    def __init__(self, money_columns: set[int], parent=None) -> None:
        super().__init__(parent)
        self._money_columns = money_columns

    def initStyleOption(self, option, index) -> None:  # noqa: N802
        super().initStyleOption(option, index)
        if index.column() not in self._money_columns:
            return
        text = index.data(QtCore.Qt.ItemDataRole.DisplayRole)
        text = text if isinstance(text, str) else ""
        if text.startswith("+"):
            color = _C_MONEY_UP
        elif text.startswith("-"):
            color = _C_MONEY_DOWN
        else:
            color = _C_MONEY_ZERO
        option.palette.setColor(
            QtGui.QPalette.ColorRole.Text, QtGui.QColor(color)
        )


_NUM_ROLE = QtCore.Qt.ItemDataRole.UserRole + 1   # 数值排序键（EditRole 会被 Qt 转字符串）


class _SortItem(QtWidgets.QTableWidgetItem):
    """可排序单元格：数值列按 UserRole 存的 float 键比较，其余按显示文本。
    （坑：覆写 __lt__ 不可调 super().__lt__（递归爆栈）；EditRole 存 float 会被
     Qt QString 化 —— 所以用 UserRole。）"""

    def __lt__(self, other: QtWidgets.QTableWidgetItem) -> bool:  # noqa: N802
        a = self.data(_NUM_ROLE)
        b = other.data(_NUM_ROLE)
        if a is not None and b is not None:
            try:
                return float(a) < float(b)
            except (TypeError, ValueError):
                pass
        return self.text() < other.text()


class MiniTable(QtWidgets.QTableWidget):
    """轻量表：list[dict] 填充 + 列规格 + 表头点击排序 + 宽度自适应铺满。

    - 列宽默认按 ColumnSpec.width 比例分配，且【始终填满视口宽度】
      （右侧不留空白）；用户拖动某列时：该列保留拖动宽度，其余列按
      比例伸缩继续铺满。
    - 行 dict 可带 "_tooltip" 键 → 该行所有单元格的悬浮提示。
    """

    def __init__(self, columns: list[ColumnSpec], parent=None) -> None:
        super().__init__(0, len(columns), parent)
        self._cols = list(columns)
        self.setHorizontalHeaderLabels([c.title for c in self._cols])
        self.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        self.verticalHeader().setVisible(False)
        self.setAlternatingRowColors(True)   # 斑马纹（QSS alternate-background-color）
        header = self.horizontalHeader()
        header.setSectionResizeMode(QtWidgets.QHeaderView.Interactive)  # 用户可拖动调宽
        header.setStretchLastSection(False)
        header.setMinimumSectionSize(40)
        header.setSortIndicatorShown(True)
        self._base = [max(spec.width, 40) for spec in self._cols]  # 用户拖动后的基准宽
        self._filling = False
        header.sectionResized.connect(self._on_section_resized)
        for c, w in enumerate(self._base):
            header.resizeSection(c, w)
        money_cols = {i for i, c in enumerate(self._cols) if c.kind == "money"}
        if money_cols:
            self.setItemDelegate(_MoneyDelegate(money_cols, self))
        # 排序完全自管：不启用 setSortingEnabled（它一开就按指示器重排，
        # 会破坏"写入顺序默认展示 + 用户点表头才排序"的语义）。
        # 用 sectionClicked -> 自己维护指示器与 sortItems。
        self.setSortingEnabled(False)
        self._sort_active = False
        self._sort_col = 0
        self._sort_asc = True
        header.sectionClicked.connect(self._on_header_clicked)

    def _on_header_clicked(self, col: int) -> None:
        """用户点击表头：同列再点切换升/降序；切换列回到升序；随即排序。"""
        if self._sort_active and self._sort_col == col:
            self._sort_asc = not self._sort_asc
        else:
            self._sort_col = col
            self._sort_asc = True
        self._sort_active = True
        self.horizontalHeader().setSortIndicator(
            col,
            QtCore.Qt.SortOrder.AscendingOrder
            if self._sort_asc
            else QtCore.Qt.SortOrder.DescendingOrder,
        )
        self.sortItems(
            col,
            QtCore.Qt.SortOrder.AscendingOrder
            if self._sort_asc
            else QtCore.Qt.SortOrder.DescendingOrder,
        )

    # ------------------------------------------------------------------
    # 宽度自适应：始终填满视口
    # ------------------------------------------------------------------
    def resizeEvent(self, e) -> None:  # noqa: N802 (Qt 约定)
        super().resizeEvent(e)
        self._distribute()

    def showEvent(self, e) -> None:  # noqa: N802 (Qt 约定)
        super().showEvent(e)
        QtCore.QTimer.singleShot(0, self._distribute)

    def _auto_fit_columns(self) -> None:
        """列宽自适应：列宽不应小于表头 / 单元格光学宽度（中文按主题字体、
        数字按 Consolas 计宽）+ padding；开用户拖动时基准取 max(用户调整, 内容)"""
        from PySide6 import QtCore as _qc
        from PySide6 import QtGui as _qg
        fm_text = _qg.QFontMetrics(self.font())
        fm_mono = _qg.QFontMetrics(_qg.QFont("Consolas"))
        hdr_fm = _qg.QFontMetrics(self.horizontalHeader().font())
        header_pad = 26   # 表头 padding + 排序指示器
        cell_pad = 18     # 单元格 padding
        for c, spec in enumerate(self._cols):
            w = hdr_fm.horizontalAdvance(spec.title) + header_pad
            for r in range(self.rowCount()):
                item = self.item(r, c)
                if item is None:
                    continue
                fm = fm_mono if spec.kind in ("int", "px", "money", "pct") else fm_text
                w = max(w, fm.horizontalAdvance(item.text()) + cell_pad)
            self._base[c] = max(self._base[c], min(w, 600))
        self._distribute()

    def _on_section_resized(self, col: int, old: int, new: int) -> None:
        if self._filling:
            return
        self._base[col] = max(new, 40)   # 用户拖动 -> 更新基准，其余列按比例继续铺满
        self._distribute(exclude=col)

    def _distribute(self, exclude: int | None = None) -> None:
        """把视口宽度按 _base 分配到各列（exclude 列保持当前宽）。

        视口足够时按比例铺满；不够时改为保持内容所需宽度（横向滚动），
        否则比例分摊会把中文表头/数字列压窄导致截断。"""
        if self._filling or not self._cols:
            return
        vp = self.viewport().width() - 4
        if vp <= len(self._base) * 40:
            return
        self._filling = True
        try:
            total = sum(self._base[c] for c in range(len(self._base)))
            if total <= vp:
                fixed = self._base[exclude] if exclude is not None else 0
                others = [c for c in range(len(self._base)) if c != exclude]
                pool = max(vp - fixed, len(others) * 40)
                others_total = sum(self._base[c] for c in others) or 1
                for c in others:
                    w = max(40, round(pool * self._base[c] / others_total))
                    self.setColumnWidth(c, w)
            else:
                # 内容宽于视口：保宽 + 横向滚动（不压宽数字/中文列）
                for c in range(len(self._base)):
                    if c != exclude:
                        self.setColumnWidth(c, self._base[c])
        finally:
            self._filling = False

    def set_rows(self, rows: list[dict]) -> None:
        # 排序关闭 -> 填充 -> 恢复（用户点过表头才重新排序，否则保持写入顺序）
        self.setSortingEnabled(False)
        self.setRowCount(len(rows))
        for r, row in enumerate(rows):
            tooltip = row.get("_tooltip", "")
            for c, spec in enumerate(self._cols):
                item = self._make_item(row.get(spec.key), spec)
                if tooltip:
                    item.setToolTip(tooltip)
                self.setItem(r, c, item)
        if self._sort_active:   # 用户点过表头 -> 维持其排序；否则保持写入顺序
            self.sortItems(
                self._sort_col,
                QtCore.Qt.SortOrder.AscendingOrder
                if self._sort_asc
                else QtCore.Qt.SortOrder.DescendingOrder,
            )

    def _make_item(self, raw, spec: ColumnSpec) -> QtWidgets.QTableWidgetItem:
        kind = spec.kind
        if raw is None:
            text = "-"
        elif kind == "money" and isinstance(raw, (int, float)) and not isinstance(raw, bool):
            text = f"{raw:+,.2f}"
        elif isinstance(raw, str):
            text = raw
        else:
            text = _fmt_value(raw)
        item = _SortItem(text)
        # 数值排序键存 UserRole（EditRole 会被 Qt 转字符串，排序比较用）
        if kind in ("int", "px", "money", "pct") and isinstance(raw, (int, float)) \
                and not isinstance(raw, bool):
            item.setData(_NUM_ROLE, float(raw))
        if kind == "sym":
            align = QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter
        elif kind in ("int", "px", "money", "pct"):
            align = (
                QtCore.Qt.AlignmentFlag.AlignRight
                | QtCore.Qt.AlignmentFlag.AlignVCenter
            )
            item.setFont(QtGui.QFont(_MONO_FONT))
        elif kind == "enum":
            align = QtCore.Qt.AlignmentFlag.AlignCenter
        elif kind == "time":
            align = (
                QtCore.Qt.AlignmentFlag.AlignRight
                | QtCore.Qt.AlignmentFlag.AlignVCenter
            )
        else:
            align = QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignVCenter
        item.setTextAlignment(align)
        return item


class ArmedButton(QtWidgets.QPushButton):
    """两步确认按钮：首击进入待发态（红字"确认进场(3s)"+ beep），3s 内再击
    发 sig_fire 信号；超时自动复位。"""

    sig_fire = QtCore.Signal()

    ARMED_TEXT = "确认进场(3s)"
    TIMEOUT_MS = 3000

    def __init__(self, label: str, parent=None) -> None:
        super().__init__(label, parent)
        self._label = label
        self._armed = False
        self._timer = QtCore.QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(self.TIMEOUT_MS)
        self._timer.timeout.connect(self.reset)
        self.clicked.connect(self._on_clicked)

    @property
    def armed(self) -> bool:
        return self._armed

    def _on_clicked(self) -> None:
        if not self._armed:
            self.arm()
        else:
            self.reset()
            self.sig_fire.emit()

    def arm(self) -> None:
        self._armed = True
        self.setText(self.ARMED_TEXT)
        self.setStyleSheet(
            f"QPushButton {{ border: 1px solid {COLORS['DOWN']};"
            f" color: {COLORS['DOWN']}; font-weight: bold; }}"
        )
        try:
            QtWidgets.QApplication.beep()
        except Exception:
            pass
        self._timer.start()

    def reset(self) -> None:
        self._timer.stop()
        self._armed = False
        self.setText(self._label)
        self.setStyleSheet("")


class RatioSplitter(QtWidgets.QSplitter):
    """按固定比例分配宽度的 Splitter：窗口尺寸变化时保持比例（如图表:控制台 = 7:3）。"""

    def __init__(self, ratio: float, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._ratio = max(0.1, min(0.9, ratio))

    def resizeEvent(self, e) -> None:  # noqa: N802 (Qt 约定)
        super().resizeEvent(e)
        w = self.width() - max(self.count() - 1, 0) * self.handleWidth()
        if self.count() == 2 and w > 80:
            self.setSizes([int(w * self._ratio), int(w * (1 - self._ratio))])


class AccountConsole(QtWidgets.QMainWindow):
    """单账户控制台独立窗口（原合理骨架 + 样式升级）。"""

    # 信号行上限（_signals_rows 无界增长会拖慢 set_rows 全量重建）
    _SIGNALS_CAP = 600
    def __init__(self, event_engine, account: dict, store: AccountsStore,
                 db: Database, on_closed=None) -> None:
        super().__init__()
        self._ee = event_engine
        self._account = account
        self._store = store
        self._db = db
        self._on_closed = on_closed
        self._session: AccountSession | None = None
        self._last_ticks: dict = {}
        self._last_tick_at: float | None = None
        self._last_account: dict | None = None
        self._current_legs: list | None = None
        self._monitoring = False
        self._signals_rows: list = []
        self._runner = None
        self._hist_ready = False
        self._account_currency = "USD"
        self._conn_state = "waiting"
        self._opp_thread = False        # 机会计算线程在跑（防重入）
        self._auto_load_pending = False  # 自动载入首个组合挂起（后台计算完成后消费）
        self._last_hist_req = 0.0       # 上次历史价差请求时间（60s 节流）

        # 后台线程 -> 主线程回调桥（机会计算结果投递等）
        self._callin = _CallInMain(self)
        self._callin.call.connect(lambda fn: fn())
        self.session_name = f'{account["login"]}@{account["server"]}'

        self.setWindowTitle(f"控制台 — {self.session_name}")
        self.resize(1180, 780)

        self._build_ui()
        self._bind_events()

        # 启动会话（自动连接本窗口账户）
        self._start_session()

    # ==================================================================
    # UI 构建
    # ==================================================================
    def _build_ui(self) -> None:
        central = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(central)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(6)
        layout.addWidget(self._build_top_bar())
        self._tabs = QtWidgets.QTabWidget()
        self._tabs.addTab(self._build_tab_data(), "规则 · 隔夜利息")
        self._tabs.addTab(self._build_tab_opp(), "交易机会")
        self._tabs.addTab(self._build_tab_strategy(), "策略控制台")
        layout.addWidget(self._tabs, stretch=1)
        self.setCentralWidget(central)

        # Toast 浮层通知（右下角，订阅 eOrderResult / eLog）
        self._toast_mgr = ToastManager()
        self._toast_mgr.attach(self)
        self._toast_mgr.notify_hook(self._ee, session_name=self.session_name)

        # 快捷键：Ctrl+1..3 切 Tab；Escape 停止监测
        for i in range(3):
            QtGui.QShortcut(
                QtGui.QKeySequence(f"Ctrl+{i + 1}"), self,
                activated=lambda idx=i: self._tabs.setCurrentIndex(idx),
            )
        self._sc_escape = QtGui.QShortcut(QtGui.QKeySequence("Escape"), self)
        self._sc_escape.activated.connect(self._stop_monitor)

        # 顶栏 2s 定时刷新（tick 时龄）
        self._conn_timer = QtCore.QTimer(self)
        self._conn_timer.setInterval(2000)
        self._conn_timer.timeout.connect(self._update_header_line)
        self._conn_timer.start()

    def _build_top_bar(self) -> QtWidgets.QWidget:
        top = QtWidgets.QWidget()
        top.setFixedHeight(40)
        lay = QtWidgets.QHBoxLayout(top)
        lay.setContentsMargins(0, 2, 0, 2)
        lay.setSpacing(10)

        self._status_pill = StatusPill()
        self._status_pill.set_state("waiting")
        self._info = QtWidgets.QLabel("-")
        self._info.setStyleSheet(f"color: {COLORS['TXT_MUTED']};")
        self._lbl_mode = QtWidgets.QLabel("纸面")
        self._lbl_mode.setStyleSheet(f"color: {COLORS['UP']}; font-weight: bold;")
        self._btn_session = QtWidgets.QPushButton("断开会话")
        self._btn_session.setToolTip("断开/重连账户；仅断接口连接，MT5 终端保持运行")
        self._btn_session.clicked.connect(self._toggle_session)

        lay.addWidget(self._status_pill)
        lay.addWidget(self._info, stretch=1)
        lay.addWidget(self._lbl_mode)
        lay.addWidget(self._btn_session)
        return top

    # ------------------------------------------------------------------
    # Tab1 规则·隔夜利息（合并一表，可排序）
    # ------------------------------------------------------------------
    def _build_tab_data(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(page)
        v.setContentsMargins(0, 4, 0, 0)

        bar = QtWidgets.QHBoxLayout()
        btn_rules = QtWidgets.QPushButton("加载全部交易规则")
        btn_rules.clicked.connect(lambda: self._sess_repeat(lambda s: s.request_rules()))
        self._rule_filter = QtWidgets.QLineEdit()
        self._rule_filter.setPlaceholderText("过滤品种名…")
        self._rule_filter.textChanged.connect(self._refresh_data_table)
        self._chk_watch = QtWidgets.QCheckBox("自动检测隔夜利息变化")
        self._chk_watch.setChecked(True)
        self._chk_watch.toggled.connect(self._toggle_watch)
        self._spin_interval = QtWidgets.QSpinBox()
        self._spin_interval.setRange(10, 3600)
        self._spin_interval.setValue(60)
        self._spin_interval.setSuffix(" s")
        self._lbl_last_swap = QtWidgets.QLabel("最近入库变更: --")
        self._lbl_last_swap.setStyleSheet(f"color: {COLORS['TXT_MUTED']};")
        bar.addWidget(btn_rules)
        bar.addWidget(self._chk_watch)
        bar.addWidget(QtWidgets.QLabel("间隔"))
        bar.addWidget(self._spin_interval)
        bar.addWidget(self._lbl_last_swap)
        bar.addStretch()
        bar.addWidget(QtWidgets.QLabel("过滤"))
        bar.addWidget(self._rule_filter, stretch=1)
        v.addLayout(bar)

        self._tbl_data = MiniTable(DATA_COLUMNS)
        self._rules_cache: list[dict] = []
        self._swaps_raw: list[dict] = []
        self._swaps_fetched_at: str | None = None
        v.addWidget(self._tbl_data, stretch=1)
        return page

    # ------------------------------------------------------------------
    # Tab2 交易机会
    # ------------------------------------------------------------------
    def _build_tab_opp(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        v = QtWidgets.QVBoxLayout(page)
        v.setContentsMargins(0, 4, 0, 0)

        bar = QtWidgets.QHBoxLayout()
        btn_gen = QtWidgets.QPushButton("由隔夜利息生成交易机会")
        btn_gen.clicked.connect(self._generate_opportunities)
        self._spin_max_legs = QtWidgets.QSpinBox()
        self._spin_max_legs.setRange(2, 5)
        self._spin_max_legs.setValue(5)
        bar.addWidget(btn_gen)
        bar.addWidget(QtWidgets.QLabel("最大腿数"))
        bar.addWidget(self._spin_max_legs)
        hint = QtWidgets.QLabel("双击行 → 载入策略控制台（自动订阅腿并拉历史价差）")
        hint.setStyleSheet(f"color: {COLORS['TXT_MUTED']};")
        bar.addWidget(hint)
        bar.addStretch()
        v.addLayout(bar)

        self._opp_table = MiniTable(OPP_COLUMNS)
        self._opportunities: list[dict] = []
        self._swaps_payload: dict | None = None
        v.addWidget(self._opp_table, stretch=1)
        return page

    # ------------------------------------------------------------------
    # Tab3 策略控制台：上下结构 —— 上部 = 左图表(7) / 右控制台(3)；下部 = 四个子Tab
    # ------------------------------------------------------------------
    def _build_tab_strategy(self) -> QtWidgets.QWidget:
        page = QtWidgets.QWidget()
        outer = QtWidgets.QVBoxLayout(page)
        outer.setContentsMargins(0, 4, 0, 0)
        outer.setSpacing(6)

        # ---- 上部：左图表(7) / 右功能操作区(3) ----
        top = RatioSplitter(0.7, QtCore.Qt.Horizontal)   # 图表:控制台 = 7:3，随窗口缩放保持
        top.setChildrenCollapsible(False)

        # 左：价差图（十字光标/阈值线/极值线）
        chart_wrap = QtWidgets.QWidget()
        cv = QtWidgets.QVBoxLayout(chart_wrap)
        cv.setContentsMargins(0, 0, 0, 0)
        self._chart = SpreadChart()
        self._chart.sig_k_changed.connect(self._on_chart_k_changed)
        self._lbl_stats = QtWidgets.QLabel("-")
        self._lbl_stats.setStyleSheet(f"color: {COLORS['TXT_MUTED']};")
        cv.addWidget(self._chart, stretch=1)
        cv.addWidget(self._lbl_stats)
        top.addWidget(chart_wrap)

        # 右：策略控制台
        right = QtWidgets.QWidget()
        rv = QtWidgets.QVBoxLayout(right)
        rv.setContentsMargins(6, 0, 0, 0)
        rv.setSpacing(6)

        self._lbl_opp = QtWidgets.QLabel("未加载交易机会")
        self._lbl_opp.setWordWrap(True)
        rv.addWidget(self._lbl_opp)

        self._legs_table = MiniTable(LEG_COLUMNS)
        self._legs_table.setMinimumHeight(96)
        rv.addWidget(self._legs_table)

        form = QtWidgets.QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        self._spin_window = QtWidgets.QSpinBox()
        self._spin_window.setRange(50, 5000)
        self._spin_window.setValue(500)
        self._spin_k = QtWidgets.QDoubleSpinBox()
        self._spin_k.setRange(0.5, 10)
        self._spin_k.setSingleStep(0.25)
        self._spin_k.setValue(2.0)
        self._spin_grid = QtWidgets.QDoubleSpinBox()
        self._spin_grid.setRange(0.1, 5)
        self._spin_grid.setSingleStep(0.1)
        self._spin_grid.setValue(0.5)
        self._spin_levels = QtWidgets.QSpinBox()
        self._spin_levels.setRange(1, 10)
        self._spin_levels.setValue(3)
        self._spin_maxpos = QtWidgets.QSpinBox()
        self._spin_maxpos.setRange(1, 100)
        self._spin_maxpos.setValue(10)
        self._spin_volume = QtWidgets.QDoubleSpinBox()
        self._spin_volume.setRange(0.01, 100)
        self._spin_volume.setSingleStep(0.01)
        self._spin_volume.setValue(0.01)
        self._chk_paper = QtWidgets.QCheckBox("纸面模式（只记录不下单）")
        self._chk_paper.setChecked(True)
        self._chk_paper.toggled.connect(self._update_mode)
        for label, w in (
            ("Z窗口", self._spin_window), ("入场阈值 k", self._spin_k),
            ("网格步进 σ", self._spin_grid), ("网格层数", self._spin_levels),
            ("最大持仓(方向)", self._spin_maxpos),
            ("每层手数(逐步小单)", self._spin_volume),
        ):
            form.addRow(label, w)
        form.addRow("", self._chk_paper)
        rv.addLayout(form)

        # 2×2 网格而非单行 4 按钮：避免右栏最小宽度过大撑爆 7:3 比例
        btns = QtWidgets.QGridLayout()
        btns.setHorizontalSpacing(6); btns.setVerticalSpacing(4)
        self._btn_start = QtWidgets.QPushButton("▶ 开始监测")
        self._btn_start.setEnabled(False)
        self._btn_start.setToolTip("等待历史价差…（载入交易机会后自动请求）")
        self._btn_start.clicked.connect(self._on_start_clicked)
        self._btn_hist = QtWidgets.QPushButton("重新加载历史价差")
        self._btn_hist.clicked.connect(self._load_history)
        self._btn_pick = QtWidgets.QPushButton("📁 选组合…")
        self._btn_pick.clicked.connect(self._pick_opportunity)
        self._btn_manual = ArmedButton("💥 手动进场")
        self._btn_manual.setToolTip("手动补一层小单（默认组合方向）；进场以自动网格为主")
        self._btn_manual.sig_fire.connect(self._manual_entry)
        btns.addWidget(self._btn_start, 0, 0)
        btns.addWidget(self._btn_hist, 0, 1)
        btns.addWidget(self._btn_pick, 1, 0)
        btns.addWidget(self._btn_manual, 1, 1)
        rv.addLayout(btns)
        rv.addStretch()
        top.addWidget(right)

        outer.addWidget(top, stretch=1)

        # ---- 下部：持仓 / 成交记录 / 信号 / 日志（四个子Tab）----
        sub = QtWidgets.QTabWidget()
        sub.setMinimumHeight(240)
        sub.setMaximumHeight(300)

        # 持仓（原始持仓，不合并）
        pos_page = QtWidgets.QWidget()
        pv = QtWidgets.QVBoxLayout(pos_page)
        pv.setContentsMargins(0, 4, 0, 0)
        self._tbl_positions = MiniTable(POSITION_COLUMNS)
        pv.addWidget(self._tbl_positions)
        sub.addTab(pos_page, "持仓")

        # 成交记录（按需加载）
        deal_page = QtWidgets.QWidget()
        dv = QtWidgets.QVBoxLayout(deal_page)
        dv.setContentsMargins(0, 4, 0, 0)
        dbar = QtWidgets.QHBoxLayout()
        self._spin_deal_days = QtWidgets.QSpinBox()
        self._spin_deal_days.setRange(1, 90)
        self._spin_deal_days.setValue(7)
        self._spin_deal_days.setSuffix(" 天")
        btn_deals = QtWidgets.QPushButton("加载成交记录")
        btn_deals.clicked.connect(self._request_deals)
        dbar.addWidget(QtWidgets.QLabel("最近"))
        dbar.addWidget(self._spin_deal_days)
        dbar.addWidget(btn_deals)
        dbar.addStretch()
        dv.addLayout(dbar)
        self._tbl_deals = MiniTable(DEAL_COLUMNS)
        dv.addWidget(self._tbl_deals)
        sub.addTab(deal_page, "成交记录")

        # 信号（自动网格分层小单记录）
        self._tbl_signals = MiniTable(SIGNAL_COLUMNS)
        sub.addTab(self._tbl_signals, "信号")

        # 日志
        self._log_text = QtWidgets.QPlainTextEdit()
        self._log_text.setReadOnly(True)
        self._log_text.setMaximumBlockCount(1000)
        sub.addTab(self._log_text, "日志")

        outer.addWidget(sub)
        return page


    # 顶栏摘要（净值/使用率/tick 时龄，2s 定时 + 事件驱动）
    # ==================================================================
    def _update_header_line(self) -> None:
        parts: list[str] = []
        if self._last_account:
            equity = _f(self._last_account.get("equity"))
            margin, margin_free = _f(self._last_account.get("margin")), _f(self._last_account.get("margin_free"))
            denom = margin + margin_free
            usage = margin / denom if denom > 0 else 0.0
            parts.append(f"净值 {equity:,.2f}")
            parts.append(f"使用率 {usage:.0%}")
            if usage > 0.9:
                self._info.setStyleSheet(f"color: {COLORS['DOWN']}; font-weight: bold;")
            elif usage > 0.8:
                self._info.setStyleSheet(f"color: {COLORS['WARN']};")
            else:
                self._info.setStyleSheet(f"color: {COLORS['TXT_MUTED']};")
        if self._conn_state == "disconnected" or self._session is None:
            parts.append("会话断开")
        elif self._last_tick_at is not None:
            age = time.time() - self._last_tick_at
            parts.append(f"tick {age:.0f}s前" + (" ⚠停滞" if age > 10 else ""))
        else:
            parts.append("tick 待数据")
        self._info.setText(" · ".join(parts))

    def _update_mode(self) -> None:
        paper = self._chk_paper.isChecked()
        self._lbl_mode.setText("纸面" if paper else "实盘")
        self._lbl_mode.setStyleSheet(
            f"color: {COLORS['UP'] if paper else COLORS['DOWN']}; font-weight: bold;"
        )

    # ==================================================================
    # 会话管理
    # ==================================================================
    def _start_session(self) -> None:
        if self._session is not None:
            return
        self._session = AccountSession(self._ee, self._account, on_exit=self._session_died)
        self._session.start()
        self._position_rows = {}
        self._signals_rows = []
        self._monitoring = False
        self._conn_state = "waiting"
        self._status_pill.set_state("waiting")
        self._btn_session.setText("断开会话")

    def _toggle_session(self) -> None:
        """连接/断开状态机（状态不再丢失：断开后可随时重连）。"""
        if self._session:
            # 纯 detach：只断接口连接，MT5 终端保持运行（终端由用户手动管理）
            self.stop_session_and_close()
        else:
            self._start_session()

    def _session_died(self, name: str) -> None:
        self._session = None
        self._monitoring = False
        self._conn_state = "disconnected"
        self._status_pill.set_state("disconnected")
        self._btn_session.setText("连接账户")
        self._update_header_line()

    def stop_session_and_close(self) -> None:
        """窗口关闭/断开 = 会话结束（后台线程停止，UI 立即响应）。

        会话 stop 含子进程 join（最长 8s），绝不能在 GUI 线程同步做，
        否则主窗口关闭时控制台会冻结数秒，看起来像"关不掉"。"""
        session = self._session
        self._session = None
        # 先解绑事件桥（防止瞬时事件在窗口销毁后仍投递引发 Signal deleted）
        for bridge in getattr(self, "_bridges", []):
            try:
                self._ee.unregister(bridge.event_type, bridge._on_event)
            except Exception:
                pass
        self._bridges = []
        if session is not None:
            def _stop_bg():
                try:
                    session.stop(close_terminal=False)
                except Exception:
                    pass
            threading.Thread(
                target=_stop_bg, name=f"stop-{self.session_name}", daemon=True
            ).start()
        self._monitoring = False
        self._conn_state = "disconnected"
        self._status_pill.set_state("disconnected")
        self._btn_session.setText("连接账户")
        self._update_header_line()
        if self._on_closed:
            self._on_closed(self.session_name)

    def closeEvent(self, e) -> None:  # noqa: N802
        # 显式停掉定时器/回调，防关闭后回调已销毁对象
        self._conn_timer.stop()
        flush = getattr(self._chart, "_curve_flush", None)
        if flush is not None:
            flush.stop()
        self.stop_session_and_close()
        super().closeEvent(e)

    # ==================================================================
    # 事件绑定
    # ==================================================================
    def _bind_events(self) -> None:
        self._bridges = []
        for event_type, slot in (
            (EVENT_STATUS, self._on_status),
            (EVENT_ACCOUNT, self._on_account),
            (EVENT_SWAPS, self._on_swaps),
            (EVENT_RULES, self._on_rules),
            (EVENT_SPREAD_HISTORY, self._on_spread_history),
            (EVENT_POSITION, self._on_positions),
            (EVENT_DEAL, self._on_deals),
            (EVENT_SPREAD_ALGO, self._on_algo),
            (EVENT_TICK, self._on_tick),
        ):
            bridge = EventBridge(self._ee, event_type)
            bridge.event_received.connect(slot)
            self._bridges.append(bridge)
        log_bridge = EventBridge(self._ee, EVENT_LOG)
        log_bridge.event_received.connect(self._dispatch_log)
        self._bridges.append(log_bridge)

    def _is_self(self, event) -> bool:
        return isinstance(event.data, dict) and event.data.get("session") == self.session_name

    def _sess_repeat(self, fn) -> None:
        if self._session is None:
            QtWidgets.QMessageBox.warning(self, "提示", "会话未启动")
            return
        fn(self._session)

    # ==================================================================
    # 槽（主线程）
    # ==================================================================
    def _on_status(self, event) -> None:
        if not self._is_self(event):
            return
        payload = event.data["payload"]
        msg = str(payload.get("msg", ""))
        if payload.get("ok"):
            self._conn_state = "connected"
            self._status_pill.set_state("connected")
            self._btn_session.setText("断开会话")
            if self._chk_watch.isChecked():
                self._sess_repeat(lambda s: s.watch_swaps(self._spin_interval.value()))
            self._subscribe_legs_cache()
        else:
            # worker 处于启动/重试态时不把状态置为失败，避免误判
            self._conn_state = "retrying"
            self._status_pill.set_state("retrying")
            if "进程退出" in msg:
                self._status_pill.set_state("disconnected")
                self._conn_state = "disconnected"
        self._update_header_line()

    def _on_account(self, event) -> None:
        if not self._is_self(event):
            return
        acc = event.data["payload"]
        self._last_account = acc
        # 机会生成需要账户货币（swap 入金货币口径换算）
        currency = acc.get("currency")
        if currency:
            self._account_currency = currency
        self._update_header_line()

    def _on_rules(self, event) -> None:
        if not self._is_self(event):
            return
        self._rules_cache = list(event.data["payload"].get("items", []))
        self._refresh_data_table()

    def _on_swaps(self, event) -> None:
        if not self._is_self(event):
            return
        payload = event.data["payload"]
        self._swaps_payload = payload
        self._swaps_raw = list(payload.get("all", []))
        self._swaps_fetched_at = payload.get("fetched_at")
        changed = self._db.last_swap_change_time(self.session_name)
        self._lbl_last_swap.setText(f"最近入库变更: {changed or '-'}")
        self._refresh_data_table()
        # 自动载入：swap 数据到齐后若尚无组合则自动生成并载入首个机会
        if not self._current_legs:
            QtCore.QTimer.singleShot(500, self._auto_load_first_opportunity)

    # ------------------------------------------------------------------
    # 合并数据表（规则 + 隔夜利息 -> 一行一品种，可排序）
    # ------------------------------------------------------------------
    def _auto_load_first_opportunity(self, *_a) -> None:
        """连接成功后自动生成并载入首个组合（主图立即有数据，无需两步点击）。

        机会计算在后台线程跑，载入动作由 _opportunities_ready 在结果到达后接手。"""
        if self._current_legs or not self._swaps_payload:
            return
        self._auto_load_pending = True
        self._generate_opportunities()

    def _refresh_data_table(self) -> None:
        from ..constants import SWAP_MODE_NAME, WEEKDAY_NAME
        text = self._rule_filter.text().strip().upper()
        # swaps 以品种为索引（更新/权威 swap 数据）
        swap_by_symbol = {s.get("symbol", ""): s for s in self._swaps_raw}
        rows = []
        for rule in self._rules_cache:
            out = _decorate_rule_row(rule)
            swap_item = swap_by_symbol.get(out.get("symbol", ""))
            if swap_item is not None:
                # swap 权威字段覆盖（数值保持 float 供 money 排序/着色）
                out["swap_long"] = _f(swap_item.get("swap_long"), _f(out.get("swap_long")))
                out["swap_short"] = _f(swap_item.get("swap_short"), _f(out.get("swap_short")))
                mode = swap_item.get("swap_mode")
                out["swap_mode"] = (
                    SWAP_MODE_NAME.get(mode, str(mode)) if isinstance(mode, int) else "-"
                )
                rollover = swap_item.get("rollover3days")
                out["swap_rollover3days"] = (
                    WEEKDAY_NAME.get(rollover, str(rollover))
                    if isinstance(rollover, int) else "-"
                )
                update = self._db.last_swap_time(self.session_name, out.get("symbol", ""))
                out["update_time"] = update or self._swaps_fetched_at or "-"
            else:
                out["update_time"] = "-"
            if text and text not in out.get("symbol", ""):
                continue
            rows.append(out)
        # 只有 swap（无规则）的品种也显示（规则字段为 "-"）
        known = {r.get("symbol", "") for r in self._rules_cache}
        for sym, swap_item in swap_by_symbol.items():
            if sym in known:
                continue
            mode = swap_item.get("swap_mode")
            rollover = swap_item.get("rollover3days")
            update = self._db.last_swap_time(self.session_name, sym)
            rows.append({
                "symbol": sym,
                "swap_long": _f(swap_item.get("swap_long")),
                "swap_short": _f(swap_item.get("swap_short")),
                "swap_mode": SWAP_MODE_NAME.get(mode, str(mode)) if isinstance(mode, int) else "-",
                "swap_rollover3days": WEEKDAY_NAME.get(rollover, str(rollover))
                if isinstance(rollover, int) else "-",
                "update_time": update or self._swaps_fetched_at or "-",
            })
        # 全量重建行时暂停重绘，避免大表刷新掉帧
        self._tbl_data.setUpdatesEnabled(False)
        try:
            self._tbl_data.set_rows(rows)
        finally:
            self._tbl_data.setUpdatesEnabled(True)

    def _toggle_watch(self, on: bool) -> None:
        if self._session is None:
            return
        if on:
            self._session.watch_swaps(self._spin_interval.value())
        else:
            self._session.unwatch_swaps()

    # ------------------------------------------------------------------
    # 交易机会
    # ------------------------------------------------------------------
    def _generate_opportunities(self) -> None:
        if self._opp_thread:
            self._append_log("机会计算进行中…")
            return
        if not self._swaps_payload:
            QtWidgets.QMessageBox.information(
                self, "提示",
                "请先在【规则·隔夜利息】页开启自动检测/在连接后等待首轮数据。"
            )
            return
        payload = self._swaps_payload
        max_legs = self._spin_max_legs.value()
        account_currency = self._account_currency or "USD"
        self._opp_thread = True

        def _run() -> None:
            opps: list[dict] = []
            err: BaseException | None = None
            try:
                opps = generate(
                    payload.get("all", []),
                    fx_rates=payload.get("fx_rates"),
                    max_legs=max_legs,
                    cap=100,
                    account_currency=account_currency,
                )
            except Exception as exc:   # 保持旧行为的容错性：失败不冻结 UI，仅提示
                opps = []
                err = exc
            finally:
                # 结果回到主线程再改 UI（_opportunities / 表格 / 弹窗）
                self._callin.call.emit(
                    lambda: self._opportunities_ready(opps, err)
                )

        threading.Thread(
            target=_run,
            name=f"opp-gen-{self.session_name}",
            daemon=True,
        ).start()

    def _opportunities_ready(self, opportunities: list[dict],
                             err: BaseException | None = None) -> None:
        """后台 generate() 完成后的主线程收尾（原 _generate_opportunities 尾部逻辑）。"""
        self._opp_thread = False
        if err is not None:
            self._auto_load_pending = False
            self._append_log(f"机会计算失败: {err}")
            return
        self._opportunities = opportunities
        self._fill_opp_table(opportunities)
        if not opportunities:
            QtWidgets.QMessageBox.information(
                self, "无结果",
                "当前品种集中未发现可中性化的 2~5 腿组合。\n"
                "（涉及币种缺 fx_rates 汇率快照的组合会被跳过）",
            )
            return
        if self._auto_load_pending:
            self._auto_load_pending = False
            if self._opportunities:
                self._append_log("已自动载入最优组合（可在【交易机会】页另选）")
                self._set_opportunity(self._opportunities[0])

    def _fill_opp_table(self, opportunities: list[dict]) -> None:
        rows = []
        for opp in opportunities:
            rows.append({
                "net_swap": _f(opp["net_swap"]),
                "legs_count": opp["legs_count"],
                "legs_text": opp["legs_text"],
                "_tooltip": (
                    f"每腿明细（买入用 swap_long，卖出用 swap_short）:\n{opp.get('legs_detail', '')}\n"
                    "净息 = Σ 权重×该腿息（USD/天，按 USD 名义权重中性化）"
                ),
            })
        self._opp_table.set_rows(rows)

    def _load_selected_opportunity(self, index) -> None:
        if index.row() >= len(self._opportunities):
            return
        opp = self._opportunities[index.row()]
        self._set_opportunity(opp)

    def _pick_opportunity_dialog(self) -> dict | None:
        """策略台内直接选机会：列表选择框，不需回交易机会页双击。"""
        if not self._opportunities:
            QtWidgets.QMessageBox.warning(
                self, "提示",
                "尚无已生成的交易机会。请先到【交易机会】页点击"
                "\“由隔夜利息生成交易机会\”。"
            )
            return None
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle("选择交易机会 → 载入策略控制台")
        dialog.setMinimumSize(760, 440)
        table = QtWidgets.QTableWidget(0, 3)
        table.setHorizontalHeaderLabels(["净息/天(USD)", "腿数", "组合"])
        table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)
        table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.Stretch)
        for i, opp in enumerate(self._opportunities):
            table.insertRow(i)
            color = QtGui.QColor(COLORS["UP"] if opp["net_swap"] > 0 else COLORS["DOWN"])
            net_item = QtWidgets.QTableWidgetItem(f'{opp["net_swap"]:+.2f}')
            net_item.setForeground(color)
            net_item.setToolTip(opp.get("legs_detail", ""))
            table.setItem(i, 0, net_item)
            table.setItem(i, 1, QtWidgets.QTableWidgetItem(str(opp["legs_count"])))
            text_item = QtWidgets.QTableWidgetItem(opp["legs_text"])
            text_item.setToolTip(opp.get("legs_detail", ""))
            table.setItem(i, 2, text_item)
        btns = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        btns.accepted.connect(dialog.accept)
        btns.rejected.connect(dialog.reject)
        layout = QtWidgets.QVBoxLayout(dialog)
        layout.addWidget(QtWidgets.QLabel("选中一行（双击也可）后确定载入策略控制台："))
        layout.addWidget(table)
        layout.addWidget(btns)
        table.doubleClicked.connect(dialog.accept)

        chosen = None
        if dialog.exec() == QtWidgets.QDialog.Accepted:
            row = table.currentRow()
            if 0 <= row < len(self._opportunities):
                chosen = self._opportunities[row]
        return chosen

    def _pick_opportunity(self) -> None:
        opp = self._pick_opportunity_dialog()
        if opp is not None:
            self._set_opportunity(opp)

    def _set_opportunity(self, opp: dict) -> None:
        self._current_legs = list(opp["legs"])
        self._runner = None    # 重建 runner 以装载新组合
        self._hist_ready = False
        self._chart.clear_series()
        self._lbl_stats.setText("-")
        self._lbl_opp.setText(
            f'<b style="color:{COLORS["UP"] if opp["net_swap"] >= 0 else COLORS["DOWN"]}">'
            f'净息估 {opp["net_swap"]:+.2f} USD/天</b> | '
            f'{opp["legs_count"]} 腿 | {opp["legs_text"]}'
        )
        self._legs_table.set_rows([
            {
                "symbol": m["symbol"],
                "side": "买" if m["side"] == "buy" else "卖",
                "weight": _f(m.get("weight")),
                "swap": _f(m.get("swap")),
                "bid": "-", "ask": "-", "spread_pts": "-", "fl_pnl": "-",
            }
            for m in opp["legs"]
        ])
        # 关键：订阅组合各腿行情，否则实时价差永远取不到 tick
        for leg in opp["legs"]:
            self._ee.put(Event(
                EVENT_SYMBOL_SUBSCRIBE,
                {"session": self.session_name, "symbol": leg["symbol"]},
            ))
        # 自动请求历史价差（到达后启用"开始监测"并初始化统计；60s 节流防重复拉取）
        self._ensure_runner()
        self._request_spread_history_guarded()
        self._tabs.setCurrentWidget(self._tabs.widget(2))   # 载入即切策略控制台
        self._append_log(
            f"已载入组合并订阅 {len(opp['legs'])} 个腿的行情: {opp['legs_text']}"
            f"（等待历史价差…）"
        )

    # ------------------------------------------------------------------
    # 策略控制台
    # ------------------------------------------------------------------
    def _cleaned_legs(self) -> list[dict]:
        return [
            {
                "symbol": leg["symbol"],
                "side": leg.get("side", "buy"),
                "weight": float(leg.get("weight", 1.0)),
            }
            for leg in (self._current_legs or [])
        ]

    def _ensure_runner(self) -> bool:
        if not self._current_legs:
            QtWidgets.QMessageBox.warning(
                self, "提示",
                "尚未选择组合。点击\“选组合…\”从交易机会中选择，"
                "或到【交易机会】页双击载入。"
            )
            return False
        if self._runner is None:
            self._runner = StrategyRunner(
                self.session_name, self._current_legs,
                {
                    "window": self._spin_window.value(),
                    "k": self._spin_k.value(),
                    "grid_step": self._spin_grid.value(),
                    "levels": self._spin_levels.value(),
                    "max_positions": self._spin_maxpos.value(),
                },
            )
        return True

    def _current_stats(self) -> dict:
        """runner 当前统计口径：先验段（live<10 且有先验）用先验 mean/std，
        否则用滚动窗口 stats；两段口径都带 mean/std 字段。无数据返回 {}。"""
        if self._runner is None:
            return {}
        series = self._runner.series
        if series.live_points() < 10 and self._runner.prior:
            return {
                "mean": self._runner.prior["mean"],
                "std": self._runner.prior["std"],
            }
        return series.stats() or {}

    def _push_thresholds(self) -> None:
        stats = self._current_stats()
        if not stats:
            return
        self._chart.set_thresholds(
            stats["mean"], stats["std"],
            self._spin_k.value(), self._spin_grid.value(), self._spin_levels.value(),
        )

    def _on_params_changed(self) -> None:
        """6 个参数 spin 实时联动：更新 runner 参数并重画阈值线。"""
        if self._runner is None:
            return
        self._runner.params.update({
            "window": self._spin_window.value(),
            "k": self._spin_k.value(),
            "grid_step": self._spin_grid.value(),
            "levels": self._spin_levels.value(),
            "max_positions": self._spin_maxpos.value(),
        })
        self._runner.series.window = self._spin_window.value()
        sig = self._runner.signal
        sig.k = self._spin_k.value()
        sig.grid_step = self._spin_grid.value()
        sig.levels = self._spin_levels.value()
        sig.max_positions = self._spin_maxpos.value()
        self._push_thresholds()

    def _on_chart_k_changed(self, k: float) -> None:
        """拖动阈值线 -> 反写 k spin（块信号防回环）+ 同步 runner。"""
        self._spin_k.blockSignals(True)
        self._spin_k.setValue(float(k))
        self._spin_k.blockSignals(False)
        if self._runner is not None:
            self._runner.params["k"] = float(k)
            self._runner.signal.k = float(k)

    def _load_history(self) -> None:
        if not self._ensure_runner():
            return
        self._request_spread_history_guarded()

    def _request_spread_history_guarded(self) -> None:
        """历史价差请求 60s 节流：窗口内已发过则跳过，避免重复拉取刷屏/压 worker。"""
        now = time.time()
        if now - self._last_hist_req < 60.0:
            self._append_log("历史价差 60 秒内已请求过，本次跳过")
            return
        self._last_hist_req = now
        self._sess_repeat(lambda s: s.request_spread_history(self._cleaned_legs()))

    def _on_spread_history(self, event) -> None:
        if not self._is_self(event) or self._runner is None:
            return
        payload = event.data["payload"]
        datasets = payload.get("datasets", {})
        # 统计先验/长窗极值：H1 全窗（≥1 年）；图表与实时：M1（用户口径，断开最少）
        nh1 = len(datasets.get("h1", {}).get("v", []))
        nm1 = len(datasets.get("m1", {}).get("v", []))
        self._append_log(
            f"已加载历史价差：M1 × {nm1} 根（图表）/H1 × {nh1} 根（统计先验）…"
        )
        self._runner.set_history(datasets.get("h1", {}))
        self._hist_ready = True
        # 图表：优先 M1（断开最少）；worker 未给 M1 时回退 H1
        line = datasets.get("m1") or datasets.get("h1") or {"t": [], "v": []}
        self._chart.set_series(list(line["t"]), list(line["v"]))
        candles = datasets.get("m1_candles") or datasets.get("h1_candles")
        if candles:
            self._chart.set_candles(
                candles["t"], candles["o"], candles["h"],
                candles["l"], candles["c"],
            )
        self._push_thresholds()
        self._chart.set_extremes(self._runner.series.extremes())
        self._btn_start.setEnabled(True)
        self._btn_start.setToolTip("")
        self._append_log("历史价差已就绪（可点开始监测）")

    # ------------------------------------------------------------------
    # 监测启停（单按钮互换 + Escape）
    # ------------------------------------------------------------------
    def _on_start_clicked(self) -> None:
        if self._monitoring:
            self._stop_monitor()
        else:
            self._start_monitor()

    def _start_monitor(self) -> None:
        if not self._ensure_runner():
            return
        if not self._hist_ready:
            self._append_log("历史价差尚未就绪，正在自动重拉…")
            self._request_spread_history_guarded()
            return
        self._monitoring = True
        self._btn_start.setText("■ 停止监测")
        self._btn_start.setStyleSheet(
            f"QPushButton {{ border: 1px solid {COLORS['DOWN']};"
            f" color: {COLORS['DOWN']}; font-weight: bold; }}"
        )
        self._append_log(
            "自动网格监测已启动（纸面·分层小单）"
            if self._chk_paper.isChecked()
            else "自动网格监测已启动（实盘!! 每层触发即下单）"
        )

    def _stop_monitor(self) -> None:
        if not self._monitoring:
            return
        self._monitoring = False
        self._btn_start.setText("▶ 开始监测")
        self._btn_start.setStyleSheet("")
        self._append_log("监测已停止")

    def _subscribe_legs_cache(self) -> None:
        legs = getattr(self, "_current_legs", None) or []
        for leg in legs:
            self._ee.put(Event(
                EVENT_SYMBOL_SUBSCRIBE,
                {"session": self.session_name, "symbol": leg["symbol"]},
            ))

    # ------------------------------------------------------------------
    # live tick / 信号（自动网格分层小单）
    # ------------------------------------------------------------------
    def _on_tick(self, event) -> None:
        if not self._is_self(event):
            return
        symbol = None
        for item in event.data["payload"]:
            symbol = item["symbol"]
            self._last_ticks[symbol] = item
        self._last_tick_at = time.time()
        self._update_leg_quotes()
        if symbol is None or self._runner is None or not self._hist_ready:
            return
        result = self._runner.on_tick(self._last_ticks)
        if result is None:
            return
        if self._monitoring:
            self._handle_strategy(result)
        else:
            # 未监测：只画实时数据（不触发信号/下单），保证图先于监测就疲惫
            self._apply_chart_tick(result)

    def _update_leg_quotes(self) -> None:
        """腿表实时报价（bid/ask/点差）：从 _last_ticks 就地更新对应行。
        就地更新不触发排序变动/不重进整表，4 腿 × 0.2s 开销可忽略。"""
        if not self._current_legs:
            return
        # 品种 -> 行号（当前排序下）
        row_by_symbol: dict[str, int] = {}
        for r in range(self._legs_table.rowCount()):
            item = self._legs_table.item(r, 0)
            if item:
                row_by_symbol[item.text()] = r
        cols = {c.key: i for i, c in enumerate(self._legs_table._cols)}
        sym_pnl = getattr(self, "_sym_pnl", {})
        for leg in self._current_legs:
            tick = self._last_ticks.get(leg["symbol"])
            row = row_by_symbol.get(leg["symbol"])
            if row is None:
                continue
            if tick is not None:
                bid, ask = _f(tick.get("bid")), _f(tick.get("ask"))
                pts = ask - bid
                it = self._legs_table.item(row, cols["bid"])
                if it:
                    it.setText(f"{bid:.5f}")
                    it.setData(_NUM_ROLE, bid)
                it = self._legs_table.item(row, cols["ask"])
                if it:
                    it.setText(f"{ask:.5f}")
                    it.setData(_NUM_ROLE, ask)
                it = self._legs_table.item(row, cols["spread_pts"])
                if it:
                    it.setText(f"{pts:.5f}")
                    it.setData(_NUM_ROLE, pts)
            # 浮盈（USD，position profit 同品种相抵口径）
            pnl = sym_pnl.get(leg["symbol"])
            it = self._legs_table.item(row, cols.get("fl_pnl", -1))
            if it is not None and pnl is not None:
                it.setText(f"{pnl:+,.2f}")
                it.setData(_NUM_ROLE, float(pnl))

    def _apply_chart_tick(self, result: dict) -> None:
        """数据先行：未监测也持续画实时曲线/蜡烛/统计，
        监测按钮只控制信号与下单，不再控制数据流。"""
        self._chart.append_point(result["ts"], result["spread"])
        self._chart.append_candle_tick(result["ts"], result["spread"])
        self._push_thresholds()
        self._chart.set_extremes(result.get("extremes") or {})
        self._update_stats_label(result)

    def _handle_strategy(self, result: dict) -> None:
        # 图表：live 增点 + 当前蜡烛聚合 + 阈值 + 极值
        self._apply_chart_tick(result)
        signal = result.get("signal")
        if signal:
            ts = dt.datetime.fromtimestamp(result["ts"]).strftime("%H:%M:%S")
            self._append_signal(ts, signal, result.get("hedge"))

    def _update_stats_label(self, result: dict) -> None:
        """统计标签（None 安全：extremes 四键可能为 None）。"""
        stats = result.get("stats") or {}
        ext = result.get("extremes") or {}

        def fmt5(v) -> str:
            return "-" if v is None else f"{float(v):+.5f}"

        def fmt2(v) -> str:
            return "-" if v is None else f"{float(v):+.2f}"

        prior_mark = " (先验)" if stats.get("prior") else ""
        keys = ("max_1y", "min_1y", "max_6m", "min_6m")
        if all(ext.get(k) is None for k in keys):
            ext_line = "极值待数据"
        else:
            ext_line = (
                f"1y [{fmt5(ext.get('max_1y'))}, {fmt5(ext.get('min_1y'))}] | "
                f"6m [{fmt5(ext.get('max_6m'))}, {fmt5(ext.get('min_6m'))}]"
            )
        vol_per_level = self._spin_volume.value()
        self._lbl_stats.setText(
            f"价差 {fmt5(result.get('spread'))} | z {fmt2(stats.get('z'))} | "
            f"mean {fmt5(stats.get('mean'))} ± {fmt5(stats.get('std'))}{prior_mark}\n"
            f"{ext_line}\n"
            f"分层：每层 {vol_per_level:.2f} 手/腿，最多 {self._spin_maxpos.value()} 层/方向"
        )

    def _append_signal(self, ts: str, signal: dict, hedge) -> None:
        mode = "paper" if self._chk_paper.isChecked() else "LIVE"
        vol_per_level = self._spin_volume.value()
        if signal["action"] == "open":
            remark = (
                f"第{signal.get('count', 1)}层 · {signal.get('remark', '')}"
                f" · 本层 {vol_per_level:.2f} 手/腿"
            )
        elif signal["action"] == "close":
            remark = f"平 {signal.get('count', 0)} 层（z 回归）"
        else:
            remark = f"{signal.get('remark', '')}（仓位已满）"
        self._signals_rows.append({
            "ts": ts,
            "action": signal["action"],
            "side": signal.get("side", ""),
            "z": signal.get("z", ""),
            "count": signal.get("count", ""),
            "mode": mode,
            "remark": remark,
        })
        # 超过上限删最旧（列表正序 append，头部即最旧），保 table/统计基于 cap 后列表
        overflow = len(self._signals_rows) - self._SIGNALS_CAP
        if overflow > 0:
            del self._signals_rows[:overflow]
        self._tbl_signals.set_rows(list(reversed(self._signals_rows)))
        if signal["action"] in ("open", "close"):
            legs_desc = [
                {"symbol": h["symbol"], "side": h["side"], "weight": h["weight"]}
                for h in hedge
            ]
            self._db.insert_trade(
                self.session_name, time.strftime("%Y-%m-%d %H:%M:%S"), mode,
                signal["action"], self.session_name, legs_desc,
                {"z": signal.get("z"), "count": signal.get("count")},
            )
        n_open = sum(1 for s in self._signals_rows if s["action"] == "open")
        self._lbl_mode.setToolTip(f"信号 {len(self._signals_rows)} 条 · open {n_open} 层")

    def _manual_entry(self) -> None:
        if not self._ensure_runner():
            return
        # 手动 = 组合方向补一层小单（不再弹方向选择；进场以自动网格为主）
        signal = {
            "action": "open", "side": "buy", "count": 1,
            "level": (self._runner.series.stats() or {}).get("last", 0), "z": 0,
        }
        self._append_signal(time.strftime("%H:%M:%S"), signal, [])
        if not self._chk_paper.isChecked():
            self._execute_live(signal)

    def _execute_live(self, signal: dict) -> None:
        """实盘执行（每个信号 = 一次价差算法请求，撕塔算法在 worker 内部两阶段闭）。

        - 腿的带方向倍数经 USD 中性权重自动计算（不手动管理价差）；
        - active leg（点差最宽）先吃单，成交回调后其余腿立即对冲（vnpy
          SpreadTakerAlgo 语义）；
        - 每层触发一次算法事件（逐步小单，不会一次满仓）。
        """
        from ..spreads import build_legs_from_opportunity

        legs = self._current_legs or []
        if not legs:
            return
        quotes = {
            sym: {"bid": _f(t.get("bid")), "ask": _f(t.get("ask"))}
            for sym, t in (self._last_ticks or {}).items()
        }
        meta = {
            r.get("symbol", ""): {
                "min_volume": _f(r.get("volume_min"), 0.01) or 0.01,
                "volume_step": _f(r.get("volume_step"), 0.01) or 0.01,
            }
            for r in self._rules_cache
            if r.get("symbol")
        }
        flip = signal.get("side") == "sell"
        opp = {"legs": [
            {"symbol": leg["symbol"],
             "side": ("sell" if flip and leg["side"] == "buy"
                      else "buy" if flip and leg["side"] == "sell"
                      else leg["side"]),
             "weight": float(leg.get("weight", 1.0)),
             "swap": leg.get("swap", 0.0)}
            for leg in legs
        ]}
        elegs = build_legs_from_opportunity(
            opp, quotes, float(self._spin_volume.value()), meta,
        )
        payload = [
            {"symbol": l.symbol, "side": l.side, "volume": l.multiplier}
            for l in elegs
        ]
        self._ee.put(Event(EVENT_ALGO_REQUEST, {
            "session": self.session_name,
            "legs": payload,
            "comment": f"strat-{signal['action']}",
        }))
        self._append_log(
            "价差算法已下发（" + ("卖" if flip else "买") + "）: "
            + " | ".join(f"{l.side}:{l.symbol}×{l.multiplier}" for l in elegs)
        )

    # ------------------------------------------------------------------
    # 持仓（原始持仓，不合并）/ 成交记录 / 日志
    # ------------------------------------------------------------------
    def _on_positions(self, event) -> None:
        """按品种合并为净中仓（同品种多空相抵；跨品种不合并）。
        顺带构建 legs 表浮盈映射（由 _update_leg_quotes 消费）。"""
        if not self._is_self(event):
            return
        raw = event.data["payload"]
        self._sym_pnl = {
            p["symbol"]: _f(p.get("profit")) for p in raw if p.get("symbol")
        }
        self._update_leg_quotes()
        merged: dict[str, dict] = {}
        for p in raw:
            symbol = p["symbol"]
            sign = 1 if p.get("type") == 0 else -1     # POSITION_TYPE_BUY=0
            vol = _f(p.get("volume"))
            m = merged.setdefault(symbol, {
                "symbol": symbol, "net_volume": 0.0, "direction": "",
                "avg_price": 0.0, "price_current": _f(p.get("price_current")),
                "profit": 0.0, "swap": 0.0, "_sign_vol": 0.0, "_abs_vol": 0.0,
                "_px_vol": 0.0,
            })
            m["_sign_vol"] += sign * vol
            m["_abs_vol"] += vol
            m["_px_vol"] += _f(p.get("price_open")) * vol
            m["price_current"] = _f(p.get("price_current"))
            m["profit"] += _f(p.get("profit"))
            m["swap"] += _f(p.get("swap"))
        rows = []
        for m in merged.values():
            net = abs(m["_sign_vol"])
            if net < 1e-12:
                direction = "对冲"
            else:
                direction = "多" if m["_sign_vol"] > 0 else "空"
            rows.append({
                "symbol": m["symbol"],
                "net_volume": net,
                "direction": direction,
                # 同品种内手数加权均价（对锁时占比仍有效）
                "avg_price": m["_px_vol"] / m["_abs_vol"] if m["_abs_vol"] else 0.0,
                "price_current": m["price_current"],
                "profit": m["profit"],
                "swap": m["swap"],
            })
        self._tbl_positions.set_rows(rows)

    def _on_algo(self, event) -> None:
        """价差算法执行进度（worker 端两阶段闭环状态流）→ 日志 + 状态色。"""
        if not self._is_self(event):
            return
        payload = event.data["payload"]
        stage = payload.get("stage", "")
        color = {
            "active": COLORS["ACCENT"], "hedge": COLORS["WARN"],
            "done": COLORS["UP"], "failed": COLORS["DOWN"],
            "stalled": COLORS["WARN"],
            "audit_ok": COLORS["UP"], "audit_mismatch": COLORS["DOWN"],
        }.get(stage, COLORS["TXT_MUTED"])
        self._append_log(
            f"[价差算法 {payload.get('algoid')}] [{stage}] {payload.get('msg')}"
        )
        if stage == "failed":
            QtWidgets.QMessageBox.critical(
                self, "价差算法失败",
                f"{payload.get('msg')}\n\n存在裸腿暴露风险，请立即检查持仓！"
            )

    def _request_deals(self) -> None:
        self._sess_repeat(
            lambda s: s.request_deals(self._spin_deal_days.value())
        )

    def _on_deals(self, event) -> None:
        if not self._is_self(event):
            return
        raw = event.data["payload"]
        # DEAL_TYPE_BUY=0 / SELL=1；DEAL_ENTRY: 0=开仓 1=平仓 2=翻转 3=对冲平仓
        entry_name = {0: "开仓", 1: "平仓", 2: "翻转", 3: "对冲平仓"}
        rows = []
        for d in raw:
            ts = d.get("time")
            rows.append({
                "time": time.strftime("%m-%d %H:%M:%S", time.localtime(ts)) if ts else "-",
                "symbol": d.get("symbol", ""),
                "direction": "买" if d.get("type") == 0 else "卖",
                "entry": entry_name.get(d.get("entry"), str(d.get("entry"))),
                "volume": _f(d.get("volume")),
                "price": _f(d.get("price")),
                "profit": _f(d.get("profit")),
                "commission": _f(d.get("commission")),
                "swap": _f(d.get("swap")),
                "comment": str(d.get("comment", "")),
            })
        self._tbl_deals.set_rows(rows)
        self._append_log(f"成交记录已加载 {len(rows)} 笔（最近 {self._spin_deal_days.value()} 天）")

    def _append_log(self, msg: str) -> None:
        self._log_text.appendPlainText(f"{time.strftime('%H:%M:%S')} {msg}")

    def _dispatch_log(self, event) -> None:
        if not self._is_self(event):
            return
        payload = event.data["payload"]
        self._log_text.appendPlainText(
            f'{event.data.get("session")} [{payload.get("level")}] '
            f'{payload.get("source")} | {payload.get("msg")}'
        )


# 兼容旧导入名（规则表曾单独存在）
RULE_COLUMNS = DATA_COLUMNS
