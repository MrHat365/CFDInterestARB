"""套隔夜利息策略引擎（纸面模式优先）。

组成：
- SpreadSeries: 价差序列缓存 + 滚动窗口统计（mean/std/上界/下界）
- SignalEngine: Z-score 网格进场逻辑（受最大持仓约束），方向仓位合并
- StrategyRunner: 消费 EVENT_TICK 合成实时价差点位，消费历史数据集初始化统计

合并仓位说明：MT5 一个单据=一个仓位；此处按 symbol 维度做净敞口合并
（sum(多腿 - 空腿)），用于最大持仓计数，与 worker 的真实账户仓位互不混淆。

网格进场语义：
  上行 Z 超过 mean + k + i*grid_step 个标准差（i=0..levels）时，
  卖出组合方向记一笔进场（价差仅回落）; 下行对称。
  进场计数按「方向」累计，同一方向最多 max_positions 笔；非同一方向独立计数。

统计口径分两段（先验 / 实时窗口）：
  - 先验（prior）：由 StrategyRunner.set_history 载入的 H1 历史价差算出的
    mean/std/n，用于实时点不足（<10 点）时的替代统计，替代时 stats 带 "prior": True；
  - 实时窗口：on_tick 逐点追加的 live 点，满 10 点后改用滚动窗口统计；
  - 长周期极值按时间戳切分段（365d/182d 截断），与统计口径相互独立。
"""

from __future__ import annotations

import collections
import math
import datetime as dt

import numpy as np

_DAY = 86_400.0


class SpreadSeries:
    """序列缓存 + 滚动窗统计 + 先验统计 + 长周期极值。"""

    def __init__(self, window: int = 500, max_points: int = 200_000) -> None:
        self.window = window
        self._t = collections.deque(maxlen=max_points)
        self._v = collections.deque(maxlen=max_points)
        self._prior: dict | None = None   # {mean, std, n}，来自 H1 历史
        self._live_n = 0                  # live 成点计数（历史点不计入）
        self._ext_cache: tuple[float, dict] | None = None  # extremes TTL 缓存 (缓存时刻, 结果)

    def append(self, ts: float, value: float) -> None:
        """追加一个 live 成点（历史点经 init_prior 载入，不走此接口）。"""
        self._t.append(ts)
        self._v.append(value)
        self._live_n += 1

    def init_prior(self, h1: dict) -> dict:
        """用 H1 历史价差计算先验统计（mean/std/n）并写入序列缓存。

        先验口径：全量 H1 历史（t=时间戳, v=价差值）无截断；
        历史点同时进入 _t/_v 仅用于图表展示，不计数为 live 点。
        :param h1: {"t": [...], "v": [...]}（t 与 v 长度相等，错位点静默跳过）
        :returns: {"mean", "std", "n"}；不足 2 点时返回 {} 且不设置先验。
        """
        vs: list[float] = []
        for t, v in zip(h1.get("t", []), h1.get("v", [])):
            try:
                ts_f, v_f = float(t), float(v)
            except (TypeError, ValueError):
                continue  # 错位点整体丢弃，保持 t/v 对齐
            self._t.append(ts_f)
            self._v.append(v_f)
            vs.append(v_f)
        vt = np.asarray(vs, dtype=float)
        if vt.size < 2:
            return {}
        mean = float(vt.mean())
        var = float(vt.var(ddof=1))
        self._prior = {"mean": mean, "std": max(var ** 0.5, 1e-12), "n": int(vt.size)}
        self._live_n = 0
        return dict(self._prior)

    def prior(self) -> dict:
        """返回先验统计副本（未设置时返回 {}）。"""
        return dict(self._prior) if self._prior else {}

    def live_points(self) -> int:
        """live 成点数（不包含历史点；重置后从 0 开始）。"""
        return min(self._live_n, len(self._v))

    def point_count(self) -> int:
        """序列缓存总点数（含先验历史点 + live 点，仅用于展示/诊断）。"""
        return len(self._v)

    def stats(self) -> dict:
        """滚动窗口统计：缓存末尾 window 个点（含历史 + live 点，兼容旧 API）。

        优先级由 StrategyRunner 决定：live <10 且有先验时缓存统计被先验替代；
        本方法仅在实时段成型（无先验或 live >=10）后直接作为口径使用。
        """
        n = min(self.window, len(self._v))
        if n < 10:
            return {}
        tail = np.asarray(list(self._v)[-n:], dtype=float)
        mean = float(tail.mean())
        var = float(tail.var(ddof=1))
        std = max(var ** 0.5, 1e-12)
        z = (tail[-1] - mean) / std
        return {
            "mean": mean, "std": std, "z": z,   # 上下界由 runner 的 k/grid_step 动态生成
            "last": float(tail[-1]),
            "n_points": len(self._v),
        }

    def extremes(self, now: float | None = None) -> dict:   # noqa: A003（与旧公开 API 同名）
        """长周期极值：按时间戳切分段（不再用序列前半段近似 6m）。

        以 now（缺省当前时间）往前 365d / 182d 截断，取落在窗口内的点求 max/min；
        某侧窗口没有任何点时对应键返回 None（图表方按缺数据显示 "-"）。
        :param now: 参考时间戳（秒）；None = 当前系统时间。
        :returns: {max_1y, min_1y, max_6m, min_6m, window_days}；
                  window_days=1y 窗内最老点到 now 的实际覆盖天数，无点为 0.0。
        """
        if now is None:
            now = dt.datetime.now().timestamp()
        cached = self._ext_cache
        if cached is not None and abs(now - cached[0]) < 60.0:
            return dict(cached[1])
        keys = ("max_1y", "min_1y", "max_6m", "min_6m")
        if not self._v:
            return {}
        cutoff_1y = now - 365 * _DAY
        cutoff_6m = now - 182 * _DAY
        t_arr = np.asarray(self._t, dtype=float)
        v_arr = np.asarray(self._v, dtype=float)
        mask_1y = t_arr >= cutoff_1y
        if not mask_1y.any():
            result = dict.fromkeys(keys, None) | {"window_days": 0.0}
            self._ext_cache = (now, result)
            return dict(result)
        values_1y = v_arr[mask_1y]
        values_6m = v_arr[t_arr >= cutoff_6m]  # mask_6m ⊆ mask_1y（cutoff_6m > cutoff_1y）
        oldest_t = float(t_arr[mask_1y].min())
        result = {
            "max_1y": float(values_1y.max()), "min_1y": float(values_1y.min()),
            "max_6m": float(values_6m.max()) if values_6m.size else None,
            "min_6m": float(values_6m.min()) if values_6m.size else None,
            "window_days": round((now - oldest_t) / _DAY, 1),
        }
        self._ext_cache = (now, result)
        return dict(result)

    def arrays(self) -> tuple[list[float], list[float]]:
        return list(self._t), list(self._v)


class SignalEngine:
    """网格进场信号（纸面/实盘共用，模式由回调侧决定）。"""

    def __init__(
        self,
        strategy_id: str,
        k: float = 2.0,
        grid_step: float = 0.5,
        levels: int = 3,
        max_positions: int = 10,
    ) -> None:
        self.strategy_id = strategy_id
        self.k = k
        self.grid_step = grid_step
        self.levels = levels
        self.max_positions = max_positions
        # 已进场层级 {方向: [(ts, level, remark)]}
        self._entries: dict[str, list[tuple]] = {"buy": [], "sell": []}

    # ------------------------------------------------------------------
    def on_spread(self, stats: dict, ts: float) -> dict | None:
        """产生进场/离场信号。返回 {action, side, level, z} 或 None。"""
        if not stats:
            return None
        mean, std, z = stats["mean"], stats["std"], stats["z"]

        # 离场：价差回到均值 ±0.3σ 内，清空同方向持仓
        for side in ("buy", "sell"):
            if self._entries[side] and abs(z) <= 0.3:
                return {"action": "close", "side": side, "count": len(self._entries[side]),
                        "level": stats["last"], "z": round(z, 2)}

        # 进场：Z 依次超过 mean ± (k + i*grid_step)·std，多腿（方向）独立计数
        for direction in ("sell", "buy"):
            for i in range(self.levels):
                level_z = self.k + i * self.grid_step
                upper_i = mean + level_z * std
                lower_i = mean - level_z * std
                if direction == "sell" and z >= level_z:
                    return self._try_open(direction, stats, ts, upper_i, f"z={z:.2f}>+{level_z}")
                if direction == "buy" and z <= -level_z:
                    return self._try_open(direction, stats, ts, lower_i, f"z={z:.2f}<-{level_z}")
        return None

    def _try_open(self, direction: str, stats: dict, ts: float, level: float, remark: str):
        if len(self._entries[direction]) >= self.max_positions:
            return {"action": "blocked", "side": direction,
                    "level": level, "z": round(stats["z"], 2)}
        self._entries[direction].append((ts, level, remark))
        return {"action": "open", "side": direction,
                "level": level, "count": len(self._entries[direction]), "z": round(stats["z"], 2)}

    def reset_direction(self, direction: str) -> None:
        self._entries[direction].clear()

    def open_count(self, direction: str | None = None) -> int:
        if direction:
            return len(self._entries[direction])
        return len(self._entries["buy"]) + len(self._entries["sell"])


class StrategyRunner:
    """组装：legs -> 实时价差 -> 信号回调。"""

    def __init__(self, strategy_id: str, legs: list[dict], params: dict) -> None:
        """:param legs: [{symbol, side, weight}]"""
        self.strategy_id = strategy_id
        self.legs = legs
        self.params = {
            "window": int(params.get("window", 500)),
            "k": float(params.get("k", 2.0)),
            "grid_step": float(params.get("grid_step", 0.5)),
            "levels": int(params.get("levels", 3)),
            "max_positions": int(params.get("max_positions", 10)),
        }
        self.series = SpreadSeries(window=self.params["window"])
        self.signal = SignalEngine(
            strategy_id, k=self.params["k"], grid_step=self.params["grid_step"],
            levels=self.params["levels"], max_positions=self.params["max_positions"],
        )
        self.long_stats: dict = {}   # 1y/6m 极值（来自 H1 历史集）
        self.prior: dict = {}        # 先验统计 {mean, std, n}（来自 H1 历史集）
        self.hedge_note = "等手数中性（可调）"

    # ------------------------------------------------------------------
    def set_history(self, h1: dict) -> None:
        """用 H1 历史价差做先验（t=时间戳, v=价差值）；仅唯一口径，避免混周期。

        两段口径：先验段（全量 H1）供 live 点 <10 时替代统计；
        实时段（on_tick 逐点）满 10 点后改用滚动窗口统计。
        """
        self.prior = self.series.init_prior(h1)
        self.long_stats = self.series.extremes()

    # ------------------------------------------------------------------
    def compute_spread(self, tick_map: dict[str, dict]) -> tuple[float, dict] | None:
        """实时价差（乘法合成，与 worker 历史同一公式）：

            合成买(Ask) = 其余腿按方向取限价侧对数加权乘法合成
            价差 = 实际得腿 Bid − 合成买价   （用户公式：
                  合成买 USDJPY(Ask) = USDCHF Ask × CHFJPY Ask）

        :param tick_map: {symbol: {"bid":..,"ask":..}} -> (spread, 各腿 hedge 快照)"""
        if not self.legs:
            return None
        # 目标腿：卖出方向（无则 |权重| 最大），跟 worker._send_spread_history 同口径
        sell = [x for x in self.legs if x.get("side", "buy") == "sell"]
        pool = sell or list(self.legs)
        target = max(pool, key=lambda m: abs(float(m.get("weight", 1.0))))
        tl = tick_map.get(target["symbol"])
        if tl is None:
            return None
        t_bid = float(tl["bid"])
        synth = 0.0
        hedge = [{
            "symbol": target["symbol"], "side": target.get("side", "sell"),
            "weight": float(target.get("weight", 1.0)), "price": t_bid,
        }]
        for leg in self.legs:
            if leg["symbol"] == target["symbol"]:
                continue
            tick = tick_map.get(leg["symbol"])
            if tick is None:
                return None
            weight = float(leg.get("weight", 1.0))
            sign = 1.0 if leg["side"] == "buy" else -1.0
            side = float(tick["ask"]) if sign > 0 else float(tick["bid"])
            if not (side > 0):
                return None
            synth += sign * math.log(side)   # 幂 ±1（用户公式，不含手数权重）
            hedge.append({
                "symbol": leg["symbol"], "side": leg["side"],
                "weight": weight, "price": side,
            })
        try:
            synthetic = math.exp(synth)
        except OverflowError:
            return None
        spread = t_bid - synthetic
        return spread, hedge

    def on_tick(self, tick_map: dict[str, dict]) -> dict | None:
        """消费一帧行情：追加 live 点并产出 {ts, spread, stats, hedge, signal, extremes}。

        统计两段口径：live 点 <10 且存在先验时，用先验 mean/std + live last
        计算 z，stats 带 "prior": True；live 点 >=10 改用滚动窗口统计（prior 标记消失）。
        """
        now = dt.datetime.now().timestamp()
        result = self.compute_spread(tick_map)
        if result is None:
            return None
        spread, hedge = result
        self.series.append(now, spread)
        prior = self.series.prior()
        if prior and self.series.live_points() < 10:
            # 先验段：用 H1 先验 mean/std 替代未成型的实时窗口
            stats = {
                "mean": prior["mean"], "std": prior["std"],
                "z": (spread - prior["mean"]) / max(prior["std"], 1e-12),
                "last": spread, "n_points": self.series.point_count(),
                "prior": True,
            }
        else:
            stats = self.series.stats()
        if not stats:
            return None
        sig = self.signal.on_spread(stats, now)
        return {
            "ts": now, "spread": spread, "stats": stats, "hedge": hedge,
            "signal": sig, "extremes": self.series.extremes(),
        }
