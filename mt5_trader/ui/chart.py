"""确定性实时价差折线图（v3：固定窗口 / 显式坐标 / 零交互状态机）。

设计原则——解决历史所有图表问题的根本方案：
- 数据模型单一：恒定 numpy 缓冲 (t, v)，只追加写入 + 脏标记；
- 视图完全显式：每次重绘用 setXRange/setYRange 直接量度"最近 N 根"窗口；
  **不用 autoRange、不用 setLimits、不响应拖拽/滚轮**（内部零状态机，
  pyqtgraph 的 setRange"a/b < lmn 平移回弹"等坑不再有入口）；
- 重绘统一走 250ms 合并定时器；数据替换/窗口切换立即重绘；
- 无科学计数法（_FixedFmtAxis 定点小数轴）；十字光标纯只读、节流；
- 阈值线可拖动（拖动→sig_k_changed 反写 k），极值线只读。

旧接口兼容：set_candles / append_candle_tick / set_candles_visible 保留为 no-op
（K 线已退役，折线为唯一视觉）。
"""

from __future__ import annotations

import bisect
import datetime as dt
import math
import time

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets

_BG = "#12161d"
_SPREAD = "#4d8df0"
_MEAN = "#4d8df0"
_K_UP = "#ea3943"     # 上阈值 / 卖出进场
_K_DOWN = "#16c784"   # 下阈值 / 买入进场
_EXTREME = "#f0b90b"
_CROSS = (255, 255, 255, 70)

_H1 = 3600.0    # 默认桶宽（秒），实际由 set_series 从数据自动推导
_REDRAW_MS = 250   # 重绘合并节流


def _fixed_ticks(values: list[float], spacing: float) -> list[str]:
    """定点小数刻度文本（根除科学计数法）。小数位由 spacing 幂推导。"""
    if not values:
        return []
    sp = abs(float(spacing)) if spacing else 1.0
    if not (sp > 0) or not (sp == sp):
        sp = 1.0
    if sp >= 1.0:
        dec = 0
    else:
        try:
            dec = min(10, max(0, int(math.ceil(-math.log10(sp)))))
        except (ValueError, OverflowError):
            dec = 5
    out = [f"{float(v):.{dec}f}" for v in values]
    return [s.rstrip("0").rstrip(".") if "." in s else s for s in out]


class _FixedFmtAxis(pg.AxisItem):
    """定点小数数值轴（pyqtgraph 默认对 1e-4 量级值会走 e-记数法）。"""

    def tickStrings(self, values, scale, spacing):  # noqa: N802
        if scale and scale != 1.0:
            values = [v / scale for v in values]
        return _fixed_ticks(values, spacing)


# 窗口选择（秒）："全部" 表示显示缓冲内所有数据
_WINDOW_CHOICES: list[tuple[str, float | None]] = [
    ("近1小时", 3600.0),
    ("近8小时", 8 * 3600.0),
    ("近24小时", 24 * 3600.0),
    ("近3天", 3 * 86400.0),
    ("近7天", 7 * 86400.0),
    ("全部", None),
]


class SpreadChart(QtWidgets.QWidget):
    """价差实时折线图（确定性窗口：下拉选择时段，零拖拽/零缩放状态）。

    对外接口（与 console 完全兼容）：
    - sig_k_changed(k)：拖动上阈值线 → 反写 k；
    - set_series(t, v) / append_point(t, v)/clear_series()：数据；
    - set_thresholds(...)/set_extremes(...)：参考线；
    - set_candles/append_candle_tick/set_candles_visible：no-op（K 线退役）。
    """

    sig_k_changed = QtCore.Signal(float)

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)

        # ---- 数据（唯一真源：两个平行 numpy 缓冲，只追加） ----
        self._buf_t = np.empty(0, dtype=float)
        self._buf_v = np.empty(0, dtype=float)
        self._bar_sec = _H1          # 桶宽（set_series 从相邻时间差推导）
        self._window_sec = 8 * 3600.0   # 显式视窗（秒）；"全部"时为 None
        self._curve_dirty = False
        # 阈值参数默认值（先于 set_thresholds 的首绘不能裸访问）
        self._mean = 0.0
        self._std = 1e-12
        self._k = 2.0
        self._grid = 0.5
        self._levels = 3
        self._thr_sig = None
        self._ext_sig = None

        # ---- 顶部条：窗口选择 + 图例说明 ----
        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        top = QtWidgets.QHBoxLayout()
        top.setContentsMargins(4, 2, 4, 0)
        self._win_combo = QtWidgets.QComboBox(self)
        for label, _sec in _WINDOW_CHOICES:
            self._win_combo.addItem(label)
        self._win_combo.setCurrentIndex(1)   # 默认 近8小时
        self._win_combo.currentIndexChanged.connect(self._on_window_changed)
        top.addWidget(self._win_combo)
        top.addStretch(1)
        self._legend_label = QtWidgets.QLabel(
            '<span style="color:#4d8df0">— 价差（M1合成）</span>&nbsp;&nbsp;'
            '<span style="color:#c8cfda">─ 均值</span>&nbsp;&nbsp;'
            '<span style="color:#ea3943">─ 上阈值（拖动调k）</span>&nbsp;&nbsp;'
            '<span style="color:#16c784">─ 下阈值</span>&nbsp;&nbsp;'
            '<span style="color:#f0b90b">·· 极值</span>'
        )
        self._legend_label.setTextFormat(QtCore.Qt.RichText)
        top.addWidget(self._legend_label)
        lay.addLayout(top)

        # ---- 主图：轴固定（底=日期，左=定点小数） ----
        self._plot = pg.PlotWidget(
            axisItems={"bottom": pg.DateAxisItem(orientation="bottom"),
                       "left": _FixedFmtAxis(orientation="left")}
        )
        lay.addWidget(self._plot)
        self._plot.setBackground(_BG)
        self._pl = self._plot.plotItem
        self._pl.showGrid(x=True, y=True, alpha=0.15)
        self._pl.setDownsampling(mode="peak")
        self._pl.setClipToView(True)
        self._vb = self._pl.vb
        # 关键：禁用图内 pan/zoom —— 视图 100% 由重绘显式设定，无隐藏状态
        self._vb.setMouseEnabled(x=False, y=False)

        # ---- 序列与参考线（全部静态，生命周期=widget） ----
        self._curve_spread = self._plot.plot(
            pen=pg.mkPen(_SPREAD, width=1.4), name="价差", antialias=False
        )
        self._curve_spread.setDownsampling(method="peak")
        self._curve_spread.setClipToView(True)
        self._curve_spread.setZValue(5)
        self._curve_mean = self._plot.plot(
            pen=pg.mkPen(_MEAN, width=1, style=QtCore.Qt.DashLine),
            name="均值", antialias=False,
        )
        self._curve_mean.setZValue(6)
        self._line_upper = pg.InfiniteLine(
            pos=0.0, angle=0, movable=True,
            pen=pg.mkPen(_K_UP, width=1), hoverPen=pg.mkPen(_K_UP, width=2),
        )
        self._line_upper.sigDragged.connect(self._on_upper_dragged)
        self._plot.addItem(self._line_upper)
        self._line_lower = pg.InfiniteLine(
            pos=0.0, angle=0, movable=True,
            pen=pg.mkPen(_K_DOWN, width=1), hoverPen=pg.mkPen(_K_DOWN, width=2),
        )
        self._line_lower.sigDragged.connect(self._on_lower_dragged)
        self._plot.addItem(self._line_lower)

        self._tier_items: list[pg.InfiniteLine] = []
        self._thr_sig = None

        # 长周期极值线 + 注记
        self._extreme_up = pg.InfiniteLine(
            pos=0.0, angle=0, movable=False,
            pen=pg.mkPen(_EXTREME, width=1, style=QtCore.Qt.DotLine))
        self._extreme_dn = pg.InfiniteLine(
            pos=0.0, angle=0, movable=False,
            pen=pg.mkPen(_EXTREME, width=1, style=QtCore.Qt.DotLine))
        self._extreme_up.hide(); self._extreme_dn.hide()
        self._plot.addItem(self._extreme_up)
        self._plot.addItem(self._extreme_dn)
        self._extreme_legend_item = None
        self._extreme_legend_label = "全窗极值"
        self._ext_sig = None
        self._extreme_text = pg.TextItem("", color=_EXTREME, anchor=(0, 0))
        self._vb.addItem(self._extreme_text)

        # ---- 十字光标（只读、节流） ----
        self._cross_v = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen(_CROSS, width=1))
        self._cross_h = pg.InfiniteLine(angle=0, movable=False, pen=pg.mkPen(_CROSS, width=1))
        for it in (self._cross_v, self._cross_h):
            self._plot.addItem(it)
            it.hide()
        self._cross_text = pg.TextItem("", color="#dde3ec", anchor=(1, 1))
        self._cross_text.setZValue(30)
        self._vb.addItem(self._cross_text)
        self._cross_text.hide()
        self._cross_last = 0.0

        self._plot.scene().sigMouseMoved.connect(self._on_mouse_moved)

        # ---- 重绘合并定时器（唯一重绘入口） ----
        self._redraw_timer = QtCore.QTimer(self)
        self._redraw_timer.setSingleShot(True)
        self._redraw_timer.timeout.connect(self._on_redraw_tick)

    # ==================================================================
    # 数据接口（顺序约定：set_series 全量替换 → append_point 增点 → 重绘）
    # ==================================================================
    def set_series(self, t: list[float], v: list[float]) -> None:
        """历史全量替换（M1/H1 折线点）。桶宽从相邻时间差自动推导。"""
        t_l = [float(x) for x in (t or [])]
        v_l = [float(x) for x in (v or [])]
        n = min(len(t_l), len(v_l))
        if n >= 2 and t_l[1] > t_l[0]:
            self._bar_sec = float(t_l[1] - t_l[0])
        self._buf_t = np.array(t_l[:n], dtype=float)
        self._buf_v = np.array(v_l[:n], dtype=float)
        self._redraw_now()

    def append_point(self, t: float, v: float) -> None:
        """live 增点：写缓冲 + 轻量跟随奖励（右缘自动含最新点）。"""
        t_f, v_f = float(t), float(v)
        if self._buf_t.size and t_f <= float(self._buf_t[-1]):
            # 过期/同刻点直接更新末值（live 重票场景），不追加
            self._buf_v[-1] = v_f
            self._schedule_redraw()
            return
        n = self._buf_t.size
        self._buf_t = np.append(self._buf_t, t_f)
        self._buf_v = np.append(self._buf_v, v_f)
        if n % 512 == 0:
            pass
        self._schedule_redraw()

    def _schedule_redraw(self) -> None:
        if not self._redraw_timer.isActive():
            self._redraw_timer.start(_REDRAW_MS)

    def _on_redraw_tick(self) -> None:
        if self._curve_dirty:
            self._curve_dirty = False
        self._redraw_now()

    def clear_series(self) -> None:
        """清空序列并隐藏全部依赖数据的标记。"""
        self._buf_t = np.empty(0, dtype=float)
        self._buf_v = np.empty(0, dtype=float)
        self._curve_dirty = False
        self._extreme_text.setText("")
        self._cross_text.hide()
        self._cross_v.hide()
        self._cross_h.hide()
        for line in (self._curve_spread, self._curve_mean):
            line.clear()
        self._extreme_up.hide()
        self._extreme_dn.hide()

    # ==================================================================
    # K 线已退役（保留接口 no-op，防历史调用点断链）
    # ==================================================================
    def set_candles(self, t, o, h, l, c) -> None:  # noqa: N803
        return

    def append_candle_tick(self, ts: float, price: float) -> None:
        return

    def set_candles_visible(self, visible: bool) -> None:
        return

    # ==================================================================
    # 参考线（阈值/层间/极值）
    # ==================================================================
    def set_thresholds(
        self, mean: float, std: float, k: float,
        grid: float, levels: int, show_levels: bool = True,
    ) -> None:
        """均值线、±kσ 可拖动线、层间阈值线。参数未变早退（tick 级滚动调用）。"""
        self._mean = float(mean)
        self._std = max(float(std), 1e-12)
        self._k = float(k)
        self._grid = float(grid)
        self._levels = max(int(levels), 1)

        upper = self._mean + self._k * self._std
        lower = self._mean - self._k * self._std
        sig = (self._mean, self._std, self._k, self._grid,
               self._levels, bool(show_levels))
        if sig == self._thr_sig:
            with QtCore.QSignalBlocker(self._line_upper):
                self._line_upper.setPos(upper)
            with QtCore.QSignalBlocker(self._line_lower):
                self._line_lower.setPos(lower)
            return
        self._thr_sig = sig

        with QtCore.QSignalBlocker(self._line_upper):
            self._line_upper.setPos(upper)
        with QtCore.QSignalBlocker(self._line_lower):
            self._line_lower.setPos(lower)

        for item in self._tier_items:
            self._plot.removeItem(item)
        self._tier_items.clear()
        if show_levels:
            for i in range(1, self._levels):
                for value, color in (
                    (upper + i * self._grid * self._std, _K_UP),
                    (lower - i * self._grid * self._std, _K_DOWN),
                ):
                    line = pg.InfiniteLine(
                        pos=value, angle=0, movable=False,
                        pen=pg.mkPen(color, width=1, style=QtCore.Qt.DotLine))
                    self._plot.addItem(line)
                    self._tier_items.append(line)

    def set_extremes(self, extremes: dict, label: str = "全窗极值") -> None:
        """长窗极值线 + 注记；值/标签未变早退。"""
        label = label or self._extreme_legend_label
        keys = ("max_1y", "min_1y", "max_6m", "min_6m")
        sig = (label,) + tuple(
            None if extremes.get(k) is None else round(float(extremes[k]), 6)
            for k in keys
        )
        if sig == self._ext_sig:
            return
        self._ext_sig = sig
        self._extreme_legend_label = label

        def _fmt(key: str) -> str:
            value = extremes.get(key)
            return "-" if value is None else f"{float(value):.5f}"

        highs = [float(extremes[k]) for k in ("max_1y", "max_6m")
                 if extremes.get(k) is not None]
        lows = [float(extremes[k]) for k in ("min_1y", "min_6m")
                if extremes.get(k) is not None]
        if highs:
            self._extreme_up.setPos(max(highs)); self._extreme_up.show()
        else:
            self._extreme_up.hide()
        if lows:
            self._extreme_dn.setPos(min(lows)); self._extreme_dn.show()
        else:
            self._extreme_dn.hide()
        self._extreme_text.setText(
            f"{label}\n1y {_fmt('max_1y')} / {_fmt('min_1y')}"
            f"   6m {_fmt('max_6m')} / {_fmt('min_6m')}"
        )

    # ==================================================================
    # 核心重绘（显式量度，无 autoRange；x 只取决于窗口选择，y 只看窗口内数据）
    # ==================================================================
    def _redraw_now(self) -> None:
        try:
            self._redraw_visible()
        except Exception:
            import sys, traceback
            traceback.print_exc(file=sys.stderr)   # 图绝不因偶发异常拖死 UI

    def _redraw_visible(self) -> None:
        n = int(self._buf_t.size)
        if not n:
            self._curve_spread.clear()
            self._curve_mean.clear()
            for item in (self._extreme_up, self._extreme_dn, self._extreme_text):
                item.hide()
            return
        t_arr = self._buf_t
        v_arr = self._buf_v
        t_last = float(t_arr[-1])
        bar = max(self._bar_sec, 1.0)

        # ---- 显式 X 窗口 ----
        if self._window_sec is None:
            x_start = float(t_arr[0])
        else:
            x_start = max(float(t_arr[0]), t_last - self._window_sec)
        i0 = int(np.searchsorted(t_arr, x_start, side="left"))
        segment_t = t_arr[max(i0 - 1, 0):n]     # 左半个缺口点保留首接缝
        segment_v = v_arr[max(i0 - 1, 0):n]

        # ---- 曲线（只传窗口切片） ----
        self._curve_spread.setData(segment_t, segment_v)
        mean = float(getattr(self, "_mean", 0.0) or 0.0)
        if segment_t.size:
            self._curve_mean.setData(segment_t, np.full(segment_t.size, mean))
        else:
            self._curve_mean.clear()

        # ---- 显式 Y 窗口（只看窗口内数据，8% 边距；阈值沿在窗口内也纳入） ----
        mean = float(getattr(self, "_mean", 0.0) or 0.0)
        std = max(float(getattr(self, "_std", 1e-12) or 1e-12), 1e-12)
        k = float(getattr(self, "_k", 2.0))
        if segment_v.size:
            finite = segment_v[np.isfinite(segment_v)]
            v_lo = float(np.min(finite)) if finite.size else 0.0
            v_hi = float(np.max(finite)) if finite.size else 0.0
            for val in (mean + k * std, mean - k * std):
                v_lo = min(v_lo, val); v_hi = max(v_hi, val)
            span = max(v_hi - v_lo, abs(v_hi or 1.0) * 0.02, 1e-9)
            pad_y = span * 0.08
            self._vb.setXRange(x_start, t_last + bar * 3.0, padding=0)
            self._vb.setYRange(v_lo - pad_y, v_hi + pad_y, padding=0)

        # ---- 极值注记位置（窗口左上） ----
        if any(it.isVisible() for it in (self._extreme_up, self._extreme_dn)):
            self._extreme_text.setPos(x_start, v_hi + pad_y * 0.2)
            self._extreme_text.show()

    # ==================================================================
    # 交互（阈值拖动 / 只读十字光标）
    # ==================================================================
    def _on_upper_dragged(self) -> None:
        if self._std <= 0:
            return
        pos = float(self._line_upper.getPos()[1])
        self.sig_k_changed.emit((pos - self._mean) / self._std)

    def _on_lower_dragged(self) -> None:
        if self._std <= 0:
            return
        pos = float(self._line_lower.getPos()[1])
        self.sig_k_changed.emit((self._mean - pos) / self._std)

    def _on_window_changed(self, *_a) -> None:
        choice = _WINDOW_CHOICES[self._win_combo.currentIndex()]
        self._window_sec = choice[1]
        self._redraw_now()

    def _on_mouse_moved(self, scene_pos) -> None:
        """只读十字：实时线跟随鼠标；读数钉在视口右上角，40ms 节流或跨点更新。"""
        if not self._plot.sceneBoundingRect().contains(scene_pos):
            self._cross_text.hide()
            self._cross_v.hide()
            self._cross_h.hide()
            return
        n = int(self._buf_t.size)
        if not n:
            return
        try:
            mouse = self._vb.mapSceneToView(scene_pos)
            x, y = float(mouse.x()), float(mouse.y())
        except Exception:
            return
        self._cross_v.setPos(x)
        self._cross_h.setPos(y)
        self._cross_v.show()
        self._cross_h.show()

        idx = bisect.bisect_left(self._buf_t.tolist(), x)
        idx = min(max(idx, 0), n - 1)
        now_mono = time.monotonic()
        if now_mono - self._cross_last >= 0.040:   # 40ms 节流（重排昂贵）
            self._cross_last = now_mono
            t_at = float(self._buf_t[idx])
            v_at = float(self._buf_v[idx])
            ts = dt.datetime.fromtimestamp(t_at).strftime("%Y-%m-%d %H:%M")
            self._cross_text.setText(f"{ts}\n价差 {v_at:+.5f}")
        xr, yr = self._vb.viewRange()
        self._cross_text.setPos(xr[1] - 8.0, yr[0] + 10.0)
        self._cross_text.show()
