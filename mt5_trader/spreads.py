"""价差组合核心（模式引 vnpy_spreadtrading.base.SpreadData，脱离框架自实现）。

职责：
- 用 opportunity 的带符号权重自动构建价差（不人工管理）；
- spread bid/ask 计算：买腿取腿 bid、卖腿取腿 ask（方向取反语义同
  vnpy SpreadData.calculate_price 的 variable_directions 翻转）；
- 盘口可成交量 = min(各腿量 / |倍数| 环到 min_volume)（vnpy 同语义）；
- 腿手数 ↔ 价差单位换算（calculate_leg_volume / calculate_spread_volume）；
- 主动腿选择 + 手数环到步长。

不存在 vnpy 框架依赖（TickData/ContractData 等以纯 dict/float 输入）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


def floor_to_step(value: float, step: float) -> float:
    """向下取整到步长（对齐 vnpy floor_to 语义），step<=0 时原值返回。"""
    if step <= 0:
        return value
    return round(math.floor(value / step + 1e-9) * step, 10)


def round_to_step(value: float, step: float) -> float:
    """就近取整到步长（下单手数用），step<=0 时原值返回。"""
    if step <= 0:
        return value
    return round(round(value / step) * step, 10)


@dataclass
class SpreadLeg:
    """价差腿（两侧行情报价快照）。"""

    symbol: str
    side: str             # 组合方向 "buy"/"sell"（决定价格公式的方向取反）
    multiplier: float = 1.0   # 每 1 个价差单位该腿的手数（带方向：卖为负），来自 USD 中性权重
    bid: float = 0.0
    ask: float = 0.0
    bid_volume: float = 0.0
    ask_volume: float = 0.0
    min_volume: float = 0.01
    volume_step: float = 0.01

    @property
    def signed(self) -> float:
        return self.multiplier if self.side == "buy" else -self.multiplier


@dataclass
class SpreadCombo:
    """价差组合：自动构建，盘口/量换算/主动腿全自动化。"""

    legs: list[SpreadLeg]

    def __post_init__(self) -> None:
        self.leg_map: dict[str, SpreadLeg] = {l.symbol: l for l in self.legs}

    # ------------------------------------------------------------------
    # 盘口（价格 & 量）
    # ------------------------------------------------------------------
    def calculate_price(self) -> dict:
        """价差盘口：bid/ask 与可成交量（逐腿方向取反 + 量取最小，语义同 vnpy）。

        SELL 组合报价时：买腿收其 bid、卖腿付其 ask（即 spread.bid 按
        vnpy 的 bid_data 语义取腿的"收价"）。
        任何一腿 bid/ask 无效 -> 返回 {"ok": False}。
        """
        bid_data: dict[str, float] = {}
        ask_data: dict[str, float] = {}
        cap_bid = math.inf
        cap_ask = math.inf

        for leg in self.legs:
            if leg.bid <= 0 or leg.ask <= 0 or leg.ask < leg.bid:
                return {"ok": False, "missing": leg.symbol}
            # 价差系数带符号（卖腿权重为负，等价 vnpy price_formula 的负系数导数）；
            # 方向取反：卖腿在组合 bid 侧取其 ask（先卖收价），在 ask 侧取其 bid
            if leg.side == "buy":
                bid_data[leg.symbol] = leg.signed * leg.bid
                ask_data[leg.symbol] = leg.signed * leg.ask
            else:
                bid_data[leg.symbol] = leg.signed * leg.ask
                ask_data[leg.symbol] = leg.signed * leg.bid

            # 盘口量：|倍数| 归一后向下环到最小手；卖腿 bid/ask 互换
            m = abs(leg.multiplier) or 1.0
            step = leg.min_volume or 0.01
            if leg.side == "buy":
                b_v = floor_to_step(leg.bid_volume / m, step)
                a_v = floor_to_step(leg.ask_volume / m, step)
            else:
                b_v = floor_to_step(leg.ask_volume / m, step)
                a_v = floor_to_step(leg.bid_volume / m, step)
            cap_bid = min(cap_bid, b_v)
            cap_ask = min(cap_ask, a_v)

        bid = sum(bid_data.values())
        ask = sum(ask_data.values())
        return {
            "ok": True,
            "bid": bid,
            "ask": ask,
            "bid_volume": cap_bid if cap_bid != math.inf else 0.0,
            "ask_volume": cap_ask if cap_ask != math.inf else 0.0,
        }

    # ------------------------------------------------------------------
    # 换算
    # ------------------------------------------------------------------
    def leg_volume(self, symbol: str, spread_units: float) -> float:
        """价差单位 -> 该腿手数（带方向：卖腿为负）。"""
        return self.leg_map[symbol].signed * spread_units

    def spread_units(self, symbol: str, leg_volume: float) -> float:
        """该腿(净)手数 -> 价差单位（vnpy calculate_spread_volume 语义）。"""
        m = self.leg_map[symbol].signed
        if abs(m) < 1e-12:
            return 0.0
        return leg_volume / m

    # ------------------------------------------------------------------
    # 主动腿（先吃单的那条）
    # ------------------------------------------------------------------
    def pick_active_symbol(self) -> str:
        """主动腿 = 当前点差最宽的腿（先把最贵的锁定价差，其余腿为被动对冲）。"""
        best, best_w = "", -1.0
        for leg in self.legs:
            if leg.bid <= 0 or leg.ask <= 0:
                continue
            width = leg.ask - leg.bid
            if width > best_w:
                best, best_w = leg.symbol, width
        if not best:
            best = max(self.leg_map, key=lambda s: abs(self.leg_map[s].multiplier))
        return best

    # ------------------------------------------------------------------
    # 目标手数（含步长环整）
    # ------------------------------------------------------------------
    def leg_targets(self, spread_units: float) -> dict[str, float]:
        """给定目标价差单位 -> {symbol: 带符号目标手数（环到该腿步长）}。"""
        out: dict[str, float] = {}
        for leg in self.legs:
            raw = self.leg_volume(leg.symbol, spread_units)
            out[leg.symbol] = round_to_step(raw, leg.volume_step or 0.01)
        return out


def pick_target_symbol(legs: list[dict]) -> str | None:
    """组合的实际报价腿 = 卖出方向 |权重| 最大）的那条。
    价差以它计价：价差 = 实际腿 bid − 其余腿的乘法合成价。
    无卖出腿时退回 |权重| 最大的买入腿。"""
    sell = [m for m in legs if _w(m) * (1 if m.get("side", "buy") == "buy" else -1) < 0]
    pool = sell or list(legs)
    if not pool:
        return None
    best = max(pool, key=lambda m: abs(_w(m)))
    return str(best.get("symbol", "")) or None


def synthetic_price(
    prices: dict[str, float], legs: list[dict], exclude: str | None,
) -> float:
    """剩余腿的**乘法（对数线性）合成价**——外汇交叉盘口径（用户公式）：

        合成买 USDJPY(Ask) = USDCHF Ask × CHFJPY Ask

    对数线性实现：合成 = exp(Σ s_i·log(侧价_i))，幂取 ±1（按腿方向），
    正权腿取 ask 侧、负权腿取 bid 侧（权利相反的限价侧）。
    **权重（手数比例）不参与定价幂**——手数管理走 SpreadLeg.multiplier；
    合成价是三角交叉面的定价偏离量（定价价差），跟手数无关。
    任一价非正 -> 返回 nan。"""
    s = 0.0
    for m in legs:
        sym = str(m.get("symbol", ""))
        if not sym or sym == exclude:
            continue
        p = _f(prices.get(sym), float("nan"))
        if not (p > 0):
            return float("nan")
        sign = 1.0 if m.get("side", "buy") == "buy" else -1.0
        s += sign * math.log(p)
    try:
        return math.exp(s)
    except OverflowError:
        return float("inf")


def spread_quote(prices: dict[str, float], legs: list[dict]) -> dict:
    """组合对价差口径的双向报价（乘法合成，用户公式）：

        得到腿 = 卖出方向 |权重| 最大腿（如 USDJPY）
        合成买价(synthetic ask) = 其余腿按方向取 ask 限价侧依权重乘法合成
        价差 = 实际得腿 bid − 合成买价   (让自己卖出实际/买入合成的利润价)
    返回 {"ok": True, "target":.., "spread":..} 或 {"ok": False}
    """
    target = pick_target_symbol(legs)
    if target is None:
        return {"ok": False, "missing": "-"}
    tp = _f(prices.get(target), float("nan"))
    if not (tp > 0):
        return {"ok": False, "missing": target}
    synth = synthetic_price(prices, legs, exclude=target)
    if not (synth > 0):
        return {"ok": False, "missing": "legs"}
    return {"ok": True, "target": target, "synthetic_ask": synth,
            "spread": tp - synth}


def build_legs_from_opportunity(
    opportunity: dict, quotes: dict[str, dict], layer_volume: float,
    symbol_meta: dict[str, dict] | None = None,
) -> list[SpreadLeg]:
    """从交易机会自动构建价差腿列表（无人工配置）。

    :param opportunity: generate() 输出（legs 内含 symbol/side/weight）
    :param quotes: {symbol: {"bid","ask"}} 实时报价快照
    :param layer_volume: 每层手数（**最重腿**的手数；其余腿按权重折算）
    :param symbol_meta: {symbol: {"min_volume","volume_step"}}（缺省 0.01）
    """
    weights = [abs(_w(m)) for m in opportunity["legs"]]
    max_w = max(weights) or 1.0
    legs: list[SpreadLeg] = []
    for m in opportunity["legs"]:
        symbol = m["symbol"]
        side = m.get("side", "buy")
        w = abs(_w(m))
        q = quotes.get(symbol, {})
        meta = (symbol_meta or {}).get(symbol, {})
        legs.append(SpreadLeg(
            symbol=symbol,
            side=side,
            multiplier=round_to_step(w / max_w * layer_volume,
                                     meta.get("volume_step", 0.01)),
            bid=_f(q.get("bid")),
            ask=_f(q.get("ask")),
            bid_volume=_f(q.get("bid_volume")),
            ask_volume=_f(q.get("ask_volume")),
            min_volume=meta.get("min_volume", 0.01),
            volume_step=meta.get("volume_step", 0.01),
        ))
    return legs


def _w(m: dict) -> float:
    try:
        return float(m.get("weight", 1.0))
    except (TypeError, ValueError):
        return 1.0


def _f(v, default: float = 0.0) -> float:
    try:
        if v is None:
            return default
        return float(v)
    except (TypeError, ValueError):
        return default
