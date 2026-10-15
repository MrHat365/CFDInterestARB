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

统计使用最近 window 个自然分钟内已收盘的 M1 值，空缺分钟不补造价格。
H1 先验在 M1 样本不足时平滑混合，标准差下限为先验的 10%；
轮询帧仅更新当前分钟收盘候选，不改变窗口长度。长期极值独立计算。
"""

from __future__ import annotations

import collections
import math
import itertools
import datetime as dt

import numpy as np

from .series_data import normalize_series
from .minute_statistics import MinuteStatistics

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
        if not math.isfinite(ts) or not math.isfinite(value):
            return
        if self._t and ts < self._t[-1]:
            return
        if self._t and ts == self._t[-1]:
            self._v[-1] = value
            return
        self._t.append(ts)
        self._v.append(value)
        self._live_n += 1

    def init_prior(self, h1: dict) -> dict:
        """H1 提供先验/极值，M1 提供滚动统计；保留请求期间到达的实时尾部。"""
        times, values = normalize_series(h1.get("t", []), h1.get("v", []))
        self._t.clear()
        self._v.clear()
        self._prior = None
        self._live_n = 0
        self._ext_cache = None
        self._t.extend(times)
        self._v.extend(values)
        vs = list(values)
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
        """滚动窗口统计：有live时仅取live尾部；无live时可查看历史统计。

        优先级由 StrategyRunner 决定：live <10 且有先验时缓存统计被先验替代；
        本方法仅在实时段成型（无先验或 live >=10）后直接作为口径使用。
        """
        available = min(self._live_n, len(self._v)) if self._live_n else len(self._v)
        n = min(self.window, available)
        if n < 10:
            return {}
        tail = np.fromiter(itertools.islice(reversed(self._v), n), dtype=float, count=n)[::-1]
        mean = float(tail.mean())
        var = float(tail.var(ddof=1))
        std = max(var ** 0.5, 1e-12)
        z = (tail[-1] - mean) / std
        return {
            "mean": mean, "std": std, "z": z,   # 上下界由 runner 的 k/grid_step 动态生成
            "last": float(tail[-1]),
            "n_points": len(self._v),
        }

    def extremes(self, now: float | None = None) -> dict:   # 与旧公开 API 同名
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
        if not all(math.isfinite(x) for x in (mean, std, z)) or std <= 0:
            return None

        # 离场：价差回到均值 ±0.3σ 内，清空同方向持仓
        for side in ("buy", "sell"):
            if self._entries[side] and abs(z) <= 0.3:
                return {"action": "close", "side": side, "count": len(self._entries[side]),
                        "level": stats["last"], "z": round(z, 2)}

        # The next unoccupied grid level is the only eligible next entry.
        # Holding the same z must not consume the entire position budget.
        for direction in ("sell", "buy"):
            count = len(self._entries[direction])
            if count >= self.max_positions:
                if (direction == "sell" and z >= self.k) or (direction == "buy" and z <= -self.k):
                    return {"action": "blocked", "side": direction, "level": stats["last"], "z": round(z, 2)}
                continue
            if count >= self.levels:
                continue
            level_z = self.k + count * self.grid_step
            if (direction == "sell" and z >= level_z) or (direction == "buy" and z <= -level_z):
                level = mean + (1 if direction == "sell" else -1) * level_z * std
                return self._try_open(direction, stats, ts, level, f"grid={count + 1}")
        return None

    def _try_open(self, direction: str, stats: dict, ts: float, level: float, remark: str):
        if len(self._entries[direction]) >= self.max_positions:
            return {"action": "blocked", "side": direction,
                    "level": level, "z": round(stats["z"], 2)}
        self._entries[direction].append((ts, level, remark))
        return {"action": "open", "side": direction,
                "level": level, "count": len(self._entries[direction]), "z": round(stats["z"], 2)}

    def restore_counts(self, counts: dict[str, int]) -> None:
        """Live mode counts are confirmed ledger layers, never emitted intents."""
        for side in ("buy", "sell"):
            count = int(counts.get(side, 0))
            if count < 0:
                raise ValueError("negative position count")
            self._entries[side] = [(0, 0, "confirmed") for _ in range(count)]

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
        self.minute_stats = MinuteStatistics(self.params['window'])
        self._last_timestamp = float('-inf')
        self._last_spread: float | None = None
        self.signal = SignalEngine(
            strategy_id, k=self.params["k"], grid_step=self.params["grid_step"],
            levels=self.params["levels"], max_positions=self.params["max_positions"],
        )
        self.long_stats: dict = {}   # 1y/6m 极值（来自 H1 历史集）
        self.prior: dict = {}        # 先验统计 {mean, std, n}（来自 H1 历史集）
        self.hedge_note = "等手数中性（可调）"

    # ------------------------------------------------------------------
    def set_history(self, h1: dict, m1: dict | None = None) -> None:
        """用 H1 历史价差做先验（t=时间戳, v=价差值）；仅唯一口径，避免混周期。

        两段口径：先验段（全量 H1）供 live 点 <10 时替代统计；
        实时段（on_tick 逐点）满 10 点后改用滚动窗口统计。
        """
        times, values = self.series.arrays()
        count = self.series.live_points()
        live = list(zip(times[-count:], values[-count:])) if count else []
        self.prior = self.series.init_prior(h1)
        latest_history = self.series._t[-1] if self.series._t else float("-inf")
        for timestamp, value in live:
            if timestamp > latest_history:
                self.series.append(timestamp, value)
        self.long_stats = self.series.extremes()
        self.minute_stats.seed(m1 or {})

    def current_stats(self) -> dict:
        self.minute_stats.window = max(10, self.series.window)
        prior = self.series.prior()
        last = self._last_spread if self._last_spread is not None else prior.get('mean', 0.)
        return self.minute_stats.stats(last, prior)

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
        t_bid = float(tl["ask"] if target.get("side", "buy") == "buy" else tl["bid"])
        if not math.isfinite(t_bid) or t_bid <= 0:
            return None
        target_sign = 1.0 if target.get("side", "buy") == "buy" else -1.0
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
            if not math.isfinite(side) or side <= 0:
                return None
            synth += (-sign / target_sign) * math.log(side)
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

    def on_tick(self, tick_map: dict[str, dict], *, emit_signal: bool = True) -> dict | None:
        """消费一帧行情：追加 live 点并产出 {ts, spread, stats, hedge, signal, extremes}。

        仅已收盘 M1 参与统计；当前分钟反复更新不会增加有效样本数。
        """
        try:
            stamps = [(float(tick_map[leg['symbol']].get('time_msc') or
                       float(tick_map[leg['symbol']]['time']) * 1000) / 1000) for leg in self.legs]
        except (KeyError, TypeError, ValueError):
            return None
        if not stamps or not all(math.isfinite(t) for t in stamps):
            return None
        now = max(stamps)
        if now < self._last_timestamp:
            return None
        result = self.compute_spread(tick_map)
        if result is None:
            return None
        spread, hedge = result
        self._last_timestamp, self._last_spread = now, spread
        self.series.append(now, spread)
        self.minute_stats.append(now, spread)
        stats = self.current_stats()
        if not stats:
            return None
        sig = self.signal.on_spread(stats, now) if emit_signal else None
        return {
            "ts": now, "spread": spread, "stats": stats, "hedge": hedge,
            "signal": sig, "extremes": self.series.extremes(),
        }
