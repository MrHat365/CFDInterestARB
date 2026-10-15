"""opportunities.py 纯逻辑层测试（图构建 / 环枚举 / generate 空输入守卫）。

断言前已通读 mt5_trader/opportunities.py 源码：
- build_edges: 以 frozenset((base, quote)) 为键 -> [(symbol, base, quote)] 平行边列表；
- _find_cycles: 返回 [(from_c, to_c, symbol, base)] 闭合路径列表（除首尾外节点
  不重复，平行边允许回走），旋转/反向等价去重；
- sides_for_cycle: from_c == base -> buy，否则 sell。
"""

from __future__ import annotations

from mt5_trader.opportunities import (
    _find_cycles,
    build_edges,
    generate,
    sides_for_cycle,
)


# ---------------------------------------------------------------------------
# generate() 空输入守卫
# ---------------------------------------------------------------------------
STOCKS = [
    {"symbol": "EURUSD", "swap_long": 1.0, "swap_short": -1.0, "swap_mode": 4},
    {"symbol": "USDJPY", "swap_long": 1.0, "swap_short": -1.0, "swap_mode": 4},
    {"symbol": "EURJPY", "swap_long": 1.0, "swap_short": -1.0, "swap_mode": 4},
]
RATES = {"EUR": 1.08, "JPY": 1.0 / 156.0}


def test_generate_empty_stocks_returns_empty():
    assert generate([], RATES) == []
    assert generate(STOCKS, RATES, should_cancel=lambda: True) == []


def test_generate_fx_rates_none_returns_empty():
    assert generate(STOCKS, None) == []


def test_generate_triangle_produces_both_directions_sorted():
    """三腿中性环（买 EURUSD + 买 USDJPY + 卖 EURJPY）两个方向都应输出，
    且按 net_swap 降序。swap_mode=4（入金货币 USD）下 swap 即 USD/天。"""
    out = generate(STOCKS, RATES)
    assert len(out) == 2
    # 净息降序
    assert out[0]["net_swap"] >= out[1]["net_swap"]
    # 两个方向互为反演：同一组 symbol，方向全翻转
    syms_a = {l["symbol"] for l in out[0]["legs"]}
    syms_b = {l["symbol"] for l in out[1]["legs"]}
    assert syms_a == syms_b
    for la, lb in zip(out[0]["legs"], out[1]["legs"]):
        assert la["symbol"] == lb["symbol"]
        assert {la["side"], lb["side"]} == {"buy", "sell"}


def test_generate_skips_symbols_with_missing_fx_rate():
    # JPY 缺汇率 -> 涉及 JPY 的组合不可计价 -> 无候选
    out = generate(STOCKS, {"EUR": 1.08})
    assert out == []


# ---------------------------------------------------------------------------
# build_edges
# ---------------------------------------------------------------------------
def test_build_edges_parses_fx_and_skips_non_fx():
    stocks = [
        {"symbol": "EURUSD"},
        {"symbol": "USDJPY"},
        {"symbol": "GBPUSDT"},      # 7 字母，parse_fx 不识别
        {"symbol": "XAUUSD"},       # XAU 不在货币词表
        {"symbol": "EURUSD.s"},     # 后缀剥离后与 EURUSD 同对 -> 平行边
    ]
    edges = build_edges(stocks)
    assert edges[frozenset(("EUR", "USD"))] == [
        ("EURUSD", "EUR", "USD"),
        ("EURUSD.s", "EUR", "USD"),
    ]
    assert edges[frozenset(("USD", "JPY"))] == [("USDJPY", "USD", "JPY")]
    # 非法品种不产生边
    all_syms = [sym for items in edges.values() for sym, _, _ in items]
    assert "GBPUSDT" not in all_syms
    assert "XAUUSD" not in all_syms


def test_build_edges_empty_stocks():
    assert build_edges([]) == {}


# ---------------------------------------------------------------------------
# _find_cycles + sides_for_cycle
# ---------------------------------------------------------------------------
def test_find_cycles_triangle_shape():
    stocks = [
        {"symbol": "EURUSD"},
        {"symbol": "USDJPY"},
        {"symbol": "EURJPY"},
    ]
    edges = build_edges(stocks)
    cycles = _find_cycles(edges, max_len=5)
    triangles = [c for c in cycles if len(c) == 3]
    assert len(triangles) == 1   # 旋转/反向去重后唯一
    cyc = triangles[0]
    # 闭合：每步 from->to 首尾相接
    for a, b in zip(cyc, cyc[1:]):
        assert a[1] == b[0]
    assert cyc[-1][1] == cyc[0][0]
    # 三条腿恰好是三个品种，各出现一次
    assert {step[2] for step in cyc} == {"EURUSD", "USDJPY", "EURJPY"}
    sides = sides_for_cycle(cyc)
    assert ("EURUSD", "buy") in sides and ("USDJPY", "buy") in sides
    assert ("EURJPY", "sell") in sides   # from=quote -> 卖


def test_find_cycles_two_leg_parallel_edges():
    stocks = [{"symbol": "GBPUSD"}, {"symbol": "GBPUSD.s"}]
    edges = build_edges(stocks)
    cycles = _find_cycles(edges, max_len=5)
    # 平行边允许回走成 2 腿环；包含同品种自环与跨品种环
    two_leg = [c for c in cycles if len(c) == 2]
    pairs = {frozenset((c[0][2], c[1][2])) for c in two_leg}
    assert frozenset(("GBPUSD", "GBPUSD.s")) in pairs
    # 跨品种 2 腿环方向互补：一买一卖
    cross = [c for c in two_leg if {c[0][2], c[1][2]} == {"GBPUSD", "GBPUSD.s"}]
    sides = sides_for_cycle(cross[0])
    assert {s for _, s in sides} == {"buy", "sell"}


def test_find_cycles_no_cycle_in_single_pair():
    edges = build_edges([{"symbol": "EURUSD"}])
    cycles = _find_cycles(edges, max_len=5)
    # 单条边无平行边：唯一可能 2 腿环是同符号自环（去重后仍存在，但
    # generate() 会按 symbols 重复过滤）。这里只断言不产生 >2 腿环。
    assert all(len(c) <= 2 for c in cycles)


def test_sides_for_cycle_direction_semantics():
    # from == base -> buy；from == quote -> sell
    cycle = [
        ("EUR", "USD", "EURUSD", "EUR"),
        ("USD", "EUR", "EURUSD.s", "EUR"),
    ]
    assert sides_for_cycle(cycle) == [("EURUSD", "buy"), ("EURUSD.s", "sell")]
