"""spreads.py 纯逻辑层测试（无 UI、无 worker、无 MT5 依赖）。"""

from __future__ import annotations

import math

from mt5_trader.spreads import (
    build_legs_from_opportunity,
    floor_to_step,
    pick_target_symbol,
    round_to_step,
    spread_quote,
    synthetic_price,
)


# ---------------------------------------------------------------------------
# 步长环整
# ---------------------------------------------------------------------------
def test_floor_to_step_exact_and_partial():
    # 恰好整倍数：原值返回（内部 1e-9 容差防浮点毛刺）
    assert floor_to_step(10.05, 0.01) == 10.05
    assert floor_to_step(0.1, 0.1) == 0.1
    # 非整倍数：向下环整
    assert floor_to_step(10.059, 0.01) == 10.05
    assert floor_to_step(0.03, 0.02) == 0.02
    assert floor_to_step(2.5, 1.0) == 2.0


def test_floor_to_step_non_positive_step_returns_value():
    # step=0 / 负 step：原值返回（源码显式分支）
    assert floor_to_step(3.14159, 0) == 3.14159
    assert floor_to_step(3.14159, -0.01) == 3.14159


def test_round_to_step_nearest():
    assert round_to_step(10.057, 0.01) == 10.06
    assert round_to_step(10.053, 0.01) == 10.05
    assert round_to_step(2.5, 1.0) in (2.0, 3.0)  # 平局语义不纳入契约，只测常规


def test_round_to_step_non_positive_step_returns_value():
    assert round_to_step(1.2345, 0) == 1.2345
    assert round_to_step(1.2345, -0.01) == 1.2345


# ---------------------------------------------------------------------------
# pick_target_symbol：卖出方向 |权重| 最大；无卖出退回买入最大
# ---------------------------------------------------------------------------
def test_pick_target_symbol_prefers_max_abs_weight_among_sells():
    # 源码口径：卖出判定 = side==sell 且 weight>0（权重存非负，符号由 side 表达）
    legs = [
        {"symbol": "USDCHF", "side": "buy", "weight": 3.0},
        {"symbol": "USDJPY", "side": "sell", "weight": 0.5},
        {"symbol": "EURJPY", "side": "sell", "weight": 2.0},
    ]
    # 卖出池只有两条，买入腿权重再大也不参与
    assert pick_target_symbol(legs) == "EURJPY"


def test_pick_target_symbol_falls_back_to_buy_when_no_sell():
    legs = [
        {"symbol": "USDCHF", "side": "buy", "weight": 1.0},
        {"symbol": "CHFJPY", "side": "buy", "weight": 3.0},
    ]
    assert pick_target_symbol(legs) == "CHFJPY"


def test_pick_target_symbol_empty_legs_returns_none():
    assert pick_target_symbol([]) is None


# ---------------------------------------------------------------------------
# synthetic_price：±1 幂乘法合成（对数线性）
# ---------------------------------------------------------------------------
# 经典三角：USDJPY = USDCHF × CHFJPY
SYN_PRICES = {"USDJPY": 156.12, "USDCHF": 0.8835, "CHFJPY": 176.8}
SYN_LEGS = [
    {"symbol": "USDJPY", "side": "sell", "weight": -1.0},
    {"symbol": "USDCHF", "side": "buy", "weight": 1.0},
    {"symbol": "CHFJPY", "side": "buy", "weight": 1.0},
]


def test_synthetic_price_multiplicative_cross():
    # 排除实际腿 USDJPY 后：合成 = exp(log(0.8835)+log(176.8)) = 0.8835×176.8
    synth = synthetic_price(SYN_PRICES, SYN_LEGS, exclude="USDJPY")
    assert synth == 0.8835 * 176.8


def test_synthetic_price_sell_leg_inverts_power():
    # sell 腿幂取 -1：0.8835×176.8 / 156.12（exp(log和) 与连乘有末位浮点差，容差 1e-9）
    synth = synthetic_price(SYN_PRICES, SYN_LEGS, exclude=None)
    assert abs(synth - 0.8835 * 176.8 / 156.12) < 1e-9


def test_synthetic_price_non_positive_price_returns_nan():
    bad = dict(SYN_PRICES, USDCHF=0.0)
    assert math.isnan(synthetic_price(bad, SYN_LEGS, exclude="USDJPY"))
    bad2 = dict(SYN_PRICES, CHFJPY=-176.8)
    assert math.isnan(synthetic_price(bad2, SYN_LEGS, exclude="USDJPY"))


def test_synthetic_price_missing_symbol_returns_nan():
    assert math.isnan(synthetic_price({"USDCHF": 0.8835}, SYN_LEGS, exclude="USDJPY"))


def test_synthetic_price_exclude_removes_exactly_one_leg():
    # exclude 只剔除一个品种：剔除卖腿 USDJPY -> 剩余买腿乘法合成
    assert abs(synthetic_price(SYN_PRICES, SYN_LEGS, exclude="USDJPY")
               - 0.8835 * 176.8) < 1e-9
    # 剔除买腿 CHFJPY -> 剩 USDJPY(sell, 幂 -1) 与 USDCHF(buy)：0.8835/156.12
    assert abs(synthetic_price(SYN_PRICES, SYN_LEGS, exclude="CHFJPY")
               - 0.8835 / 156.12) < 1e-9


# ---------------------------------------------------------------------------
# spread_quote：实际腿 bid − 合成买价
# ---------------------------------------------------------------------------
def test_spread_quote_user_formula():
    q = spread_quote(SYN_PRICES, SYN_LEGS)
    assert q["ok"] is True
    assert q["target"] == "USDJPY"
    expected = 156.12 - 0.8835 * 176.8
    assert abs(q["spread"] - expected) < 1e-9
    assert abs(q["synthetic_ask"] - 0.8835 * 176.8) < 1e-9


def test_spread_quote_missing_target_price():
    prices = {"USDCHF": 0.8835, "CHFJPY": 176.8}
    q = spread_quote(prices, SYN_LEGS)
    assert q["ok"] is False
    assert q["missing"] == "USDJPY"


def test_spread_quote_non_positive_target_price():
    prices = dict(SYN_PRICES, USDJPY=0.0)
    q = spread_quote(prices, SYN_LEGS)
    assert q["ok"] is False
    assert q["missing"] == "USDJPY"


def test_spread_quote_non_positive_leg_price():
    # 其余腿价格非正 -> 合成价 nan -> ok=False（missing="legs"）
    prices = dict(SYN_PRICES, USDCHF=-0.8835)
    q = spread_quote(prices, SYN_LEGS)
    assert q["ok"] is False
    assert q["missing"] == "legs"


def test_spread_quote_empty_legs():
    q = spread_quote(SYN_PRICES, [])
    assert q["ok"] is False


# ---------------------------------------------------------------------------
# build_legs_from_opportunity：权重归一 + 手数系数 + 步长环整
# ---------------------------------------------------------------------------
def test_build_legs_from_opportunity_weights_and_volume():
    opportunity = {
        "legs": [
            {"symbol": "USDJPY", "side": "sell", "weight": -2.0},
            {"symbol": "USDCHF", "side": "buy", "weight": 1.0},
        ]
    }
    quotes = {
        "USDJPY": {"bid": 156.12, "ask": 156.15, "bid_volume": 10.0, "ask_volume": 10.0},
        "USDCHF": {"bid": 0.8833, "ask": 0.8835, "bid_volume": 20.0, "ask_volume": 20.0},
    }
    meta = {"USDJPY": {"min_volume": 0.01, "volume_step": 0.01}}
    legs = build_legs_from_opportunity(opportunity, quotes, layer_volume=0.12)

    assert [l.symbol for l in legs] == ["USDJPY", "USDCHF"]
    # 手数系数 = |w|/max(|w|) × layer_volume，再环整到该腿步长
    assert legs[0].multiplier == 0.12          # 2/2 × 0.12
    assert legs[1].multiplier == 0.06          # 1/2 × 0.12
    assert legs[0].side == "sell"
    assert legs[1].side == "buy"
    # 盘口快照透传
    assert legs[0].bid == 156.12 and legs[0].ask == 156.15
    assert legs[1].bid == 0.8833 and legs[1].ask == 0.8835
    # symbol_meta 只对显式给出的品种生效，缺省 0.01
    assert legs[0].volume_step == 0.01 and legs[0].min_volume == 0.01
    assert legs[1].volume_step == 0.01 and legs[1].min_volume == 0.01


def test_build_legs_from_opportunity_missing_quote_defaults_zero():
    opportunity = {"legs": [{"symbol": "USDJPY", "side": "sell", "weight": -1.0}]}
    legs = build_legs_from_opportunity(opportunity, quotes={}, layer_volume=1.0)
    assert legs[0].bid == 0.0 and legs[0].ask == 0.0
    assert legs[0].bid_volume == 0.0 and legs[0].ask_volume == 0.0


def test_build_legs_from_opportunity_rounds_to_volume_step():
    opportunity = {"legs": [{"symbol": "USDJPY", "side": "sell", "weight": -1.0}]}
    # 0.1234 环整到 0.01 步长 -> 0.12
    legs = build_legs_from_opportunity(
        opportunity, quotes={}, layer_volume=0.1234,
        symbol_meta={"USDJPY": {"min_volume": 0.01, "volume_step": 0.01}},
    )
    assert legs[0].multiplier == 0.12
