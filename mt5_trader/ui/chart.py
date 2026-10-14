"""可复用价差图表组件（pyqtgraph）。

职责：
- 展示历史价差序列 + 均值/±kσ 阈值/网格层间阈值/长周期极值；
- 上下阈值线可拖动，拖动上阈值时通过 sig_k_changed 发出新 k 值；
- 十字光标跟随鼠标（时间/价差值）。

数据窗口口径：
- 数据窗口完全由调用方控制（set_series 全量替换 / append_point 增点），
  本组件内部不做任何滑动窗口裁剪，调用方自行决定保留多少点。

配色与主题（theme.Cloud COLORS）对齐：
- 背景 #12161d（BG_TABLE）、网格 #202836（GRID）、价差/均值 #4d8df0（ACCENT）、
  上阈值 #ea3943（DOWN）、下阈值 #16c784（UP）、极值 #f0b90b（WARN）。
"""

from __future__ import annotations

import bisect
import datetime as dt
import math
import time

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets

# 本组件固定用色（与 theme.py 调色板 Token 同值，此处独立常量避免引入主题耦合）
_BG = "#12161d"
_GRID = "#202836"
_SPREAD = "#4d8df0"
_MEAN = "#4d8df0"
_K_UP = "#ea3943"     # 上阈值 / 卖出进场方向
_K_DOWN = "#16c784"   # 下阈值 / 买入进场方向
_EXTREME = "#f0b90b"
_CROSS = (255, 255, 255, 70)

# 蜡烛笔刷（模块级复用，避免每块重创建）
_PEN_UP = QtGui.QPen(QtGui.QColor(_K_DOWN), 1)      # 阳线绿
_PEN_DN = QtGui.QPen(QtGui.QColor(_K_UP), 1)        # 阴线红
_BRUSH_UP = QtGui.QBrush(QtGui.QColor(22, 199, 132, 120))
_BRUSH_DN = QtGui.QBrush(QtGui.QColor(234, 57, 67, 120))

_DAY = 86_400.0


_H1 = 3600.0   # 蜡烛周期（与价差历史口径一致：H1）
_PICTURE_STEP = 600   # 蜡烛 QPicture 分块步长；重绘只重放可见块
_VIEW_BARS = 1200     # 载入后默认显示的最近蜡烛根数（更早历史可拖动/缩放查看）


class _CandlestickItem(pg.GraphicsObject):
    """组合价差 OHLC 蜡烛绘制件（模式同 vnpy CandleItem，但直接基于数组自绘）。

    阳线（收盘>=开盘）绿 #16c784、阴线红 #ea3943，实体半透。
    """

    def __init__(self) -> None:
        super().__init__()
        self._t: list[float] = []
        self._o: list[float] = []
        self._pending_bucket: float | None = None   # 当前未收线蜡烛桶起点
        self._bar_sec: float = _H1                   # 桶宽（秒）；set_candles 从数据推导
        self._h: list[float] = []
        self._l: list[float] = []
        self._c: list[float] = []
        self._w = None   # 蜡烛宽度（绘制时按可见范围估算）
        self._paths: list[tuple[bool, QtGui.QPainterPath, QtGui.QPainterPath]] | None = None

    def set_candles(
        self, t: list[float], o: list[float], h: list[float],
        l: list[float], c: list[float],
    ) -> None:
        self._t = [float(x) for x in (t or [])]
        self._o = [float(x) for x in (o or [])]
        self._h = [float(x) for x in (h or [])]
        self._l = [float(x) for x in (l or [])]
        self._c = [float(x) for x in (c or [])]
        self._pending_bucket = None
        # 蜡烛桶宽（秒）从数据推导：M1=60、H1=3600 —— 实时聚合/pending/窗口跟随
        if len(self._t) >= 2 and self._t[1] > self._t[0]:
            self._bar_sec = float(self._t[1] - self._t[0])
        self._paths = None   # 全量失效重绘
        self.update()

    def update_last(self, price: float, ts: float, bucket: float | None = None) -> None:
        """live tick 聚合：更新最后一根蜡烛（h/l/c），跨入新桶则新增一根；桶宽取数据推导值。"""
        if not self._t:
            return
        if ts < self._t[-1] - 1e-9:
            return   # 过期 tick（历史还没重载时可能出现）
        bucket = bucket or self._bar_sec
        current_open = self._t[-1]
        if ts >= current_open + bucket:
            # 新蜡烛：O=H=L=C=price（无盘中细节，仅点级精度）
            self._t.append(math.floor(ts / bucket) * bucket)
            self._o.append(price); self._h.append(price)
            self._l.append(price); self._c.append(price)
            self._pending_bucket = self._t[-1]
        else:
            price = float(price)
            self._h[-1] = max(self._h[-1], price)
            self._l[-1] = min(self._l[-1], price)
            self._c[-1] = price
            self._pending_bucket = self._t[-1]
        self._invalidate_last_block()   # 只有最后一根变；其他块缓存留在位
        self.update()

    def pending_info(self):
        """未收线的蜡烛 info：（桶起点, 顶部最高值, 本地时间 HH:MM）或 None。"""
        if not self._t or self._pending_bucket is None:
            return None
        i = self._t.index(self._pending_bucket) if self._pending_bucket in self._t else len(self._t) - 1
        if i >= len(self._t):
            return None
        top = max(self._o[i], self._h[i], self._l[i], self._c[i])
        hhmm = dt.datetime.fromtimestamp(self._pending_bucket + self._bar_sec).strftime("%H:%M")
        return (self._t[i], top, hhmm)


    def clear(self) -> None:
        self._t.clear(); self._o.clear(); self._h.clear()
        self._l.clear(); self._c.clear()
        self._paths = None
        self.update()

    def _invalidate_last_block(self) -> None:
        """live tick 只改最后一根 → 只作废最后一块缓存（原实现全量重建，tick 卡）。"""
        if self._paths:
            self._paths[-1] = (False, None, None)

    # ---- pyqtgraph 绘制协议 ----
    def boundingRect(self) -> QtCore.QRectF:  # noqa: N802
        if not self._t:
            return QtCore.QRectF()
        if self._w is None:
            self._w = (self._t[1] - self._t[0]) * 0.7 if len(self._t) > 1 else _H1 * 0.6
        x_min = self._t[0] - self._w
        x_max = self._t[-1] + self._w
        y_lo = min(min(self._l), min(self._o), min(self._c))
        y_hi = max(max(self._h), max(self._o), max(self._c))
        return QtCore.QRectF(x_min, y_lo, x_max - x_min, y_hi - y_lo)

    def paint(self, painter: QtGui.QPainter, option, widget=None) -> None:  # noqa: N802
        if not self._t:
            return
        # 只重绘可见区段对应块（blazing fast 的直接来源：
        # 十字线/光标每帧仅触发所在窗口的个别块，而非全量；
        # live tick 只使最后一块失效 —— 修改后 tick 重造成本从
        # 9599 根降为 600 根）
        try:
            vr = self.viewRect()
            x_lo, x_hi = vr.left(), vr.right()
        except Exception:
            x_lo, x_hi = self._t[0], self._t[-1]
        if not (x_hi > x_lo) or x_hi < self._t[0] or x_lo > self._t[-1]:
            return
        half = self._w if isinstance(self._w, float) else _H1
        i0 = max(bisect.bisect_left(self._t, x_lo - half), 0)
        i1 = min(bisect.bisect_right(self._t, x_hi + half), len(self._t) - 1)
        n_blocks = (len(self._t) + _PICTURE_STEP - 1) // _PICTURE_STEP
        if self._paths is None:
            self._paths = [(False, None, None)] * n_blocks
        elif len(self._paths) != n_blocks:
            if len(self._paths) < n_blocks:
                self._paths = self._paths + [(False, None, None)] * (n_blocks - len(self._paths))
            else:
                self._paths = self._paths[:n_blocks]
        b0 = i0 // _PICTURE_STEP
        b1 = i1 // _PICTURE_STEP
        last = min(b1, n_blocks - 1)
        for bi in range(b0, last + 1):
            valid, up, down = self._paths[bi]
            if not valid:
                valid, up, down = self._generate_block(bi)
                self._paths[bi] = (valid, up, down)
            painter.setPen(_PEN_UP); painter.setBrush(_BRUSH_UP)
            painter.drawPath(up)
            painter.setPen(_PEN_DN); painter.setBrush(_BRUSH_DN)
            painter.drawPath(down)

    def _generate_block(self, bi: int):
        """重造指定块（up_path/down_path 双色二分，一次 drawPath 一块）。"""
        up = QtGui.QPainterPath()
        down = QtGui.QPainterPath()
        if self._w is None:
            self._w = (self._t[1] - self._t[0]) * 0.7 if len(self._t) > 1 else _H1 * 0.6
        half = self._w / 2
        start = bi * _PICTURE_STEP
        for i in range(start, min(start + _PICTURE_STEP, len(self._t))):
            t, o, h, low, c = (
                self._t[i], self._o[i], self._h[i], self._l[i], self._c[i]
            )
            path = up if c >= o else down
            # 影线 + 实体合入同一个 path（一次 drawPath fill+stroke）
            path.moveTo(t, h)
            path.lineTo(t, low)
            body_top = max(o, c)
            body_bottom = min(o, c)
            body_h = max(body_top - body_bottom, abs(o + c) * 1e-9)
            if body_h > 0:
                path.addRect(
                    QtCore.QRectF(t - half, body_bottom, self._w, body_h)
                )
            else:
                path.moveTo(t - half, body_bottom)
                path.lineTo(t + half, body_bottom)
        return (True, up, down)


class SpreadChart(QtWidgets.QWidget):
    """价差序列图（pyqtgraph PlotWidget 封装，可嵌入任意策略台/页面）。"""

    sig_k_changed = QtCore.Signal(float)  # 用户拖动上阈值线 -> 发出新的 k 值

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)

        # 图形状态
        self._t: list[float] = []
        self._v: list[float] = []
        self._mean = 0.0
        self._std = 1e-12
        self._k = 2.0
        self._grid = 0.5
        self._levels = 3
        self._curve_dirty = False
        self._bar_sec = _H1   # 当前蜡烛桶宽（秒）；set_candles 跟随数据推导（M1=60）

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        # 图例说明条（不能用 pg LegendItem：pyqtgraph 0.14 的 ItemSample.paint
        # 直接取 item.opts，对 InfiniteLine/自绘蜡烛会 AttributeError →
        # Qt 渲染期异常风暴 → 段错误；且此前 PlotWidget 从未 addWidget 进布局，
        # 导致整个图表一曲空白灰底不渲染）。用顶部 QLabel 图例代替。
        self._legend_label = QtWidgets.QLabel(
            '<span style="color:#4d8df0">— 价差/均值</span>&nbsp;&nbsp;&nbsp;'
            '<span style="color:#ea3943">─ 上阈值（拖动调 k）</span>&nbsp;&nbsp;&nbsp;'
            '<span style="color:#16c784">─ 下阈值</span>&nbsp;&nbsp;&nbsp;'
            '<span style="color:#16c784">▕▏</span><span style="color:#ea3943">▕▏</span>'
            '<span style="color:#c8cfda"> 价差K线(H1)</span>'
        )
        self._legend_label.setTextFormat(QtCore.Qt.RichText)
        self._legend_label.setStyleSheet(
            f"color:#c8cfda; background:{_BG}; padding:1px 4px; font-size:11px;"
        )
        lay.addWidget(self._legend_label)

        self._plot = pg.PlotWidget(
            axisItems={"bottom": pg.DateAxisItem(orientation="bottom")}
        )
        lay.addWidget(self._plot)
        self._plot.setBackground(_BG)
        self._pl = self._plot.plotItem
        self._pl.showGrid(x=True, y=True, alpha=0.15)
        self._pl.setDownsampling(mode="peak")
        self._pl.setClipToView(True)
        self._pl.showButtons()
        self._vb = self._pl.vb

        # ---- 序列与阈值图形 ----
        self._curve_spread = self._plot.plot(
            pen=pg.mkPen(_SPREAD, width=1.4), name="价差", antialias=False
        )
        self._curve_spread.setDownsampling(method="peak")
        self._curve_spread.setClipToView(True)
        self._curve_spread.setZValue(5)   # M1 蜡烛半透明墙会遮线：线浮上层
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

        # 网格层间阈值线（DotLine 细线，动态重建）
        self._tier_items: list[pg.InfiniteLine] = []

        # ---- 组合价差 OHLC 蜡烛（模式引 vnpy CandleItem/BarGenerator）----
        self._candles = _CandlestickItem()
        self._candles.setZValue(-1)   # 蜡烛垫底，价差线/阈值线在上层
        self._plot.addItem(self._candles)
        self._candles_visible = True

        # 长周期极值线 + 文本
        self._extreme_up = pg.InfiniteLine(
            pos=0.0, angle=0, movable=False, pen=pg.mkPen(_EXTREME, width=1, style=QtCore.Qt.DotLine)
        )
        self._extreme_dn = pg.InfiniteLine(
            pos=0.0, angle=0, movable=False, pen=pg.mkPen(_EXTREME, width=1, style=QtCore.Qt.DotLine)
        )
        self._extreme_up.hide()
        self._extreme_dn.hide()
        self._plot.addItem(self._extreme_up)
        self._plot.addItem(self._extreme_dn)
        self._extreme_legend_item: pg.InfiniteLine | None = None
        self._extreme_legend_label = "全窗极值"
        self._extreme_text = pg.TextItem("", color=_EXTREME, anchor=(0, 0))
        self._vb.addItem(self._extreme_text)

        # "开盘中" 游标（当前 H1 未收线）
        self._pending_text = pg.TextItem("", color=_K_UP, anchor=(0.5, 1))
        self._pending_text.hide()
        self._vb.addItem(self._pending_text)

        # ---- 十字光标 ----
        self._cross_v = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen(_CROSS, width=1))
        self._cross_h = pg.InfiniteLine(angle=0, movable=False, pen=pg.mkPen(_CROSS, width=1))
        for item in (self._cross_v, self._cross_h):
            self._plot.addItem(item)
            item.hide()
        self._cross_text = pg.TextItem("", color="#dde3ec", anchor=(1, 1))
        self._cross_text.setZValue(30)
        self._vb.addItem(self._cross_text)
        self._cross_text.hide()
        self._cross_bucket = None   # 最近光标所在 H1 桶（文本重排节流）
        self._cross_last = 0.0      # 上次文本重排的单调钟时刻（时间维度节流）
        self._cross_pending = False   # 跨桶后尚未重排的待显示项
        self._cross_flush_scheduled = False
        self._last_scene_pos = None

        self._plot.scene().sigMouseMoved.connect(self._on_mouse_moved)
        # 实时曲线 setData 限频定时器（200ms）；一欧几中的 flush 全量重设
        self._curve_flush = QtCore.QTimer(self)
        self._curve_flush.setSingleShot(True)
        self._curve_flush.timeout.connect(self._flush_curve)
        self._refresh_limits()

    # ==================================================================
    # 数据接口（窗口由调用方控制）
    # ==================================================================
    def set_series(self, t: list[float], v: list[float]) -> None:
        """历史全量替换：t=时间戳(s)，v=价差值。"""
        self._t = [float(x) for x in (t or [])]
        self._v = [float(x) for x in (v or [])]
        self._redraw()
        self._autorange_once()

    def append_point(self, t: float, v: float) -> None:
        """live 增点（追加）；setData 限频 200ms，避免每 tick 全量重建曲线。"""
        self._t.append(float(t))
        self._v.append(float(v))
        self._curve_dirty = True
        if not self._curve_flush.isActive():
            self._curve_flush.start(200)

    def _flush_curve(self) -> None:
        if self._curve_dirty:
            self._curve_dirty = False
            self._redraw()

    # ==================================================================
    # K线（组合价差 OHLC；模式引 vnpy ChartWizard：价差→蜡烛）
    # ==================================================================
    def set_candles(
        self,
        t: list[float], o: list[float], h: list[float],
        l: list[float], c: list[float],
    ) -> None:
        """历史蜡烛全量替换（等长 OHLC 时间序列）。桶宽自动跟随数据（M1/H1）。"""
        self._bar_sec = self._candles._bar_sec
        self._candles.set_candles(t, o, h, l, c)
        self._bar_sec = self._candles._bar_sec
        self._pending_text.hide()
        self._refresh_limits()
        self._autorange_once()

    def append_candle_tick(self, ts: float, price: float) -> None:
        """live tick 聚合到当前蜡烛（跨入新桶则新增一根，桶宽由数据推导）。
        等价于 vnpy ChartWizard 的 Tick→BarGenerator→update_bar 链路。"""
        self._candles.update_last(float(price), float(ts))
        self._update_pending_text()
        self._refresh_limits()

    def set_candles_visible(self, visible: bool) -> None:
        self._candles_visible = bool(visible)
        self._candles.setVisible(self._candles_visible)

    def clear_series(self) -> None:
        """清空序列并隐藏依赖数据的标记。"""
        self._t.clear()
        self._v.clear()
        self._candles.clear()
        self._pending_text.hide()
        self._extreme_text.setText("")
        self._cross_text.hide()
        self._cross_v.hide()
        self._cross_h.hide()
        self._redraw()

    def set_thresholds(
        self, mean: float, std: float, k: float,
        grid: float, levels: int, show_levels: bool = True,
    ) -> None:
        """更新统计参考线：均值、±kσ 可拖动线、网格层间阈值线。

        :param mean/std: 统计均值与标准差（实时窗口或先验，口径由调用方给）;
        :param k: 基础阈值倍数（拖动上阈值线时按 (pos-mean)/std 反算）;
        :param grid/levels: 网格步进 σ 与层数（层间线画 i=1..levels-1，避免与基础线重叠）;
        :param show_levels: 是否显示层间阈值线。
        """
        self._mean = float(mean)
        self._std = max(float(std), 1e-12)
        self._k = float(k)
        self._grid = float(grid)
        self._levels = max(int(levels), 1)

        upper = self._mean + self._k * self._std
        lower = self._mean - self._k * self._std
        # 参数与显示未变：只挪两根可拖线位置，不重建层间线（tick 逐撞同理），
        # 否则每 tick 重建 InfiniteLine 会让拖动/移动明显卡顿。
        sig = (self._mean, self._std, self._k, self._grid,
               self._levels, bool(show_levels))
        if sig == getattr(self, "_thr_sig", None):
            with QtCore.QSignalBlocker(self._line_upper):
                self._line_upper.setPos(upper)
            with QtCore.QSignalBlocker(self._line_lower):
                self._line_lower.setPos(lower)
            return
        self._thr_sig = sig
        # 程序化移动时屏蔽拖动信号，避免误发 sig_k_changed
        with QtCore.QSignalBlocker(self._line_upper):
            self._line_upper.setPos(upper)
        with QtCore.QSignalBlocker(self._line_lower):
            self._line_lower.setPos(lower)

        # 层间阈值：mean ± (k + i*grid)·std，i=1..levels-1
        for item in self._tier_items:
            self._plot.removeItem(item)
        self._tier_items.clear()
        if show_levels:
            for i in range(1, self._levels):
                level_z = self._k + i * self._grid
                for value, color in (
                    (upper + i * self._grid * self._std, _K_UP),
                    (lower - i * self._grid * self._std, _K_DOWN),
                ):
                    line = pg.InfiniteLine(
                        pos=value, angle=0, movable=False,
                        pen=pg.mkPen(color, width=1, style=QtCore.Qt.DotLine),
                    )
                    self._plot.addItem(line)
                    self._tier_items.append(line)
        self._refresh_limits()

    def set_extremes(self, extremes: dict, label: str = "全窗极值") -> None:
        """设置长周期极值线：max_1y/min_1y/max_6m/min_6m（值缺数据为 None）。

        画两根水平线（全部极值中的最大/最小，DotLine #f0b90b），
        左上角 TextItem 列出四个值，缺数据显示 "-"。
        """
        label = label or self._extreme_legend_label
        keys = ("max_1y", "min_1y", "max_6m", "min_6m")
        # 每 tick 都被调用：值/标签未变时早退（不 setText 不重排）
        sig = (label,) + tuple(
            None if extremes.get(k) is None else round(float(extremes[k]), 6)
            for k in keys
        )
        if sig == getattr(self, "_ext_sig", None):
            return
        self._ext_sig = sig
        self._extreme_legend_label = label

        def _fmt(key: str) -> str:
            value = extremes.get(key)
            return "-" if value is None else f"{float(value):.5f}"

        highs = [float(extremes[k]) for k in ("max_1y", "max_6m") if extremes.get(k) is not None]
        lows = [float(extremes[k]) for k in ("min_1y", "min_6m") if extremes.get(k) is not None]

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
            f"{label}\n1y {_fmt('max_1y')} / {_fmt('min_1y')}   6m {_fmt('max_6m')} / {_fmt('min_6m')}"
        )
        self._update_extreme_text_pos()
        self._refresh_limits()

    def _update_pending_text(self) -> None:
        """开盘中游标：当前 H1 蜡烛未收线 → 文本在蜡烛顶上方。"""
        info = self._candles.pending_info()
        if info is None:
            self._pending_text.hide()
            return
        t, top, hhmm = info
        self._pending_text.setText(f"开盘中（收盘预计 {hhmm}）")
        self._pending_text.setPos(t + _H1 / 2, top * 1.0005)
        self._pending_text.show()

    # ==================================================================
    # 内部绘制逻辑
    # ==================================================================
    def _autorange_once(self) -> None:
        """ViewBox 显式移动到窗口内（原点默认视图让图表看起来空白）。

        用 setXRange/setYRange 而非 enableAutoRange——后者会被
        _refresh_limits() 写入的 setLimits 覆盖，表现为数据全部"不显示"。

        默认窗口只展示最近 _VIEW_BARS 根蜡烛（更早的靠拖动/缩放查看），
        否则 9599 根全部挤在一屏，鼠标一动整幅重绘必然卡。"""
        c_t = self._candles._t
        if not self._t and not c_t:
            return
        xs = self._t + c_t
        ys = self._v + self._candles._h + self._candles._l
        if not xs:
            return
        x_min_all, x_max_all = min(xs), max(xs)
        pad_x = max((x_max_all - x_min_all) * 0.05, 60.0)
        x_right = x_max_all + pad_x
        # 默认窗口 = 最近 _VIEW_BARS 根蜡烛（M1=60s→20 小时；H1=50 天）
        bar_sec = float(getattr(self, "_bar_sec", _H1) or _H1)
        x_left = max(x_min_all - pad_x, x_right - _VIEW_BARS * bar_sec)
        # y 包络只取窗口内数据（窗口外的极值会把曲线压成一条线）
        x_cut = x_left + pad_x * 0.2
        ys_w = [v_ for t_, v_ in zip(self._t, self._v) if t_ >= x_cut]
        for t_, h_, l_ in zip(c_t, self._candles._h, self._candles._l):
            if t_ >= x_cut:
                ys_w.append(h_)
                ys_w.append(l_)
        if ys_w:
            y_lo, y_hi = min(ys_w), max(ys_w)
            pad_y = max((y_hi - y_lo) * 0.10, abs(y_hi or 1.0) * 0.01, 1e-9)
        else:
            y_lo, y_hi = 0, 1
            pad_y = 1
        try:
            vb = self._plot.getPlotItem().getViewBox()
            vb.setXRange(x_left, x_right, padding=0)
            vb.setYRange(y_lo - pad_y, y_hi + pad_y, padding=0)
        except Exception:
            pass
        self._update_extreme_text_pos()

    def _redraw(self) -> None:
        if not self._t:
            # 空数据不能用 setData([], [])（pg 无法识别空列表），改用 clear()
            self._curve_spread.clear()
            self._curve_mean.clear()
        else:
            self._curve_spread.setData(self._t, self._v)
            self._curve_mean.setData(self._t, np.full(len(self._v), self._mean))
        self._update_extreme_text_pos()
        self._refresh_limits()

    def _update_extreme_text_pos(self) -> None:
        if self._t:
            self._extreme_text.setPos(self._t[0], self._v_max())

    def _v_max(self) -> float:
        return float(np.max(self._v)) if self._v else 0.0

    def _refresh_limits(self) -> None:
        """setLimits 防拖飞：把视图边界锁在数据+阈值+蜡烛的包络内（含少量边距）。"""
        c_t, c_h, c_l = (self._candles._t, self._candles._h, self._candles._l)
        xs = self._t + c_t
        if not xs:
            self._plot.setLimits(xMin=None, xMax=None, yMin=None, yMax=None)
            return
        x_lo, x_hi = min(xs), max(xs)
        pad_x = max((x_hi - x_lo) * 0.05, 1.0)
        ys = list(self._v)
        ys.extend(c_h)
        ys.extend(c_l)
        ys.extend((
            self._mean + self._k * self._std,
            self._mean - self._k * self._std,
            self._mean + (self._k + max(self._levels - 1, 0) * self._grid) * self._std,
            self._mean - (self._k + max(self._levels - 1, 0) * self._grid) * self._std,
        ))
        y_lo, y_hi = min(ys), max(ys)
        pad_y = max((y_hi - y_lo) * 0.1, abs(y_hi or y_lo) * 1e-6, 1e-9)
        self._plot.setLimits(
            xMin=x_lo - pad_x, xMax=x_hi + pad_x,
            yMin=y_lo - pad_y, yMax=y_hi + pad_y,
        )

    # ==================================================================
    # 交互
    # ==================================================================
    def _flush_cross_lazy(self) -> None:
        """时间门槛关闭期间跨过的桶：停住 45ms 后补一次重排。"""
        self._cross_flush_scheduled = False
        pos = getattr(self, "_last_scene_pos", None)
        if pos is not None and self._cross_pending:
            self._on_mouse_moved(pos)

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

    def _on_mouse_moved(self, scene_pos) -> None:
        """十字光标：线跟随鼠标；读数固定在视口右上角、
        仅在跨入新 H1 桶时才重排文本（QGraphicsTextItem 重排缓存，
        逐像素 setText 是卡顿/闪烁源）。

        支持纯蜡烛轴（未载入价差线时用蜡烛收盘序列）；读取的是
        光标下方最近一根蜡烛/数据点。"""
        if not self._plot.sceneBoundingRect().contains(scene_pos):
            self._cross_text.hide()
            self._cross_v.hide()
            self._cross_h.hide()
            return
        ts_axis = self._t if self._t else self._candles._t
        vs_axis = self._v if self._t else self._candles._c
        if not ts_axis:
            self._cross_text.hide()
            self._cross_v.hide()
            self._cross_h.hide()
            return
        mouse = self._vb.mapSceneToView(scene_pos)
        x, y = float(mouse.x()), float(mouse.y())
        self._last_scene_pos = scene_pos
        self._cross_v.setPos(x)
        self._cross_h.setPos(y)
        self._cross_v.show()
        self._cross_h.show()

        idx = bisect.bisect_left(ts_axis, x)
        idx = min(max(idx, 0), len(ts_axis) - 1)
        bucket = int(ts_axis[idx] // _H1) * _H1
        if bucket != self._cross_bucket:
            self._cross_bucket = bucket   # 此桶成为待显示项；重排受时间门槛约束
            self._cross_pending = True
            if not self._cross_flush_scheduled:   # 停住后 45ms 内补一次 flush
                self._cross_flush_scheduled = True
                QtCore.QTimer.singleShot(45, self._flush_cross_lazy)
        if self._cross_pending and time.monotonic() - self._cross_last >= 0.040:
            self._cross_pending = False
            self._cross_last = time.monotonic()
            ts = dt.datetime.fromtimestamp(bucket).strftime("%Y-%m-%d %H:%M")
            v_at = vs_axis[idx]
            self._cross_text.setText(f"{ts}\n价差 {v_at:+.4f}")
        # 文本钉在视口右上角（不随光标挪动，不乱跳）
        xr, yr = self._vb.viewRange()
        self._cross_text.setPos(xr[1] - 8.0, yr[0] + 10.0)
        self._cross_text.show()
