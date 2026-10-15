"""Qt spread chart: bounded normalized data, explicit follow/manual view modes."""

from __future__ import annotations

import datetime as dt
import math
import time

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtWidgets

from ..series_data import SeriesBuffer, peak_envelope

_BG = "#12161d"
_SPREAD = "#4d8df0"
_MEAN = "#4d8df0"
_K_UP = "#ea3943"     # 上阈值 / 卖出进场
_K_DOWN = "#16c784"   # 下阈值 / 买入进场
_EXTREME = "#f0b90b"
_CROSS = (255, 255, 255, 70)

_H1 = 3600.0    # 默认桶宽（秒），实际由 set_series 从数据自动推导
_REDRAW_MS = 33    # 合并同一帧更新；不是行情或成交延迟保证


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
            values = [v * scale for v in values]
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
    """价差实时折线图（固定窗口跟随 + 可暂停的手动缩放/平移）。

    对外接口（与 console 完全兼容）：
    - sig_k_changed(k)：拖动上阈值线 → 反写 k；
    - set_series(t, v) / append_point(t, v)/clear_series()：数据；
    - set_thresholds(...)/set_extremes(...)：参考线；
    """

    sig_k_changed = QtCore.Signal(float)

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)

        # ---- 数据（唯一真源：两个平行 numpy 缓冲，只追加） ----
        self._buffer = SeriesBuffer()
        self._bar_sec = _H1          # 桶宽（set_series 从相邻时间差推导）
        self._window_sec = 8 * 3600.0   # 显式视窗（秒）；"全部"时为 None
        self._curve_dirty = False
        self._follow = True
        # 阈值参数默认值（先于 set_thresholds 的首绘不能裸访问）
        self._mean = 0.0
        self._std = 1e-12
        self._k = 2.0
        self._grid = 0.5
        self._levels = 3
        self._thr_sig: tuple | None = None
        self._ext_sig: tuple | None = None

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
        self._follow_button = QtWidgets.QPushButton("跟随最新")
        self._follow_button.clicked.connect(self._resume_follow)
        top.addWidget(self._follow_button)
        self._error_label = QtWidgets.QLabel("")
        top.addWidget(self._error_label)
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
            axisItems={"bottom": pg.DateAxisItem(orientation="bottom", utcOffset=0),
                       "left": _FixedFmtAxis(orientation="left")}
        )
        lay.addWidget(self._plot)
        self._plot.setBackground(_BG)
        self._pl = self._plot.plotItem
        self._pl.getAxis("left").enableAutoSIPrefix(False)
        self._pl.showGrid(x=True, y=True, alpha=0.15)
        # Reduce by screen time bins ourselves: history + live ticks are not
        # uniformly spaced, as pyqtgraph's auto-downsampling assumes.
        self._pl.setDownsampling(ds=1, auto=False, mode="peak")
        self._pl.setClipToView(True)
        self._vb = self._pl.vb
        # Follow mode owns ranges; manual pan/zoom pauses follow until reset.
        self._vb.disableAutoRange()
        self._vb.setMouseEnabled(x=True, y=True)
        self._vb.sigRangeChangedManually.connect(self._manual_range)

        # ---- 序列与参考线（全部静态，生命周期=widget） ----
        self._curve_spread = self._plot.plot(
            pen=pg.mkPen(_SPREAD, width=1.4), name="价差", antialias=False
        )
        self._curve_spread.setDownsampling(ds=1, auto=False, method="peak")
        self._curve_spread.setClipToView(True)
        # SeriesBuffer rejects non-finite inputs before they reach the renderer.
        self._curve_spread.setSkipFiniteCheck(True)
        self._curve_spread.setZValue(5)
        self._curve_mean = self._plot.plot(
            pen=pg.mkPen(_MEAN, width=1, style=QtCore.Qt.DashLine),
            name="均值", antialias=False,
        )
        self._curve_mean.setZValue(6)
        self._curve_mean.setDownsampling(ds=1, auto=False)
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
        self._line_upper.hide()
        self._line_lower.hide()
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
        self._extreme_up.hide()
        self._extreme_dn.hide()
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
    @property
    def _buf_t(self):
        return self._buffer.times

    @property
    def _buf_v(self):
        return self._buffer.values

    def set_series(self, t, v, preserve_live=False) -> None:
        self._buffer.replace(t, v, preserve_live)
        diffs = np.diff(self._buf_t)
        self._bar_sec = float(np.median(diffs)) if len(diffs) else 60.0
        self._redraw_now()

    def append_point(self, t: float, v: float) -> None:
        if self._buffer.append(t, v):
            self._schedule_redraw()

    def _manual_range(self, *_args) -> None:
        self._follow = False
        self._follow_button.setText("恢复跟随")
        self._schedule_redraw()

    def _resume_follow(self) -> None:
        self._follow = True
        self._follow_button.setText("跟随最新")
        self._redraw_now()

    def dispose(self) -> None:
        self._redraw_timer.stop()
        try:
            self._plot.scene().sigMouseMoved.disconnect(self._on_mouse_moved)
        except (RuntimeError, TypeError):
            pass

    def closeEvent(self, event) -> None:  # noqa: N802
        self.dispose()
        super().closeEvent(event)

    def _schedule_redraw(self) -> None:
        if not self._redraw_timer.isActive():
            self._redraw_timer.start(_REDRAW_MS)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if hasattr(self, '_redraw_timer'):
            self._schedule_redraw()  # Recompute the pixel budget for the viewport.

    def _on_redraw_tick(self) -> None:
        if self._curve_dirty:
            self._curve_dirty = False
        self._redraw_now()

    def clear_series(self) -> None:
        """清空序列并隐藏全部依赖数据的标记。"""
        self._redraw_timer.stop()
        self._buffer.clear()
        self._thr_sig = self._ext_sig = None
        self._bar_sec = 60.0
        self._follow = True
        self._line_upper.hide()
        self._line_lower.hide()
        for item in self._tier_items:
            self._plot.removeItem(item)
        self._tier_items.clear()
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
    # 参考线（阈值/层间/极值）
    # ==================================================================
    def set_thresholds(
        self, mean: float, std: float, k: float,
        grid: float, levels: int, show_levels: bool = True,
    ) -> None:
        """均值线、±kσ 可拖动线、层间阈值线。参数未变早退（tick 级滚动调用）。"""
        if not all(math.isfinite(float(x)) for x in (mean, std, k, grid)):
            raise ValueError("阈值必须为有限数值")
        self._line_upper.show()
        self._line_lower.show()
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

        values = []
        if show_levels:
            for i in range(1, self._levels):
                values.extend(((upper + i * self._grid * self._std, _K_UP),
                               (lower - i * self._grid * self._std, _K_DOWN)))
        while len(self._tier_items) > len(values):
            self._plot.removeItem(self._tier_items.pop())
        for i, (value, color) in enumerate(values):
            if i == len(self._tier_items):
                line = pg.InfiniteLine(angle=0, movable=False,
                                      pen=pg.mkPen(color, width=1, style=QtCore.Qt.DotLine))
                self._plot.addItem(line)
                self._tier_items.append(line)
            self._tier_items[i].setPos(value)
            self._tier_items[i].show()
        self._schedule_redraw()

    def set_extremes(self, extremes: dict, label: str = "全窗极值") -> None:
        """长窗极值线 + 注记；值/标签未变早退。"""
        extremes = {k: v for k, v in extremes.items()
                    if v is not None and math.isfinite(float(v))}
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
            self._extreme_up.setPos(max(highs))
            self._extreme_up.show()
        else:
            self._extreme_up.hide()
        if lows:
            self._extreme_dn.setPos(min(lows))
            self._extreme_dn.show()
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
            self._error_label.clear()
        except Exception as exc:
            self._error_label.setText(f"图表更新失败：{exc}")
            import sys
            import traceback
            traceback.print_exc(file=sys.stderr)   # 图绝不因偶发异常拖死 UI

    def _redraw_visible(self) -> None:
        n = int(self._buf_t.size)
        if not n:
            self._curve_spread.clear()
            self._curve_mean.clear()
            self._line_upper.hide()
            self._line_lower.hide()
            self._thr_sig = self._ext_sig = None
            for item in self._tier_items:
                item.hide()
            for item in (self._extreme_up, self._extreme_dn, self._extreme_text):
                item.hide()
            return
        t_arr = self._buf_t
        v_arr = self._buf_v
        t_last = float(t_arr[-1])
        bar = max(self._bar_sec, 1.0)

        # ---- 显式 X 窗口 ----
        if not self._follow:
            x_start, x_end = self._vb.viewRange()[0]
        elif self._window_sec is None:
            x_start = float(t_arr[0])
        else:
            x_start = max(float(t_arr[0]), t_last - self._window_sec)
        i0 = int(np.searchsorted(t_arr, x_start, side="left"))
        end = n if self._follow else min(n, int(np.searchsorted(t_arr, x_end)) + 1)
        segment_t = t_arr[max(i0 - 1, 0):end]     # 左半个缺口点保留首接缝
        segment_v = v_arr[max(i0 - 1, 0):end]

        # ---- 曲线（按屏幕列保留真实极值，完整缓冲继续用于统计/十字） ----
        right = t_last + bar * 3.0 if self._follow else x_end
        render_t, render_v = peak_envelope(segment_t, segment_v, x_start, right,
                                           max(1, int(self._vb.width())))
        self._curve_spread.setData(render_t, render_v)
        mean = float(getattr(self, "_mean", 0.0) or 0.0)
        if segment_t.size and self._thr_sig:
            self._curve_mean.setData([segment_t[0], segment_t[-1]], [mean, mean])
        else:
            self._curve_mean.clear()

        # ---- 显式 Y 窗口（只看窗口内数据，8% 边距；阈值沿在窗口内也纳入） ----
        mean = float(getattr(self, "_mean", 0.0) or 0.0)
        std = max(float(getattr(self, "_std", 1e-12) or 1e-12), 1e-12)
        k = float(getattr(self, "_k", 2.0))
        if segment_v.size:
            v_lo = float(np.min(segment_v))
            v_hi = float(np.max(segment_v))
            for val in ((mean + k * std, mean - k * std) if self._thr_sig else ()):
                v_lo = min(v_lo, val)
                v_hi = max(v_hi, val)
            span = max(v_hi - v_lo, abs(v_hi or 1.0) * 0.02, 1e-9)
            pad_y = span * 0.08
            if self._follow:
                self._vb.setXRange(x_start, t_last + bar * 3.0, padding=0)
                self._vb.setYRange(v_lo - pad_y, v_hi + pad_y, padding=0)

        # ---- 极值注记位置（窗口左上） ----
        if segment_v.size and any(it.isVisible() for it in (self._extreme_up, self._extreme_dn)):
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
        self._resume_follow()

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

        idx = int(np.searchsorted(self._buf_t, x))
        idx = min(max(idx, 0), n - 1)
        if idx > 0 and abs(x - self._buf_t[idx - 1]) < abs(x - self._buf_t[idx]):
            idx -= 1
        now_mono = time.monotonic()
        if now_mono - self._cross_last >= 0.040:   # 40ms 节流（重排昂贵）
            self._cross_last = now_mono
            t_at = float(self._buf_t[idx])
            v_at = float(self._buf_v[idx])
            ts = dt.datetime.fromtimestamp(t_at, dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            self._cross_text.setText(f"{ts}\n价差 {v_at:+.5f}")
        xr, yr = self._vb.viewRange()
        self._cross_text.setPos(xr[1] - (xr[1] - xr[0]) * 0.01,
                                yr[0] + (yr[1] - yr[0]) * 0.03)
        self._cross_text.show()
