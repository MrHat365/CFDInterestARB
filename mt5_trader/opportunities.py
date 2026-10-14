"""套隔夜利息交易机会生成器。

原理：在品种集合上找「中性组合」——组合后各标约货币的 USD 敞口归零，依靠各腿
隔夜利息差实现正 carry。枚举方法：把每个 FX 品种视为货币图上的有向边
(base -> quote)，在图中枚举长度 2~5 的闭合路径（环），方向决定买卖方向：
    循环 c0 -c1-> ... -> ck -> c0
    边由 base 向 quote 正向走 => 该腿买入（long base）；反向走 => 卖出。
非负权重由 weights.usd_neutral_weights 解出（USD 口径加权中性，不等手）；
2 腿中性要求同一货币对在两个不同符号上重复出现（如 GBPUSD 与 GBPUSDT
或不同后缀）。

产出：每条机会包含 legs（symbol/side/中性权重）与净 swap 估计（买用
swap_long、卖用 swap_short，先经 weights.swap_to_usd_per_day 统一换算到
USD/天，再按权重加权求和）。
"""

from __future__ import annotations

from .weights import swap_to_usd_per_day, usd_neutral_weights

# 识别 FX 的货币词表（3 字母 ISO）
_CURRENCIES = {
    "USD", "EUR", "GBP", "JPY", "CHF", "AUD", "NZD", "CAD",
    "SEK", "NOK", "DKK", "PLN", "HUF", "CZK", "TRY", "CNH", "CNY",
    "SGD", "MXN", "ZAR", "HKD", "ILS", "RON",
}


def parse_fx(symbol: str) -> tuple[str, str] | None:
    """从品种名解析 (base, quote)；仅经典 6+3 命名（后缀已剥离）。"""
    name = symbol.split(".", 1)[0].upper()
    if len(name) != 6:
        return None
    base, quote = name[:3], name[3:]
    if base in _CURRENCIES and quote in _CURRENCIES:
        return base, quote
    return None


def build_edges(stocks: list[dict]) -> dict[str, list[tuple[str, str, str]]]:
    """货币对 -> [(symbol, base, quote)]；同对多符号即平行边（2腿中性素材）。"""
    edges: dict[tuple[str, str], list[tuple[str, str, str]]] = {}
    for item in stocks:
        parsed = parse_fx(item["symbol"])
        if parsed is None:
            continue
        base, quote = parsed
        if base == quote:
            continue
        # 任意符号对视为无向边上的双向通行
        edges.setdefault(frozenset((base, quote)), []).append(
            (item["symbol"], base, quote)
        )
    return edges


def sides_for_cycle(cycle: list[tuple[str, str, str, str]]) -> list[tuple[str, str]]:
    """闭合路径 -> [(symbol, side)]。

    :param cycle: [(from_currency, to_currency, symbol, base)] 有序（from=base 或 quote），
                  最后一项必须回到起点。side 由 from 是否为 log 朝向决定：
                  from == base => 买入（做多 base）
                  from == quote => 卖出
    """
    out = []
    for from_c, to_c, symbol, base in cycle:
        if from_c == base:
            out.append((symbol, "buy"))
        else:
            out.append((symbol, "sell"))
    return out


def _find_cycles(edges: dict, max_len: int = 5) -> list[list[tuple[str, str, str, str]]]:
    """在无向多重图上枚举 2..max_len 的除首尾外节点不重复闭合路径。"""
    adj: dict[str, list[tuple[str, str, str]]] = {}
    for pair_key, items in edges.items():
        base, quote = tuple(pair_key)
        for symbol, b, q in items:
            adj.setdefault(b, []).append((q, symbol, b))
            adj.setdefault(q, []).append((b, symbol, b))

    cycles: list[list[tuple[str, str, str, str]]] = []

    def dfs(path) -> None:
        node = path[-1][1]
        start = path[0][0]
        depth = len(path)
        for nxt, symbol, base in adj.get(node, []):
            # 平行边允许走回上一个节点（2 腿中性：同一货币对两个符号）
            if nxt == start and depth >= 2:
                # path[0] 是种子节点，不计入腿
                cycles.append([*path[1:], (node, nxt, symbol, base)])
                continue
            if nxt == node:
                continue
            if depth >= max_len:
                continue
            visited = {p[0] for p in path[1:]} | {path[0][0]}
            if nxt in visited and nxt != start:
                continue
            if nxt == start:
                continue  # 在上一分支处理
            dfs([*path, (node, nxt, symbol, base)])

    for start in sorted(adj):
        dfs([(start, start, "", start)])

    # 去重：旋转/反向等价视为同一条
    unique: dict[frozenset, list] = {}
    for c in cycles:
        key = frozenset(
            frozenset((step[0], step[1])) for step in c
        ) | frozenset(step[2] for step in c)
        if key not in unique:
            unique[key] = c
    return list(unique.values())


def is_neutral(sides: list[tuple[str, str]], stocks_by_symbol: dict) -> bool:
    """等手数下逐货币敞口求和是否为零。"""
    flows: dict[str, int] = {}
    for symbol, side in sides:
        parsed = parse_fx(symbol)
        if parsed is None:
            return False
        base, quote = parsed
        sign = 1 if side == "buy" else -1
        flows[base] = flows.get(base, 0) + sign
        flows[quote] = flows.get(quote, 0) - sign
    return all(v == 0 for v in flows.values())


def generate(
    stocks: list[dict],
    fx_rates: dict[str, float] | None,
    max_legs: int = 5,
    cap: int = 100,
    account_currency: str = "USD",
) -> list[dict]:
    """stocks: [{symbol, swap_long, swap_short, swap_mode, ...}] -> 机会列表。

    关键修正：同一个中性行走环路有 **两个相反的交化方向**（把所有买入变卖出、
    卖出变买入），净息恰好互为相反数，它们是两个真实不同的交易方向。
 此前去重逻辑把两个方向折叠成随机保留的一个，导致生成器武断地只输出
    负息方向而丢弃正息方向。现在两个方向都会输出，按净息降序排列。

    量化口径（USD）：swap_long/short 来自 MT5 SymbolInfo，计价单位随
    swap_mode 不同（点数/品种货币/入金货币等）。这里先用
    weights.swap_to_usd_per_day 把每腿换算为 USD/天（1 标准手），再用
    weights.usd_neutral_weights 解出非负加权中性系数，净息为
    Σ w_i × swap_usd_i（USD/天/组合）。不可 neutralize（含缺汇率、
    swap 换不出 USD、USD 流不闭合）的组合直接跳过。

    UI 侧必须先提供币种汇率快照（fx_rates 币种->USD，如 1 EUR = 1.08 USD；
    fx_rates=None 或涉及币种缺汇率时相关组合直接跳过，全无候选返回空表，
    由 UI 先引导用户刷新/配置汇率）。

    输出按净隔夜利息估计（USD/天）降序，最多返回 cap 条（默认 100）。
    """
    stocks = [s for s in stocks if parse_fx(s.get("symbol", ""))]
    if fx_rates is None:
        return []
    rates: dict[str, float] = dict(fx_rates)
    rates.setdefault("USD", 1.0)
    symbol_map = {s["symbol"]: s for s in stocks}
    edges = build_edges(stocks)
    opportunities: list[dict] = []

    def _build(legs: list[tuple[str, str]]) -> dict | None:
        per_leg_usd: list[float] = []
        for symbol, side in legs:
            info = symbol_map[symbol]
            swap_usd = swap_to_usd_per_day(
                info, side, rates, account_currency=account_currency
            )
            base, quote = parse_fx(symbol) or ("", "")
            if swap_usd is None:
                return None
            if base not in rates or quote not in rates:
                return None
            per_leg_usd.append(swap_usd)
        w = usd_neutral_weights(
            [{"symbol": sym, "side": side} for sym, side in legs], rates
        )
        if w is None:
            return None
        net_swap = float(sum(wi * s for wi, s in zip(w, per_leg_usd)))
        leg_desc = [("买" if side == "buy" else "卖", sym) for sym, side in legs]
        leg_meta = [
            {
                "symbol": sym,
                "side": side,
                "weight": float(w[i]),
                "swap": per_leg_usd[i],
            }
            for i, (sym, side) in enumerate(legs)
        ]
        return {
            "id": "+".join(f"{side[:1]}:{sym}" for sym, side in legs),
            "legs_count": len(legs),
            "legs_text": " + ".join(f"{s} {sym}" for s, sym in leg_desc),
            "legs": leg_meta,
            "net_swap": round(net_swap, 2),
            # 每腿明细（用于表格 tooltip）：买卖方向 + 权重（手数系数）
            # + 该腿隔夜息换算为 USD/天（1 标准手口径）
            "legs_detail": "\n".join(
                f"{'买' if m['side'] == 'buy' else '卖'} {m['symbol']} "
                f"×{m['weight']:.2f} → {m['swap']:+.2f} USD/天"
                for m in leg_meta
            ),
            "currencies": " ".join(sorted(
                {p for s, _ in legs for p in (parse_fx(s) or ())})),  # noqa: E501
        }

    for cycle in _find_cycles(edges, max_len=max_legs):
        legs = sides_for_cycle(cycle)
        if len(legs) < 2 or len(legs) > max_legs:
            continue
        if not is_neutral(legs, symbol_map):
            continue
        # 同一 symbol 不允许重复（等手数中性下重复腿无意义）
        symbols_in = [s for s, _ in legs]
        if len(set(symbols_in)) != len(symbols_in):
            continue

        # 方向 A 与反向 A（所有买卖翻转）：两个都是真实的候选交易。
        # 新过滤：涉及币种必须都在 fx_rates；各腿 swap_to_usd_per_day(side)
        # 非 None；usd_neutral_weights 非 None（不满足则整条组合跳过）。
        built = _build(legs)
        if built is not None:
            opportunities.append(built)
        flipped = [(sym, "sell" if side == "buy" else "buy") for sym, side in legs]
        built_flipped = _build(flipped)
        if built_flipped is not None:
            opportunities.append(built_flipped)

    opportunities.sort(key=lambda o: o["net_swap"], reverse=True)
    return opportunities[:cap]
