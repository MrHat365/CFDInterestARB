"""USD 口径的中性权重与单腿隔夜利息换算（仅依赖 numpy）。

usd_neutral_weights：把每条腿按 1 标准手（FX 标准名义）的逐币种敞口写成
USD 价值的符号流向量，求非负权重 w 使组合后各币种的 USD 净敞口最小——
把「等手数中性」推广为「USD 口径加权中性」，解决交叉盘名义/计价不对称。

swap_to_usd_per_day：按 MQL5 SYMBOL_SWAP_MODE 权威语义把单腿隔夜利息
换算为 USD/天（1 标准手）。mode 权威取值：
    0 DISABLED          无掉期
    1 POINTS            按点
    2 CURRENCY_SYMBOL   品种基础货币
    3 CURRENCY_MARGIN   保证金货币
    4 CURRENCY_DEPOSIT  账户货币
    5 INTEREST_CURRENT  当前价年利率（FX 名义近似）
    6 INTEREST_OPEN     开仓价年利率（需持仓开仓价，本模块不估算）
    7/8 REOPEN          重开价格调整（不冒充年息）
"""

from __future__ import annotations

import math

import numpy as np

# 残余流容差：任一币种残余 USD 流不得超过该币种最大单腿流的 5%
_RESIDUAL_TOL = 0.05


def _parse_fx(symbol: str) -> tuple[str, str] | None:
    """与 opportunities.parse_fx 同口径的 (base, quote) 六字母解析。"""
    name = symbol.split(".", 1)[0].upper()
    if len(name) != 6:
        return None
    return name[:3], name[3:]


def _usd_rate(usd_rates: dict[str, float], currency: str) -> float | None:
    """币种 -> USD 单价；USD 恒为 1.0，缺失/非法返回 None。"""
    if currency == "USD":
        return 1.0
    r = usd_rates.get(currency) if isinstance(usd_rates, dict) else None
    if not isinstance(r, (int, float)) or isinstance(r, bool) or not math.isfinite(float(r)) or float(r) <= 0:
        return None
    return float(r)


def usd_neutral_weights(
    legs: list[dict], usd_rates: dict[str, float]
) -> list[float] | None:
    """求解组合的 USD 中性非负权重。

    :param legs: [{"symbol": "EURJPY.s", "side": "buy"}, ...]（side: buy/sell）
    :param usd_rates: 币种 -> USD 汇率（USD=1.0），如 1 EUR = 1.08 USD
    :return: 与 legs 等长的权重列表（按 max(|w|) 归一化），不可中性化返回 None

    每腿（1 标准手）的货币流，各币种单独按 USD 计价（敞口列口径）：
        buy:  吃 1 手 base（价值 usd_rates[base] USD），付 rb/rq 单位 quote
              （价值 (rb/rq)×rq = rb USD）
        sell: 反号。即 base 列 ±rb，quote 列 ∓rb。
    组成 行=腿 的矩阵 A（每行按腿方向取正负），求 w>=0 使 ||A.T @ w|| 最小。
    数学上该问题需约束 ||w||=1 才有非平凡解（否则 w=0 平凡最优），其精确解
    即 A.T 的最小奇异右向量；用 np.linalg.lstsq(A, zeros) 的最小二乘思想处理欠定
    最小范数问题，含负分量时按投影迭代：把负分量 clip 到 0，对剩余支撑集
    重新求解（≤5 腿几轮即
    收敛）。最终按 max(|w|) 归一化；
    若任一币种残余流 > 该币种最大单腿流的 5%，判为不可中性化，返回 None。
    """
    if not isinstance(legs, list) or not legs:
        return None

    leg_flows: list[dict[str, float]] = []
    for leg in legs:
        if not isinstance(leg, dict):
            return None
        pair = _parse_fx(leg.get("symbol", ""))
        side = leg.get("side")
        if pair is None or side not in ("buy", "sell"):
            return None
        base, quote = pair
        rb = _usd_rate(usd_rates, base)
        rq = _usd_rate(usd_rates, quote)
        if rb is None or rq is None:
            return None
        sign = 1.0 if side == "buy" else -1.0
        contract = leg.get("contract_size", 100_000)
        if not isinstance(contract, (int, float)) or not math.isfinite(contract) or contract <= 0:
            return None
        rb *= contract / 100_000
        # Normalize by 100k; relative contract sizes still affect lot weights.
        quote_units = rb / rq               # quote 币单位（100k 名义口径）
        quote_usd = quote_units * rq        # = rb，即该腿 quote 敞口的 USD 价值
        leg_flows.append({base: sign * rb, quote: -sign * quote_usd})

    currencies = sorted({c for f in leg_flows for c in f})
    # A：行=腿（每行按腿方向取正负），列=币种
    a_mat = np.zeros((len(legs), len(currencies)))
    for i, flows in enumerate(leg_flows):
        for j, cur in enumerate(currencies):
            a_mat[i, j] = flows.get(cur, 0.0)

    w = _project_min_flow(a_mat.T)  # B = A.T：行=币种，列=腿
    if w is None:
        return None

    # 残余流容差检验：各币种的加权 USD 净敞口
    # （“最大单腿流”按加权后的单腿敞口取，与归一化尺度无关）
    residual = a_mat.T @ w  # 每币种的 USD 净敞口
    for j in range(len(currencies)):
        worst = max(abs(w[i] * a_mat[i, j]) for i in range(len(legs)))
        if worst > 0 and abs(residual[j]) > _RESIDUAL_TOL * worst:
            return None

    peak = float(np.max(np.abs(w)))
    if peak <= 0:
        return None
    return [float(x / peak) for x in w]


def _project_min_flow(b_mat: np.ndarray) -> np.ndarray | None:
    """min ||B @ w|| s.t. w >= 0（B：行=币种，列=腿）。

    无约束时精确解是 B 的最小奇异右向量（等价于对零右端项的欠定
    最小范数最小二乘问题加上 ||w||=1 约束消去平凡零解）；若该向量
    含负分量，把负分量 clip 到 0 并在剩余支撑集上重新求解（投影迭代）。
    """
    n = b_mat.shape[1]
    active = list(range(n))
    w = np.zeros(n)
    for _ in range(n + 2):  # 每轮要么收敛要么支撑集收窄，>n+2 轮必然终止
        if not active:
            return None
        sub = b_mat[:, active]
        vt = np.linalg.svd(sub, full_matrices=True)[2]
        v = vt[-1]
        # 定向：以绝对值最大分量为正，避免整体反号导致全列被误 clip
        pivot = int(np.argmax(np.abs(v)))
        if v[pivot] < 0:
            v = -v
        w[active] = np.maximum(v, 0.0)
        neg = v < 0
        if not neg.any():
            return w
        # 投影：负分量 clip 到 0，在剩余分量上继续求解
        active = [col for k, col in enumerate(active) if not neg[k]]
    return None


def swap_to_usd_per_day(
    info: dict,
    side: str,
    usd_rates: dict[str, float],
    account_currency: str = "USD",
) -> float | None:
    """单腿隔夜利息 -> USD/天（1 标准手）；字段无效/键缺失返回 None。

    :param info: MT5 SymbolInfo 所需字段：
        swap_long, swap_short, swap_mode, point, tick_size, tick_value,
        trade_contract_size, currency_base, currency_profit, currency_margin
    """
    if not isinstance(info, dict):
        return None
    if side not in ("buy", "sell"):
        return None

    raw = info.get("swap_long" if side == "buy" else "swap_short")
    if not isinstance(raw, (int, float)) or isinstance(raw, bool):
        return None
    swap = float(raw)
    if not math.isfinite(swap):
        return None

    mode = info.get("swap_mode")
    if not isinstance(mode, int) or isinstance(mode, bool):
        return None

    def rate_to_usd(currency: str | None) -> float | None:
        """换算链终点：币种 -> USD 的汇率。"""
        if not isinstance(currency, str):
            return None
        return _usd_rate(usd_rates, currency)

    if mode == 0:  # DISABLED：无掉期，等价于不可计价
        return None

    if mode == 1:  # POINTS：点数
        point = info.get("point")
        tick_size = info.get("tick_size")
        tick_value = info.get("tick_value")
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool)
                   for v in (point, tick_size, tick_value)):
            return None
        if not all(math.isfinite(float(v)) and float(v) > 0 for v in (point, tick_size, tick_value)):
            return None
        deposit_usd = rate_to_usd(account_currency)
        if deposit_usd is None:
            return None
        # MT5 trade_tick_value is denominated in the deposit currency.
        value_deposit = swap * float(tick_value) * (float(point) / float(tick_size))
        return value_deposit * deposit_usd

    if mode == 4:  # CURRENCY_DEPOSIT：账户货币计价
        if account_currency == "USD":
            return swap
        acc = rate_to_usd(account_currency)
        if acc is None:
            return None
        return swap * acc

    if mode == 2:  # CURRENCY_SYMBOL：品种基础货币计价
        profit_usd = rate_to_usd(info.get("currency_base"))
        if profit_usd is None:
            return None
        return swap * profit_usd

    if mode == 3:  # CURRENCY_MARGIN：保证金货币计价
        margin_usd = rate_to_usd(info.get("currency_margin"))
        if margin_usd is None:
            return None
        return swap * margin_usd

    if mode in (6, 7, 8):
        # INTEREST_OPEN needs each position's open price; REOPEN modes encode
        # price adjustments, not annual interest. Do not invent a USD/day yield.
        return None

    if mode == 5:
        contract = info.get("trade_contract_size")
        if not isinstance(contract, (int, float)) or isinstance(contract, bool):
            return None
        if not math.isfinite(float(contract)) or contract <= 0:
            return None
        base_usd = rate_to_usd(info.get("currency_base"))
        if base_usd is None:
            return None
        return float(contract) * base_usd * swap / 100.0 / 360.0

    return None  # 未知 swap_mode
