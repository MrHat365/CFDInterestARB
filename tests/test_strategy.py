"""strategy.py 纯逻辑层测试（SpreadSeries / SignalEngine / StrategyRunner）。"""

from __future__ import annotations

import math

from mt5_trader.strategy import SignalEngine, SpreadSeries, StrategyRunner

_DAY = 86_400.0


# ---------------------------------------------------------------------------
# SpreadSeries.stats / extremes
# ---------------------------------------------------------------------------
def _make_series(n: int = 200) -> tuple[SpreadSeries, float, list[float]]:
    """n=200 个点：ts 从 now-400d 到 now-2d（等距 2 天），value = i。

    落入 1y 窗口 <=> (400-2i) <= 365 <=> i >= 17.5（即 i >= 18）；
    落入 6m 窗口 <=> (400-2i) <= 182 <=> i >= 109。
    返回 (series, now, values)。
    """
    now = 1_800_000_000.0
    s = SpreadSeries()
    values = []
    for i in range(n):
        ts = now - (400 - 2 * i) * _DAY
        values.append(float(i))
        s.append(ts, float(i))
    return s, now, values


def test_stats_matches_manual_linear_computation():
    s, _now, values = _make_series(200)
    st = s.stats()
    assert st, "200 点应产出统计"
    n = len(values)
    mean = sum(values) / n
    var = sum((x - mean) ** 2 for x in values) / (n - 1)
    std = max(var ** 0.5, 1e-12)
    assert st["mean"] == mean
    assert st["std"] == std
    assert st["z"] == (values[-1] - mean) / std
    assert st["last"] == values[-1]
    assert st["n_points"] == n


def test_stats_uses_tail_window():
    now = 1_800_000_000.0
    s = SpreadSeries(window=10)
    for i in range(20):
        s.append(now + i, float(i))   # 0..19，尾部 10 点 = 10..19
    st = s.stats()
    tail = [float(i) for i in range(10, 20)]
    mean = sum(tail) / 10
    var = sum((x - mean) ** 2 for x in tail) / 9
    assert st["mean"] == mean
    assert st["std"] == var ** 0.5
    assert st["last"] == 19.0


def test_stats_returns_empty_below_10_points():
    s = SpreadSeries()
    for i in range(9):
        s.append(float(i), float(i))
    assert s.stats() == {}


def test_extremes_segments_by_timestamp():
    s, now, _values = _make_series(200)
    ex = s.extremes(now=now)
    # 1y 窗口（>= now-365d）：i >= 18 -> 值 18..199（400 天前的 i=0..17 被截断）
    assert ex["max_1y"] == 199.0
    assert ex["min_1y"] == 18.0
    # 6m 窗口（>= now-182d）：i >= 109 -> 值 109..199
    assert ex["max_6m"] == 199.0
    assert ex["min_6m"] == 109.0
    # window_days = 1y 窗内最老点（i=18，364 天前）到 now 的覆盖天数
    assert ex["window_days"] == 364.0
    assert ex["max_1y"] is not None and ex["min_6m"] is not None


def test_extremes_older_than_one_year_excluded():
    now = 1_800_000_000.0
    s = SpreadSeries()
    # 最老点 400 天前（超出 365d 窗口），其余点在窗口内
    s.append(now - 400 * _DAY, 1000.0)
    s.append(now - 100 * _DAY, 5.0)
    s.append(now - 10 * _DAY, 7.0)
    ex = s.extremes(now=now)
    assert ex["max_1y"] == 7.0    # 400 天前的 1000.0 被截断
    assert ex["min_1y"] == 5.0
    # 6m 窗口：两个点都在 182d 内
    assert ex["max_6m"] == 7.0
    assert ex["min_6m"] == 5.0
    # window_days 以 1y 窗内最老点计：100 天
    assert ex["window_days"] == 100.0


def test_extremes_empty_series_returns_empty_dict():
    s, now, _ = _make_series(0)
    assert s.extremes(now=now) == {}


# ---------------------------------------------------------------------------
# SpreadSeries.init_prior 先验语义
# ---------------------------------------------------------------------------
def test_init_prior_mean_std_n():
    s = SpreadSeries()
    h1 = {"t": [1.0, 2.0, 3.0], "v": [1.0, 2.0, 3.0]}
    out = s.init_prior(h1)
    assert out["mean"] == 2.0
    assert out["std"] == 1.0
    assert out["n"] == 3
    # prior() 返回副本，内容一致
    assert s.prior() == {"mean": 2.0, "std": 1.0, "n": 3}
    assert s.prior() is not s.prior()
    # 历史点进入缓存但不算 live 点
    assert s.live_points() == 0
    assert s.point_count() == 3
    assert s.arrays() == ([1.0, 2.0, 3.0], [1.0, 2.0, 3.0])


def test_init_prior_skips_misaligned_points():
    s = SpreadSeries()
    h1 = {"t": [1.0, "bad", 3.0, 4.0], "v": [1.0, 2.0, 3.0, None]}
    out = s.init_prior(h1)
    # "bad" 与 None 均无法 float，成对丢弃 -> 只剩 (1.0,1.0),(3.0,3.0)
    assert out == {"mean": 2.0, "std": 2.0 ** 0.5, "n": 2}
    assert s.point_count() == 2


def test_init_prior_below_two_points_returns_empty_and_unsets():
    s = SpreadSeries()
    assert s.init_prior({"t": [1.0], "v": [1.0]}) == {}
    assert s.prior() == {}


# ---------------------------------------------------------------------------
# StrategyRunner.compute_spread：用户公式（乘法合成三腿）
# ---------------------------------------------------------------------------
RUNNER_LEGS = [
    {"symbol": "USDJPY", "side": "sell", "weight": -1.0},
    {"symbol": "USDCHF", "side": "buy", "weight": 1.0},
    {"symbol": "CHFJPY", "side": "buy", "weight": 1.0},
]
RUNNER_TICKS = {
    "USDJPY": {"bid": 156.12, "ask": 156.15},
    "USDCHF": {"bid": 0.8833, "ask": 0.8835},
    "CHFJPY": {"bid": 176.70, "ask": 176.80},
}


def _runner() -> StrategyRunner:
    return StrategyRunner("test_strategy", RUNNER_LEGS,
                          {"window": 500, "k": 2.0, "grid_step": 0.5,
                           "levels": 3, "max_positions": 10})


def test_compute_spread_user_formula():
    r = _runner()
    out = r.compute_spread(RUNNER_TICKS)
    assert out is not None
    spread, hedge = out
    # 用户公式：156.12(bid) − 0.8835×176.8（合成 = 买腿 ask 的乘法合成）
    assert abs(spread - (156.12 - 0.8835 * 176.8)) < 1e-9
    # hedge 快照：目标腿在首位（用 bid），买腿用 ask
    assert hedge[0] == {"symbol": "USDJPY", "side": "sell",
                        "weight": -1.0, "price": 156.12}
    assert hedge[1] == {"symbol": "USDCHF", "side": "buy",
                        "weight": 1.0, "price": 0.8835}
    assert hedge[2] == {"symbol": "CHFJPY", "side": "buy",
                        "weight": 1.0, "price": 176.80}


def test_compute_spread_missing_tick_returns_none():
    r = _runner()
    ticks = dict(RUNNER_TICKS)
    del ticks["CHFJPY"]
    assert r.compute_spread(ticks) is None
    assert r.compute_spread({}) is None


def test_compute_spread_non_positive_leg_price_returns_none():
    r = _runner()
    ticks = {k: dict(v) for k, v in RUNNER_TICKS.items()}
    ticks["USDCHF"]["ask"] = 0.0
    assert r.compute_spread(ticks) is None


def test_compute_spread_no_legs_returns_none():
    r = StrategyRunner("empty", [], {})
    assert r.compute_spread(RUNNER_TICKS) is None


# ---------------------------------------------------------------------------
# SignalEngine.on_spread
# ---------------------------------------------------------------------------
def _stats(z: float) -> dict:
    return {"mean": 0.0, "std": 1.0, "z": z, "last": z, "n_points": 50}


def test_on_spread_upside_triggers_open_sell():
    eng = SignalEngine("s1", k=2.0, grid_step=0.5, levels=3)
    sig = eng.on_spread(_stats(2.5), ts=1_000.0)
    assert sig is not None
    assert sig["action"] == "open"
    assert sig["side"] == "sell"
    # level = mean + k*std = 0 + 2.0*1.0 = 2.0（i=0 层最先触发）
    assert sig["level"] == 2.0
    assert sig["count"] == 1
    assert eng.open_count("sell") == 1
    assert eng.open_count() == 1


def test_on_spread_downside_triggers_open_buy():
    eng = SignalEngine("s1", k=2.0, grid_step=0.5, levels=3)
    sig = eng.on_spread(_stats(-2.5), ts=1_000.0)
    assert sig is not None
    assert sig["action"] == "open"
    assert sig["side"] == "buy"
    assert sig["level"] == -2.0


def test_on_spread_within_band_returns_none():
    eng = SignalEngine("s1", k=2.0, grid_step=0.5, levels=3)
    assert eng.on_spread(_stats(1.0), ts=1_000.0) is None
    assert eng.on_spread({}, ts=1_000.0) is None


def test_on_spread_grid_progression():
    eng = SignalEngine("s1", k=2.0, grid_step=0.5, levels=3)
    sig0 = eng.on_spread(_stats(2.1), ts=1.0)
    assert sig0["level"] == 2.0
    # 网格阈值是进场门槛：每次 z 超过最低门槛即新增一笔（level 均为 k*std 基准层），
    # 同方向计数递增
    sig1 = eng.on_spread(_stats(2.6), ts=2.0)
    assert sig1["action"] == "open"
    assert sig1["level"] == 2.0
    assert sig1["count"] == 2


def test_on_spread_close_when_z_reverts():
    eng = SignalEngine("s1", k=2.0, grid_step=0.5, levels=3)
    eng.on_spread(_stats(2.5), ts=1.0)
    sig = eng.on_spread(_stats(0.1), ts=2.0)
    assert sig is not None
    assert sig["action"] == "close"
    assert sig["side"] == "sell"
    assert sig["count"] == 1


def test_on_spread_blocked_at_max_positions():
    eng = SignalEngine("s1", k=2.0, grid_step=0.5, levels=3, max_positions=1)
    first = eng.on_spread(_stats(2.5), ts=1.0)
    assert first["action"] == "open"
    second = eng.on_spread(_stats(2.5), ts=2.0)
    assert second["action"] == "blocked"
    assert eng.open_count("sell") == 1
