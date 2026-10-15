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


_DAY = 86_400.0


_H1 = 3600.0   # （历史遗留默认秒宽；M1=60，实际桶宽由 set_series 从数据自动推导）


def _fixed_ticks(values: list[float], spacing: float) -> list[str]:
    """定点小数刻度文本（根除科学计数法）。

    小数位数由整定间隔 spacing 推导：spacing≈2e-5 → 5 位，
    绝不让 Qt 走 str/吧 scientific route。插值律：dec = ceil(-log10(spacing))。
    过大 spacing 用整数（补 0 位）；与 0 完全重叠时按数值特异性容衲。"""
    if not values:
        return []
    sp = abs(float(spacing)) if spacing else 1.0
    if not (sp > 0) or not (sp == sp):   # nan/inf 防护
        sp = 1.0
    import math as _m
    if sp >= 1.0:
        dec = 0
    else:
        try:
            dec = min(10, max(0, int(_m.ceil(-_m.log10(sp)))))
        except (ValueError, OverflowError):
            dec = 5
    out = [f"{float(v):.{dec}f}" for v in values]
    # 去尾零（保可读性）：'0.00020'→'0.0002'、'-0.000000'→'0'
    return [s.rstrip("0").rstrip(".") if "." in s else s for s in out]


class _FixedFmtAxis(pg.AxisItem):
    """定点小数数值轴：根除科学计数法（pyqtgraph 默认 tick 对
    1e-4 量级值会走 e-枚计法）。spacing 精度自动适应缩放层级。"""

    def tickStrings(self, values, scale, spacing):  # noqa: N802
        if scale and scale != 1.0:
            values = [v / scale for v in values]
        return _fixed_ticks(values, spacing)
_VIEW_BARS = 1200   # 载入后默认显示的最近数据点数（更早历史可拖动/缩放查看）


class SpreadChart(QtWidgets.QWidget):
    """价差序列图（pyqtgraph PlotWidget 封装，可嵌入任意策略台/页面）。"""

    sig_k_changed = QtCore.Signal(float)  # 用户拖动上阈值线 -> 发出新的 k 值

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)

        # 图形状态
        self._t: list[float] = []
        self._v: list[float] = []
        # 恒定预分配 numpy 缓冲（容量按需倍增）：_redraw/setData 只发缓冲切片
        # [self._buf_t[:n], self._buf_v[:n]]，避免每次从 Python list 重建
        # np 数组；_t/_v 仍保留供 bisect 十字光标与 pending 逻辑使用。
        self._buf_t: np.ndarray = np.empty(0, dtype=float)
        self._buf_v: np.ndarray = np.empty(0, dtype=float)
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
            '<span style="color:#4d8df0">— 价差（M1 合成）</span>&nbsp;&nbsp;&nbsp;'
            '<span style="color:#c8cfda">— 均值</span>&nbsp;&nbsp;&nbsp;'
            '<span style="color:#ea3943">─ 上阈值（拖动调 k）</span>&nbsp;&nbsp;&nbsp;'
            '<span style="color:#16c784">─ 下阈值</span>&nbsp;&nbsp;&nbsp;'
            '<span style="color:#f0b90b">·· 极值</span>'
        )
        self._legend_label.setTextFormat(QtCore.Qt.RichText)
        self._legend_label.setStyleSheet(
            f"color:#c8cfda; background:{_BG}; padding:1px 4px; font-size:11px;"
        )
        lay.addWidget(self._legend_label)

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
        """历史全量替换：t=时间戳(s)，v=价差折线点（M1 合成收盘）。

        数据一次写入恒定预分配 numpy 缓冲（容量不足才倍增），
        _redraw 只发缓冲切片，不再每次从 list 重建 np 数组。
        桶宽（_bar_sec）从相邻时间差自动推出（M1=60、H1=3600），
        用于默认视窗宽度计算与批量聚合提示。"""
        t_list = [float(x) for x in (t or [])]
        v_list = [float(x) for x in (v or [])]
        n = min(len(t_list), len(v_list))
        # 折线桶宽不再依赖蜡烛：从相邻时间差推导（等距取样即周期）
        if n >= 2 and t_list[1] > t_list[0]:
            self._bar_sec = float(t_list[1] - t_list[0])
        self._buf_ensure(n)
        self._buf_t[:n] = t_list[:n]
        self._buf_v[:n] = v_list[:n]
        self._t = t_list
        self._v = list(v_list[:n])
        self._redraw()
        self._autorange_once()

    def _buf_ensure(self, n: int) -> None:
        """恒定缓冲容量保证 >= n：容量按需倍增，够用时复用不重分配。"""
        cap = len(self._buf_t)
        if n <= cap:
            return
        new_cap = cap or 128
        while new_cap < n:
            new_cap *= 2
        self._buf_t = np.empty(new_cap, dtype=float)
        self._buf_v = np.empty(new_cap, dtype=float)

    def append_point(self, t: float, v: float) -> None:
        """live 增点（追加）；setData 限频 200ms + 视图自动跟随右缘（可拖动打断）。"""
        t_f = float(t)
        v_f = float(v)
        self._t.append(t_f)
        self._v.append(v_f)
        n = len(self._t)
        self._buf_ensure(n)
        self._buf_t[n - 1] = t_f
        self._buf_v[n - 1] = v_f
        self._follow_right_edge(t_f)
        self._curve_dirty = True
        if not self._curve_flush.isActive():
            self._curve_flush.start(200)

    def _follow_right_edge(self, t_latest: float) -> None:
        """视图跟随：仅当用户当前正看着最右端（右缘差 latest ≤ 2 个桶宽）
        时才把窗口平移到最新点；手动拖/缩放到老时段则不跟随（不打断浏览）。

        setXRange 前先刷新 limits（xMax 追最新数据+pad），避免请求的右缘
        被 pyqtgraph 的 mn<lmn 平移回弹（_window 平移的坑已定位：limits
        下限锁在旧 x_lo - pad_x 时任何右移都会被弹回起点，左界用 None 已解）。"""
        try:
            xr, _yr = self._vb.viewRange()
            right = xr[1]
            bar = max(self._bar_sec, 1.0)
            if t_latest - right <= bar * 2.0:   # 用户在看最新段 → 平移
                self._refresh_limits()          # 同步 xMax 至最新数据包络
                span = right - xr[0]
                lead = bar * 5.0                # 右缘留 5 个桶宽空白
                self._vb.setXRange(t_latest + lead - span, t_latest + lead,
                                   padding=0)
        except Exception:
            pass

    def _flush_curve(self) -> None:
        if self._curve_dirty:
            self._curve_dirty = False
            self._redraw()

    # ==================================================================
    # K 线接口（已退役：折线方案；保留为 no-op 以防历史调用点断链）
    # ==================================================================
    def set_candles(self, t, o, h, l, c) -> None:  # noqa: N803
        """历史蜡烛全量替换：K 线已退役（折线方案），保留接口为 no-op。"""
        return

    def append_candle_tick(self, ts: float, price: float) -> None:
        """live tick 蜡烛聚合：K 线已退役，保留接口为 no-op（实时走 append_point）。"""
        return

    def set_candles_visible(self, visible: bool) -> None:
        """K 线可见性开关：K 线已退役，保留接口为 no-op。"""
        return

    def clear_series(self) -> None:
        """清空序列并隐藏依赖数据的标记。"""
        self._t.clear()
        self._v.clear()
        # 缓冲长度同步：切片窗口归零（已分配容量保留，避免反复申请/释放）
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

    # ==================================================================
    # 内部绘制逻辑
    # ==================================================================
    def _autorange_once(self) -> None:
        """ViewBox 显式移动到窗口内（原点默认视图让图表看起来空白）。

        用 setXRange/setYRange 而非 enableAutoRange——后者会被
        _refresh_limits() 写入的 setLimits 覆盖，表现为数据全部"不显示"。

        默认窗口只展示最近 _VIEW_BARS 根蜡烛（更早的靠拖动/缩放查看），
        否则 9599 根全部挤在一屏，鼠标一动整幅重绘必然卡。"""
        if not self._t:
            return
        xs = self._t
        ys = self._v
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
        n = len(self._t)
        if not n:
            # 空数据不能用 setData([], [])（pg 无法识别空列表），改用 clear()
            self._curve_spread.clear()
            self._curve_mean.clear()
        else:
            # 只发恒定缓冲切片：无 list→np 重建，flush 全量重设避免 O(n) 重建开销
            buf_t = self._buf_t[:n]
            buf_v = self._buf_v[:n]
            self._curve_spread.setData(buf_t, buf_v)
            self._curve_mean.setData(buf_t, np.full(n, self._mean))
        self._update_extreme_text_pos()
        self._refresh_limits()

    def _update_extreme_text_pos(self) -> None:
        if self._t:
            self._extreme_text.setPos(self._t[0], self._v_max())

    def _v_max(self) -> float:
        n = len(self._v)
        return float(self._buf_v[:n].max()) if n else 0.0

    def _refresh_limits(self) -> None:
        """setLimits 防拖飞：右/上/下边界锁在数据+阈值的包络内（含少量边距）。

        **xMin 故意不设限**：pyqtgraph 的 setRange 在 mn<lmn 时会把整个视窗
        平移回下限（"delta=lmn-mn; mn=lmn; mx+=delta"），锁住 xMin 就
        锁死了实时跟随/右移（窗口在 setXRange 后纹丝不动）。
        用户向左回溯仍可拖（xMax 限制仅在数据右界之外）。"""
        xs = self._t
        if not xs:
            self._plot.setLimits(xMin=None, xMax=None, yMin=None, yMax=None)
            return
        x_lo, x_hi = min(xs), max(xs)
        pad_x = max((x_hi - x_lo) * 0.05, 1.0)
        ys = list(self._v)
        ys.extend((
            self._mean + self._k * self._std,
            self._mean - self._k * self._std,
            self._mean + (self._k + max(self._levels - 1, 0) * self._grid) * self._std,
            self._mean - (self._k + max(self._levels - 1, 0) * self._grid) * self._std,
        ))
        y_lo, y_hi = min(ys), max(ys)
        pad_y = max((y_hi - y_lo) * 0.1, abs(y_hi or y_lo) * 1e-6, 1e-9)
        self._plot.setLimits(
            xMin=None, xMax=x_hi + pad_x,
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
        ts_axis = self._t
        vs_axis = self._v
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
            self._cross_text.setText(f"{ts}\n价差 {v_at:+.5f}")
        # 文本钉在视口右上角（不随光标挪动，不乱跳）
        xr, yr = self._vb.viewRange()
        self._cross_text.setPos(xr[1] - 8.0, yr[0] + 10.0)
        self._cross_text.show()
