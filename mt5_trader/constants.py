"""事件类型常量与 MT5 枚举的显示映射。

架构：每个 MT5 账户运行在独立子进程（MetaTrader5 模块是进程级单例，
一进程只能连一个账户），主 GUI 进程通过管道队列聚合所有账户事件。

事件信封（Event.data）统一为：
    {"session": <账户标识 login@server>, "payload": <负载>}

负载类型：
    EVENT_TICK      payload = list[dict]  每项 {symbol, bid, ask, time}
    EVENT_ACCOUNT   payload = dict        account_info._asdict()
    EVENT_POSITION  payload = list[dict]
    EVENT_ORDER     payload = list[dict]
    EVENT_DEAL      payload = list[dict]
    EVENT_SYMBOLS   payload = list[str]   当前会话已订阅品种（非全量！）
    EVENT_STATUS    payload = dict {ok, msg}
    EVENT_LOG       payload = dict {source, msg, level}
    EVENT_ORDER_RESULT payload = dict {ok, msg, request}
    EVENT_RULES     payload = {items: list[dict], fetched_at: str}
    EVENT_SWAPS     payload = {fetched_at: str, changed: list[dict], all: list[dict], first: bool}
    EVENT_SPREAD_HISTORY payload = {datasets: {m1/h1: {t, v}}, legs: list}

GUI -> Session 请求（data 内必须带 session 字段定位账户）：
    EVENT_SYMBOL_SUBSCRIBE {symbol}
    EVENT_ORDER_REQUEST    {kind: market/close/cancel, ...}
    EVENT_DEAL_REQUEST     {days}
    EVENT_ALGO_REQUEST     {legs: [{symbol,side,volume}], comment}
以及 worker 直读 action 的扩展请求：
    rules / swap_watch(interval) / swap_unwatch / spread_history(legs) / spread_algo
Session -> GUI（价差算法执行进度）
    EVENT_SPREAD_ALGO payload = {algoid, stage: active|hedge|done|failed|stopped, msg}
"""

# ---------------------------------------------------------------------------
# 事件类型
# ---------------------------------------------------------------------------
EVENT_STATUS = "eSessionStatus"
EVENT_LOG = "eLog"
EVENT_TICK = "eTick"
EVENT_ACCOUNT = "eAccount"
EVENT_POSITION = "ePosition"
EVENT_ORDER = "eOrder"
EVENT_DEAL = "eDeal"
EVENT_SYMBOLS = "eSymbols"

# GUI -> Session
EVENT_SYMBOL_SUBSCRIBE = "eSymbolSubscribe"
EVENT_ORDER_REQUEST = "eOrderRequest"
EVENT_DEAL_REQUEST = "eDealRequest"
EVENT_ALGO_REQUEST = "eAlgoRequest"

# Session -> GUI（价差算法执行进度）
EVENT_SPREAD_ALGO = "eSpreadAlgo"

# Session -> GUI
EVENT_ORDER_RESULT = "eOrderResult"

# 大数据载荷（按需请求）
EVENT_RULES = "eRules"                  # {items: list[dict], fetched_at}
EVENT_SWAPS = "eSwaps"                  # {fetched_at, changed, all, first}
EVENT_SPREAD_HISTORY = "eSpreadHistory"  # {datasets: {m1/h1: {t, v}}, legs}

# ---------------------------------------------------------------------------
# MT5 数值枚举（镜像 MQL5/MetaTrader5 模块取值，GUI 进程无需 import mt5）
# ---------------------------------------------------------------------------
POSITION_TYPE_BUY = 0
POSITION_TYPE_SELL = 1

ORDER_TYPE_BUY = 0
ORDER_TYPE_SELL = 1
ORDER_TYPE_BUY_LIMIT = 2
ORDER_TYPE_SELL_LIMIT = 3
ORDER_TYPE_BUY_STOP = 4
ORDER_TYPE_SELL_STOP = 5
ORDER_TYPE_BUY_STOP_LIMIT = 6
ORDER_TYPE_SELL_STOP_LIMIT = 7

DEAL_TYPE_BUY = 0
DEAL_TYPE_SELL = 1

TRADE_RETCODE_DONE = 10009  # 订单/操作成功完成

POSITION_TYPE_NAME = {
    POSITION_TYPE_BUY: "多",
    POSITION_TYPE_SELL: "空",
}

ORDER_TYPE_NAME = {
    ORDER_TYPE_BUY: "买入",
    ORDER_TYPE_SELL: "卖出",
    ORDER_TYPE_BUY_LIMIT: "限价买",
    ORDER_TYPE_SELL_LIMIT: "限价卖",
    ORDER_TYPE_BUY_STOP: "停损买",
    ORDER_TYPE_SELL_STOP: "停损卖",
    ORDER_TYPE_BUY_STOP_LIMIT: "停限买",
    ORDER_TYPE_SELL_STOP_LIMIT: "停限卖",
}

DEAL_TYPE_NAME = {
    DEAL_TYPE_BUY: "买",
    DEAL_TYPE_SELL: "卖",
}

# ---------------------------------------------------------------------------
# SymbolInfo 枚举镜像（交易规则面板显示用）
# ---------------------------------------------------------------------------
SWAP_MODE_NAME = {
    0: "无",                    1: "按点",
    2: "入金货币(账户)",           3: "品种利润货币",
    4: "保证金货币",
    5: "当前价年利率",             6: "开仓价年利率",
    7: "当日重开(按7)",            8: "次日重开(按8)",
}

TRADE_MODE_NAME = {
    0: "禁用", 1: "仅允许买入", 2: "仅允许卖出", 3: "仅允许平仓", 4: "完全交易",
}

CALC_MODE_NAME = {
    0: "外汇", 1: "外汇无杠杆", 2: "期货", 3: "CFD",
    4: "CFD杠杆", 5: "撮合证券", 6: "证券期货", 7: "撮合期权",
    8: "撮合期权保证金", 9: "债券", 10: "交易所股票 (MOEX)",
    11: "交易所债券 (MOEX)", 12: "交易所期货 (MOEX)", 13: "交易所 (MOEX)",
    14: "加密货币", 15: "加密货币杠杆", 16: "交易所保证金",
}

EXECUTION_MODE_NAME = {
    0: "请求", 1: "即时", 2: "市场", 3: "交易所",
}

WEEKDAY_NAME = {
    0: "周日", 1: "周一", 2: "周二", 3: "周三", 4: "周四", 5: "周五", 6: "周六",
}

def filling_mode_bits(flags: int) -> str:
    """SymbolInfo.filling_mode 为掩码：FOK=1, IOC=2 (7:FOK+IOC+RETURN)"""
    if not flags:
        return "-"
    parts = []
    if flags & 1:
        parts.append("FOK")
    if flags & 2:
        parts.append("IOC")
    if flags & 4:
        parts.append("RETURN")
    return "|".join(parts)

def order_mode_bits(flags: int) -> str:
    if not flags:
        return "-"
    table = [(1, "市价"), (2, "限价"), (4, "停损"), (8, "停损限价"),
             (16, "SL"), (32, "TP"), (64, "对称平仓")]
    return "|".join(t for b, t in table if flags & b) or str(flags)

def expiration_mode_bits(flags: int) -> str:
    if not flags:
        return "-"
    table = [(1, "GTC"), (2, "当日"), (4, "指定时间"), (8, "指定日")]
    return "|".join(t for b, t in table if flags & b) or str(flags)
