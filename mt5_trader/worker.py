"""每账户一个 MT5 worker；通过本地 JSON 消息处理行情、受管交易与自动任务。

请求由主循环分派，交易由订单线程串行执行；事件为 {type, payload} 字典。
旧的无归属 order/spread_algo 请求仅返回拒绝，不执行交易。
"""

from __future__ import annotations

import copy
import datetime as dt
import math
import queue as _queue
import threading
import time
from pathlib import Path
from typing import Any, TYPE_CHECKING, ContextManager

import MetaTrader5 as mt5

from .constants import (
    EVENT_AUTOMATION_STATE,
    EVENT_MANAGED_STATE,
    MAGIC_DEFAULT,
    EVENT_ACCOUNT,
    EVENT_DEAL,
    EVENT_LOG,
    EVENT_ORDER,
    EVENT_ORDER_RESULT,
    EVENT_POSITION,
    EVENT_RULES,
    EVENT_SPREAD_ALGO,
    EVENT_SPREAD_HISTORY,
    EVENT_STATUS,
    EVENT_SWAPS,
    EVENT_SYMBOLS,
    EVENT_TICK,
)
from .spreads import round_to_step
from .managed_trading import OrderNotSent

if TYPE_CHECKING:
    from .managed_trading import ManagedExecutor
    from .automated_trading import AutomationEngine


# UI 心跳应答事件名（与 account_session 的 ping/pong 心跳协议对应；
# 容忍其未进 constants.py：仅 worker↔UI 传输层使用，不触发其它副作用）。
_EVENT_PONG = "ePong"


def _f(v, default: float = 0.0) -> float:
    """payload 数值安全取值：None/非法 -> default（bool 视为非法）。"""
    try:
        if v is None or isinstance(v, bool):
            return default
        value = float(v)
        return value if math.isfinite(value) else default
    except (TypeError, ValueError):
        return default


class _Worker:
    TICK_INTERVAL = 0.2
    SNAPSHOT_INTERVAL = 1.0
    ERROR_COOLDOWN = 5.0
    LINK_FAIL_LIMIT = 3          # 连续 N 次 account_info 无数据 → 判定连接丢失
    RECONNECT_EVERY = 30.0       # 连接丢失后的重连尝试间隔（秒）

    def __init__(self, req_queue: Any, rep_queue: Any, config: dict) -> None:
        self._req = req_queue
        self._rep = rep_queue
        self._config = config
        self._name = str(config["login"])
        self._server = config.get("server", "?")

        self._stop_evt = threading.Event()
        self._connected = False
        # 不可变集合：订阅线程读、请求线程替换，无锁原子
        self._symbols: frozenset[str] = frozenset()
        self._last_error_at: dict[str, float] = {}
        self._swap_watch_thread: threading.Thread | None = None
        self._swap_watch_evt = threading.Event()
        # MetaTrader5 Python API 非线程安全：轮询/swap 监视/历史请求等线程
        # 必须串行访问，全部包在这把锁里（否则 IPC 崩溃导致子进程退出）
        self._mt5_lock = threading.RLock()
        self._swap_fingerprint: dict[str, tuple] = {}
        # fx_rates 快照的品种名缓存 {小写: 真实名}（首次 symbols_get 后懒加载）
        self._fx_symbol_cache: dict[str, str] | None = None
        # 连接丢失检测/自动重连状态（_poll_snapshot 维护）
        self._link_lost = False
        self._link_fail_n = 0
        self._last_reconnect = 0.0
        self._order_q: _queue.Queue = _queue.Queue(maxsize=128)
        # 价差历史后台线程：拉取/构造不再阻塞请求主循环（订单延迟）
        # latest-wins：进行中的拉取不被打断，pending 恒为最新请求的 legs 快照
        self._hist_lock = threading.Lock()
        self._hist_evt = threading.Event()
        self._hist_pending: dict | None = None
        self._hist_thread: threading.Thread | None = None
        self._managed: ManagedExecutor | None = None
        self._automation: AutomationEngine | None = None
        self._automation_lease: ContextManager | None = None
        self._automation_symbols: frozenset[str] = frozenset()
        from .worker_health import WorkerHealth
        self._health = WorkerHealth()
        from .broker_clock import BrokerClock
        self._clock = BrokerClock(config.get('broker_timezone', 'UTC'))
        # 静默市场低流动性腿可 5-10 分钟无新 tick，上限放宽到 600s；
        # 默认保持 60（与远程作者口径一致），用户在账户对话框按需加大。
        self._quote_max_age = float(config.get('quote_max_age', 60))
        if not math.isfinite(self._quote_max_age) or not 5 <= self._quote_max_age <= 600:
            raise ValueError('市场报价最大年龄必须为5至600秒')

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------
    def run(self) -> None:
        # Pure attach. A separate IPC reader can stop/ping during initialize.
        if not self._connect():
            with self._mt5_lock:
                mt5.shutdown()
            self._send(EVENT_STATUS, {"ok": False, "msg": "会话已停止"})
            return

        self._health.ready()
        poller = threading.Thread(target=self._poll_loop, daemon=True)
        poller.start()

        self._hist_thread = threading.Thread(
            target=self._history_loop, name="spread-history", daemon=True)
        self._hist_thread.start()

        self._order_thread = threading.Thread(
            target=self._order_loop, name="orders", daemon=True)
        self._order_thread.start()

        poll_fail = 0
        while not self._stop_evt.is_set():
            try:
                self._process_requests(0.2)
            except Exception as exc:
                self._push_error("req_loop", repr(exc))
                poll_fail += 1
                if poll_fail >= 6:
                    self._push_error(
                        "req_loop", f"请求主循环连续失败 {poll_fail} 次熔断退出")
                    self._stop_evt.set()
                    break
                continue
            poll_fail = 0

        self.request_stop()
        # An accepted algorithm executes its passive hedges on the order thread.
        # Do not shutdown the API between its legs. Parent enforces a hard timeout.
        self._order_thread.join()
        self._hist_thread.join()
        poller.join()
        if self._swap_watch_thread:
            self._swap_watch_thread.join()
        self._connected = False
        with self._mt5_lock:
            try:
                mt5.shutdown()
            except Exception:
                pass
        self._send(EVENT_STATUS, {"ok": False, "msg": "已断开"})

    # ------------------------------------------------------------------
    # 连接（含新终端/无 broker 缓存的登录兑底）
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # 连接（唯一路线：用户先在 MT5 终端里手动登录 → 账户绑定该终端 → attach）
    # ------------------------------------------------------------------
    def request_stop(self) -> None:
        """IPC reader may call this while the request loop is busy attaching."""
        self._stop_evt.set()
        self._swap_watch_evt.set()
        self._hist_evt.set()

    def _identity_matches(self, account) -> bool:
        return (account is not None
                and int(account.login) == int(self._config["login"])
                and str(account.server).strip() == str(self._server).strip())

    def _account_available(self, account) -> bool:
        terminal = mt5.terminal_info()
        return self._identity_matches(account) and terminal is not None and bool(terminal.connected)

    def _connect(self) -> bool:
        """连接流程（纯 attach，永不拉起终端、永不写凭据）。

        唯一支持的使用路线：
        1. 用户先在 MT5 客户端里手动登录目标账户；
        2. 账户管理里把账户绑定到该终端；
        3. 打开控制台 → worker attach 到已运行终端。

        worker 只做：initialize(path=绑定终端) → account_info 校验账号；
        账号不匹配则轮询等待（用户在终端里人工切换），永不超时，
        永不拉起/重启/写凭据操作终端。"""
        path = self._config.get("terminal_path")
        login = int(self._config["login"])
        server = self._server
        if not path:
            self._push_error(
                "config",
                f"账户 {login}@{server} 未绑定终端：请先在 MT5 客户端手动登录，"
                "再到账户管理里绑定该终端",
            )
            return False
        if not Path(path).exists():
            self._push_error("config", f"绑定的终端不存在: {path}")
            return False
        self._log(f"worker[{self._name}] attach 到终端: {path}")

        last_summary = 0.0
        while not self._stop_evt.is_set():
            self._health.mark('initialize')
            self._send(EVENT_STATUS, {'ok': False, 'msg': f'等待终端登录 {login}@{server}'})
            with self._mt5_lock:
                try:
                    # initialize may launch a terminal unless we verify it is running.
                    if not self._terminal_pids_for_path(path):
                        self._log("请先手动启动并登录绑定的 MT5 终端")
                        if not self._wait_stop(5):
                            return False
                        continue
                    ok = mt5.initialize(path=path, timeout=10000)
                except Exception as exc:
                    self._push_error("attach", repr(exc))
                    if not self._wait_stop(15):
                        return False
                    continue
                if not ok:
                    self._push_error("attach", str(mt5.last_error()))
                    try:
                        mt5.shutdown()
                    except Exception:
                        pass
                    if not self._wait_stop(10):
                        return False
                    continue
                acc = mt5.account_info()
                available = self._account_available(acc)
            if available:
                self._on_connected()
                return True
            now = time.time()
            if now - last_summary > 30:
                last_summary = now
                with self._mt5_lock:
                    if acc is not None:
                        self._log(
                            f"已 attach 到终端，但当前登录的是 {acc.login}@{acc.server}，"
                            f"请在终端里切换登录到 {login}@{server}"
                        )
                        try:
                            mt5.shutdown()
                        except Exception:
                            pass
                    else:
                        self._log(
                            f"已 attach 到终端但尚未登录，请在终端里手动登录 "
                            f"{login}@{server}"
                        )
                        # 尚未登录时保持 attach（不 shutdown），等待用户在终端内登录
            if not self._wait_stop(5):
                return False

        return False  # 仅 stop 时退出

    def _wait_stop(self, seconds: float) -> bool:
        """等待 seconds；期间收到 stop 返回 False（会话终止），否则 True。"""
        return not self._stop_evt.wait(seconds)

    @staticmethod
    def _terminal_pids_for_path(path: str) -> list[int]:
        """返回 exe 路径匹配给定路径的所有运行进程 pid（供关闭/重启判断）。"""
        import psutil

        want = (path or "").lower()
        pids: list[int] = []
        for p in psutil.process_iter(["pid", "exe"]):
            try:
                if (p.info.get("exe") or "").lower() == want:
                    pids.append(p.info["pid"])
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return pids

    # ------------------------------------------------------------------
    def _on_connected(self) -> None:
        self._connected = True

        for symbol in self._config.get("symbols", []):
            self._subscribe(symbol, silent=True)

        with self._mt5_lock:
            acc = mt5.account_info()
            ver = mt5.version()
        self._log(
            f"连接成功 | 终端 {ver} | {acc.login}@{acc.server} "
            f"杠杆 {acc.leverage} 净值 {acc.equity:.2f} | "
            f"初始订阅: {sorted(self._symbols)}"
        )
        self._send(EVENT_STATUS, {"ok": True, "msg": "已连接"})
        self._send(EVENT_SYMBOLS, sorted(self._symbols))

    # ------------------------------------------------------------------
    # 请求处理（主线程侧）
    # ------------------------------------------------------------------
    def _process_requests(self, timeout: float) -> None:
        try:
            msg = self._req.get(timeout=timeout)
        except _queue.Empty:
            return
        action = msg.get("action")

        if self._stop_evt.is_set():
            return
        if action == "subscriptions":
            wanted = {str(x).strip() for x in msg.get("symbols", []) if str(x).strip()}
            # Do not deselect Market Watch symbols belonging to the terminal user.
            self._symbols = frozenset()
            for symbol in wanted:
                self._subscribe(symbol)
            self._send(EVENT_SYMBOLS, sorted(self._symbols))
        elif action == "subscribe":
            self._subscribe(str(msg["symbol"]))
        elif action in ("managed_trade", "managed_snapshot", "automation"):
            self._queue_order(msg)
        elif action == "order":
            self._push_result(False, "无归属订单入口已禁用，请使用受管策略入口", msg.get("request", {}))
        elif action == "ping":
            # UI 心跳（与 account_session 心跳协议对应）：只回 pong，无其它副作用
            self._send(_EVENT_PONG, {"ts": time.time()})
        elif action == "deals":
            self._send_deals(int(msg.get("days", 7)))
        elif action == "rules":
            self._send_rules()
        elif action == "swap_watch":
            self._start_swap_watch(float(msg.get("interval", 60)))
        elif action == "swap_unwatch":
            self._stop_swap_watch()
        elif action == "spread_history":
            self._send_spread_history(msg)
        elif action == "spread_algo":
            self._push_error("spread_algo", "旧的无归属交易接口已禁用，请使用受管策略入口")
        elif action == "stop":
            self.request_stop()
        elif action is None:
            pass   # config 首条消息（worker_pipe 握手），忽略
        else:
            self._log(f"未知请求 action={action!r}")

    # ------------------------------------------------------------------
    # 受管订单与自动任务队列
    # ------------------------------------------------------------------
    def _queue_order(self, msg: dict) -> None:
        try:
            self._order_q.put_nowait(msg)
        except _queue.Full:
            if msg.get("action") == "automation":
                self._send(EVENT_AUTOMATION_STATE, {"request_id": msg.get("request_id"),
                    "error": "任务队列已满，未接受操作，请刷新后重试"})
                return
            if msg.get("action") in ("managed_trade", "managed_snapshot"):
                self._send(EVENT_MANAGED_STATE, {"ready": False, "request_id": msg.get("request_id"), "error": "订单队列已满，请核对后重试"})
                return
            self._send(EVENT_SPREAD_ALGO, {"algoid": "", "stage": "failed", "msg": "委托队列已满，请核对后重试"})

    def _send_rules(self) -> None:
        with self._mt5_lock:
            raw = mt5.symbols_get()
            if raw is None:
                self._push_error("symbols_get", str(mt5.last_error()))
                return
            items = []
            for s in raw:
                d = s._asdict()
                # SymbolInfo 叫 name，不叫 symbol；补齐供 GUI 表格/过滤使用
                d["symbol"] = s.name
                items.append(d)
        self._send(EVENT_RULES, {
            "items": items,
            "fetched_at": time.time(),
        })

    def _start_swap_watch(self, interval: float) -> None:
        self._stop_swap_watch()
        if self._swap_watch_thread:
            self._swap_watch_thread.join()
        cancel = threading.Event()
        self._swap_watch_evt = cancel
        interval = max(5.0, float(interval))

        def watch() -> None:
            while not cancel.is_set() and not self._stop_evt.is_set():
                try:
                    if self._connected and not self._link_lost:
                        self._swap_scan()
                except Exception as exc:
                    self._push_error("swap_watch", repr(exc))
                cancel.wait(interval)

        self._swap_watch_thread = threading.Thread(target=watch, name="swap-watch", daemon=True)
        self._swap_watch_thread.start()

    def _stop_swap_watch(self) -> None:
        self._swap_watch_evt.set()

    def _swap_scan(self) -> None:
        """全品种 swap 指纹比对，变化才推送（=> 自建更新时间）。"""
        with self._mt5_lock:
            raw = mt5.symbols_get()
        if raw is None:
            self._push_error("symbols_get", str(mt5.last_error()))
            return
        fetched_at_s = time.time()

        changed, all_items = [], []
        new_state: dict[str, tuple] = {}
        for s in raw:
            fp = (round(s.swap_long, 10), round(s.swap_short, 10), s.swap_rollover3days)
            new_state[s.name] = fp
            all_items.append({
                "symbol": s.name,
                "swap_long": s.swap_long,
                "swap_short": s.swap_short,
                "rollover3days": s.swap_rollover3days,
                "swap_mode": s.swap_mode,
                "currency_base": getattr(s, "currency_base", ""),
                "currency_profit": getattr(s, "currency_profit", ""),
                # 换算字段：供 GUI 侧 swap→USD/天 与 USD 中性权重计算
                "point": s.point,
                "tick_size": s.trade_tick_size,
                "tick_value": s.trade_tick_value,
                "contract_size": s.trade_contract_size,
                "trade_contract_size": s.trade_contract_size,
                "currency_margin": getattr(s, "currency_margin", ""),
                "currency_deposit": getattr(s, "currency_deposit", ""),
            })
            prev = self._swap_fingerprint.get(s.name)
            if prev != fp:
                changed.append({**all_items[-1], "first": prev is None})

        self._swap_fingerprint = new_state
        payload = {
            "fetched_at": dt.datetime.fromtimestamp(fetched_at_s, dt.timezone.utc).isoformat(),
            "epoch": fetched_at_s,
            "first": not bool(changed) and bool(all_items),
            "changed": changed,
            "all": all_items,
            "fx_rates": self._fx_rates_snapshot(),
        }
        self._send(EVENT_SWAPS, payload)

    def _fx_rates_snapshot(self) -> dict[str, float]:
        """币种→USD 汇率快照（供 GUI 侧 swap 换算/中性权重）。

        只用 7 大主流美元对的当前中间价（每轮仅 7 次 tick 调用，开销可控）；
        取不到的币种不入表（GUI 侧会把涉及这些币种的组合过滤）。
        品种名大小写/后缀无关解析（broker 如 Bitget 用 EURUSD.s 后缀）。"""
        with self._mt5_lock:
            # 首次构建 {小写名: 真实名} 缓存（含后缀）
            if self._fx_symbol_cache is None:
                self._fx_symbol_cache = {
                    s.name.lower(): s.name for s in (mt5.symbols_get() or [])
                }
            cache = self._fx_symbol_cache

            def resolve(sym: str) -> str | None:
                if mt5.symbol_info(sym) is not None:
                    return sym
                low = sym.lower()
                if low in cache:
                    return cache[low]
                # 后缀匹配: eurusd → eurusd.s（取同前缀中字母序最小的一个）
                hits = sorted(name for k, name in cache.items()
                              if k.startswith(low + "."))
                return hits[0] if hits else None

            rates: dict[str, float] = {"USD": 1.0}
            # XXXUSD 直接对（EURUSD.s 等）：1 单位币种 = 中间价 USD
            for cur, sym in (("EUR", "EURUSD"), ("GBP", "GBPUSD"),
                             ("AUD", "AUDUSD"), ("NZD", "NZDUSD")):
                real = resolve(sym)
                if real is None:
                    continue
                tick = mt5.symbol_info_tick(real)
                if tick is not None and tick.bid > 0 and tick.ask > 0:
                    rates[cur] = (tick.bid + tick.ask) / 2
            # USDBBB 型（USDCAD.s 等）：1 USD = 中间价 BBB
            for cur, sym in (("CAD", "USDCAD"), ("CHF", "USDCHF"), ("JPY", "USDJPY")):
                real = resolve(sym)
                if real is None:
                    continue
                tick = mt5.symbol_info_tick(real)
                if tick is not None and tick.bid > 0 and tick.ask > 0:
                    mid = (tick.bid + tick.ask) / 2
                    rates[cur] = 1.0 / mid
        return rates

    # ------------------------------------------------------------------
    # 价差历史（多腿对齐）
    # ------------------------------------------------------------------
    def _spread_adj(self, symbol: str) -> float:
        """当前点差（价格维度）：spread_pts × point，作为该腿 bid→ask 的近似常量偏移。"""
        try:
            with self._mt5_lock:
                info = mt5.symbol_info(symbol)
            if info is not None and info.spread > 0 and info.point > 0:
                return float(info.spread) * float(info.point)
        except Exception:
            pass
        return 0.0

    def _send_spread_history(self, msg: dict) -> None:
        """历史价差（乘法合成，用户公式），转发给后台拉取线程。

        请求本身只做入队：M1(14400)+H1(9600) 的 copy_rates 拉取耗时 1~3s
        （全程持 _mt5_lock），同步执行会阻塞请求主循环使 order 请求排队数秒。
        latest-wins：进行中的拉取不被打断，pending 恒为最新请求的快照。"""
        legs = msg.get("legs", [])
        if not legs:
            self._push_error("spread_history", "legs 为空")
            return
        if len({leg["symbol"] for leg in legs}) != len(legs):
            self._push_error("spread_history", "legs 含重复 symbol")
            return
        with self._hist_lock:
            self._hist_pending = copy.deepcopy(msg)
        self._hist_evt.set()
        self._log("历史拉取已在后台排队")

    def _history_loop(self) -> None:
        """价差历史专用后台线程：循环取 latest-wins 的 pending legs 执行拉取。

        与旧同步执行完全复用 _build_spread_dataset（内部持 _mt5_lock，
        与轮询/交易线程互斥，保持现状）；rep_queue 线程安全，可直接 _send。
        重复请求不丢最终数据（处理完 pending 后循环等下一个）；stop 退出。"""
        while True:
            self._hist_evt.wait()
            if self._stop_evt.is_set():
                return
            self._hist_evt.clear()
            while True:   # 处理 pending 直到为空（期间新请求持续覆盖）
                with self._hist_lock:
                    request = self._hist_pending
                    self._hist_pending = None
                if request is None:
                    break
                try:
                    self._fetch_spread_history(request["legs"], request.get("request_id", ""))
                except Exception as exc:
                    self._send(EVENT_SPREAD_HISTORY, {"ok": False, "error": str(exc),
                               "legs": request["legs"], "request_id": request.get("request_id", "")})
                if self._stop_evt.is_set():
                    return
            # pending 空且 evt 可能在 clear 前被再 set：循环回去 wait 确认

    def _fetch_spread_history(self, legs: list, request_id: str = "") -> None:
        """（后台线程）执行原 _send_spread_history 的拉取与构造。

        旧版实现文档：合成买 USDJPY(Ask) = USDCHF Ask × CHFJPY Ask，
        价差 = 实际卖出 Bid − 合成买入 Ask；目标腿 = 卖出方向 |权重| 最大，
        其余腿在 log 空间按带符号权重合成（w>0 取 ask 侧、w<0 取 bid 侧）。"""
        # 目标腿：卖出方向（无则 |权重| 最大）
        tp = None
        tp_candidates = [
            (leg, abs(float(leg.get("weight", 1.0))))
            for leg in legs
            if leg.get("side", "buy") == "sell"
        ]
        if tp_candidates:
            tp = max(tp_candidates, key=lambda kv: kv[1])[0]
        else:
            tp = max(legs, key=lambda lg: lg.get("weight", 1.0))
        target_sym = tp["symbol"]
        target_w = float(tp.get("weight", 1.0))
        if tp.get("side", "buy") == "sell":
            target_w = -target_w

        datasets: dict[str, dict] = {}
        # 图与实时信号口径 = M1 折线（用户要求，断开最少）；
        # H1（≥1 年）仅供统计先验/长窗极值参考。
        # 蜡烛已退役（GUI 折线方案）：不再合成 _candles，省 ~40% 拉取时间与带宽。
        for tf_name, tf, count in (
            ("h1", mt5.TIMEFRAME_H1, 9600),
            ("m1", mt5.TIMEFRAME_M1, 14400),   # ≈10 个交易日 M1
        ):
            line = self._build_spread_dataset(legs, target_sym, tf_name, tf, count)
            if line is None:
                raise ValueError(f"{tf_name} 历史数据不可用")
            datasets[tf_name] = line

        self._send(EVENT_SPREAD_HISTORY, {
            "ok": True, "request_id": request_id,
            "datasets": datasets,
            "legs": legs,
            "fetched_at": time.time(),
        })

    # ------------------------------------------------------------------
    # 订单/算法线程（请求主循环只入队，串行发单保持顺序化语义）
    # ------------------------------------------------------------------
    def _order_loop(self) -> None:
        try:
            self._order_service()
        finally:
            if self._automation_lease is not None:
                self._automation_lease.__exit__(None, None, None)
                self._automation_lease = None

    def _order_service(self) -> None:
        """串行执行受管订单与自动任务，停止时取消尚未执行的排队请求。"""
        last_publish = 0.
        try:
            self._ensure_automation()
        except Exception as exc:
            self._send(EVENT_AUTOMATION_STATE, {"error": str(exc), "running": False})
        while True:
            try:
                msg = self._order_q.get(timeout=0.05)
            except _queue.Empty:
                if self._stop_evt.is_set():
                    return
                msg = {}
            if self._stop_evt.is_set():
                self._push_error("order_loop", "退出时取消尚未执行的委托")
                return
            try:
                action = msg.get("action")
                if action == "automation":
                    self._handle_automation(msg)
                elif action in ("managed_trade", "managed_snapshot"):
                    self._handle_managed(msg)
                elif action == "order":
                    self._push_result(False, "无归属订单入口已禁用", msg.get("request", {}))
                elif action == "spread_algo":
                    self._push_error("spread_algo", "旧的无归属算法接口已禁用")
                # No stale per-tick trading jobs are queued. Each pass evaluates
                # latest conditions and dispatches at most one basket.
                if self._automation is not None:
                    if self._order_q.empty():
                        self._automation.pulse()  # Apply queued human controls first.
                    if time.monotonic() - last_publish >= 1:
                        self._send(EVENT_AUTOMATION_STATE, self._automation.view())
                        last_publish = time.monotonic()
            except Exception as exc:
                self._push_error("order_loop", repr(exc))
                self._send(EVENT_SPREAD_ALGO, {"algoid": "", "stage": "failed",
                           "msg": f"委托执行异常，结果需人工核对: {exc}"})
            finally:
                self._health.mark('orders')

    def _ensure_managed(self):
        from .managed_trading import ExecutionLedger, ManagedExecutor
        if self._managed is None:
            path = self._config.get("execution_db")
            if not path:
                raise ValueError("没有配置持久交易账本，拒绝实盘")
            ledger = ExecutionLedger(path, f"{self._name}@{self._server}")
            self._managed = ManagedExecutor(self, mt5, ledger)
        return self._managed

    def _ensure_automation(self):
        from .automated_trading import AutomationEngine
        if self._automation is None:
            executor = self._ensure_managed()
            lease = executor.ledger.exclusive()
            lease.__enter__()
            try:
                self._automation = AutomationEngine(executor)
            except Exception:
                lease.__exit__(None, None, None)
                raise
            # Acquired/released on the order thread. Prevent a second worker
            # using this ledger from running stale in-memory task switches.
            self._automation_lease = lease
            with self._mt5_lock:
                for symbol in self._automation.symbols():
                    mt5.symbol_select(symbol, True)
            self._automation_symbols = self._automation.symbols()

    def _handle_automation(self, message):
        try:
            self._ensure_automation()
            assert self._automation is not None
            self._automation.command(message)
            self._automation_symbols = self._automation.symbols()
            self._send(EVENT_AUTOMATION_STATE, {**self._automation.view(),
                'request_id': message.get('request_id')})
        except Exception as exc:
            state = self._automation.view() if self._automation is not None else {}
            self._send(EVENT_AUTOMATION_STATE, {**state, 'request_id': message.get('request_id'),
                                               'error': str(exc)})

    def _handle_managed(self, message: dict) -> None:
        request_id = message.get("request_id")
        try:
            managed = self._ensure_managed()
            if str(message.get('strategy_id', '')).startswith('auto:') and message['action'] == 'managed_trade':
                raise ValueError('后台任务归属只允许通过任务执行器操作')
            if message["action"] == "managed_trade":
                if message.get('operation') == 'open' and self._automation is not None:
                    limits = self._automation.account_limits(managed.snapshot())
                    message = {**message, **{key: min(float(message.get(key, 0)), value)
                                            for key, value in limits.items()}}
                reply = managed.execute(message)
                state = reply["state"]
            else:
                state = managed.snapshot()
            self._send(EVENT_MANAGED_STATE, {**state, "request_id": request_id})
        except Exception as exc:
            state = {"ready": False, "batches": [], "blocked": True}
            if self._managed is not None:
                try:
                    state = self._managed.snapshot()
                except Exception:
                    pass  # Preserve the original failure in the mandatory reply.
            self._send(EVENT_MANAGED_STATE, {**state, "request_id": request_id, "error": str(exc)})
            self._push_error("managed_trade", str(exc))
        finally:
            if self._automation is not None:
                self._automation._state_at = 0.

    def _build_spread_dataset(self, legs, target_sym, tf_name, tf, count) -> dict | None:
        """单周期的乘法合成价差**折线**序列（用户公式）。

        只在 legs 各自同时存在该时间戳时才出点（交集），
        M1 交集缺口远小于 H1（周末/结市更短），缺口更少、图更连贯。
        蜡烛已退役：只返回 {"t": [...], "v": [...]}。"""
        leg_ohlc: dict[str, dict[str, list]] = {}
        for leg in legs:
            with self._mt5_lock:
                rates = mt5.copy_rates_from_pos(leg["symbol"], tf, 0, count)
                error = mt5.last_error() if rates is None or len(rates) == 0 else None
            if rates is None or len(rates) == 0:
                self._push_error("copy_rates", f"{leg['symbol']} {tf_name}: {error}")
                return None
            # CPU conversion of history must not hold the live-order API lock.
            ohlc = leg_ohlc[leg["symbol"]] = {k: [] for k in ("t", "o", "h", "l", "c")}
            for r in rates:
                try:
                    stamp = self._clock.to_utc(r['time'])
                except ValueError:
                    continue  # DST-ambiguous/nonexistent bars have no unique UTC time.
                ohlc["t"].append(stamp)
                ohlc["o"].append(float(r["open"]))
                ohlc["h"].append(float(r["high"]))
                ohlc["l"].append(float(r["low"]))
                ohlc["c"].append(float(r["close"]))
        ohlc_parts = [leg_ohlc[leg["symbol"]] for leg in legs]
        common_t = set(ohlc_parts[0]["t"])
        for part in ohlc_parts[1:]:
            common_t &= set(part["t"])
        common_t = sorted(common_t)
        idx_by_sym = [
            {t: i for i, t in enumerate(part["t"])} for part in ohlc_parts
        ]
        leg_adj = [self._spread_adj(leg["symbol"]) for leg in legs]
        comp = [   # (i_part, 幂(±1 按方向), adj) 其余腿（非目标）
            (i, (1 if leg.get("side", "buy") == "buy" else -1), leg_adj[i])
            for i, leg in enumerate(legs) if leg["symbol"] != target_sym
        ]
        tp_idx = next(
            i for i, leg in enumerate(legs) if leg["symbol"] == target_sym
        )
        tp_part = ohlc_parts[tp_idx]

        target_sign = 1.0 if next(leg for leg in legs if leg["symbol"] == target_sym).get("side", "buy") == "buy" else -1.0
        target_adjustment = self._spread_adj(target_sym) if target_sign > 0 else 0.0

        def _synth(vals: list[float]) -> float:
            """vals[i] = 该腿当前限价侧价（含点差），按 ±1 幂乘法合成。"""
            s = 0.0
            for (_, pw, _adj), v in zip(comp, vals):
                if v <= 0:
                    return float("nan")
                s += (-pw / target_sign) * math.log(v)
            try:
                return math.exp(s)
            except OverflowError:
                return float("nan")

        t_out, v_out = [], []
        for t in common_t:
            ti = idx_by_sym[tp_idx][t]
            t_bid_c = tp_part["c"][ti] + target_adjustment
            close_side = []
            for (pi, pw, adj) in comp:
                i = idx_by_sym[pi][t]
                bid = ohlc_parts[pi]["c"][i]
                close_side.append(bid + adj if pw > 0 else bid)
            spread_c = t_bid_c - _synth(close_side)
            if math.isfinite(spread_c):
                v_out.append(spread_c)
                t_out.append(t)
        return {"t": t_out, "v": v_out}

    def _resolve_symbol(self, symbol: str) -> str | None:
        """大小写无关地把输入解析到终端内真实存在的品种名。
        broker 常见后缀小写（如 EURUSD.s）——之前整体 .upper() 会把
        EURAUD.s 错误变成 EURAUD.S 导致订阅失败。"""
        with self._mt5_lock:
            for cand in (symbol, symbol.upper()):
                if cand and mt5.symbol_info(cand) is not None:
                    return cand
            for s in (mt5.symbols_get() or []):
                if s.name.lower() == symbol.lower():
                    return s.name
        return None

    def _subscribe(self, symbol: str, silent: bool = False) -> None:
        symbol = symbol.strip()
        if not symbol:
            return
        canonical = self._resolve_symbol(symbol)
        if canonical is None:
            self._push_error("symbol_select", f"{symbol}: 该账户/终端无此品种")
            return
        if canonical in self._symbols:
            self._send(EVENT_SYMBOLS, sorted(self._symbols))
            return
        if not self._connected:
            self._push_error("subscribe", f"{canonical}: 未连接，忽略")
            return
        with self._mt5_lock:
            ok = mt5.symbol_select(canonical, True)
            err = mt5.last_error()
        if not ok:
            self._push_error("symbol_select", f"{canonical}: {err}")
            return
        self._symbols = self._symbols | {canonical}
        if not silent:
            note = "" if canonical == symbol else f"（原输入: {symbol}）"
            self._log(f"已订阅行情: {canonical}{note}")
        self._send(EVENT_SYMBOLS, sorted(self._symbols))

    def _send_deals(self, days: int) -> None:
        date_to = time.time()
        with self._mt5_lock:
            deals = mt5.history_deals_get(date_to - days * 86400, date_to)
        if deals is None:
            self._push_error("history_deals_get", str(mt5.last_error()))
            return
        self._send(EVENT_DEAL, [d._asdict() for d in deals])

    # ------------------------------------------------------------------
    # 订单构造（直接拼 mt5 request）
    # ------------------------------------------------------------------
    @staticmethod
    def _not_sent(request, reason):
        if request.get("_managed"):
            raise OrderNotSent(reason)
        return None

    def _build_market(self, req: dict):
        if req.get("side", "buy") not in ("buy", "sell"):
            return self._not_sent(req, "本地委托校验失败，未调用broker")
        with self._mt5_lock:
            info = mt5.symbol_info(req["symbol"])
        volume = _f(req.get("volume"))
        if (info is None or not info.volume_min <= volume <= info.volume_max
                or info.volume_step <= 0
                or abs(volume - round_to_step(volume, info.volume_step)) > 1e-8):
            return self._not_sent(req, "本地委托校验失败，未调用broker")
        tick = self._tick(req["symbol"])
        if tick is None:
            return self._not_sent(req, "本地委托校验失败，未调用broker")
        side = mt5.ORDER_TYPE_BUY if req.get("side", "buy") == "buy" else mt5.ORDER_TYPE_SELL
        with self._mt5_lock:
            if not self._account_available(mt5.account_info()):
                self._push_error("order_identity", "账户身份不匹配或终端断线，拒绝发单")
                return self._not_sent(req, "本地委托校验失败，未调用broker")
            return mt5.order_send({
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": req["symbol"],
                "volume": float(req["volume"]),
                "type": side,
                "price": tick["ask"] if side == mt5.ORDER_TYPE_BUY else tick["bid"],
                "sl": float(req.get("sl") or 0.0),
                "tp": float(req.get("tp") or 0.0),
                "deviation": int(req.get("deviation", 20)),
                "magic": int(req.get("magic", MAGIC_DEFAULT)),
                "comment": str(req.get("comment", "light-trader"))[:31],
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": int(req.get("type_filling", mt5.ORDER_FILLING_IOC)),
            })

    def _build_close(self, req: dict):
        with self._mt5_lock:
            positions = mt5.positions_get(ticket=int(req["ticket"]))
        volume = _f(req.get("volume"))
        if not positions or len(positions) != 1:
            return self._not_sent(req, "本地委托校验失败，未调用broker")
        position = positions[0]
        if (position.symbol != req["symbol"]
                or (req.get("identifier") is not None and int(position.identifier) != int(req["identifier"]))
                or volume <= 0 or volume > position.volume
                or req.get("side") != ("buy" if position.type == 0 else "sell")):
            return self._not_sent(req, "本地委托校验失败，未调用broker")
        tick = self._tick(req["symbol"])
        if tick is None:
            return self._not_sent(req, "本地委托校验失败，未调用broker")
        # 持仓为多(POSITION_TYPE_BUY=0) -> 反向卖出
        close_side = (
            mt5.ORDER_TYPE_SELL if req.get("side", "buy") == "buy" else mt5.ORDER_TYPE_BUY
        )
        with self._mt5_lock:
            if not self._account_available(mt5.account_info()):
                self._push_error("order_identity", "账户身份不匹配或终端断线，拒绝发单")
                return self._not_sent(req, "本地委托校验失败，未调用broker")
            return mt5.order_send({
                "action": mt5.TRADE_ACTION_DEAL,
                "position": int(req["ticket"]),
                "symbol": req["symbol"],
                "volume": float(req["volume"]),
                "type": close_side,
                "price": tick["ask"] if close_side == mt5.ORDER_TYPE_BUY else tick["bid"],
                "deviation": int(req.get("deviation", 20)),
                "magic": int(req.get("magic", MAGIC_DEFAULT)),
                "comment": str(req.get("comment", "close"))[:31],
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": int(req.get("type_filling", mt5.ORDER_FILLING_IOC)),
            })

    def _tick(self, symbol: str) -> dict | None:
        with self._mt5_lock:
            raw = mt5.symbol_info_tick(symbol)
        if raw is None:
            self._push_error("symbol_info_tick", f"{symbol}: {mt5.last_error()}")
            return None
        try:
            result = self._clock.tick(raw._asdict())
        except (ValueError, TypeError, OverflowError) as exc:
            self._push_error('quote_clock', f'{symbol}: {exc}')
            return None
        if (not all(math.isfinite(float(result.get(k, 0))) for k in ("bid", "ask", "time"))
                or result["bid"] <= 0 or result["ask"] < result["bid"]
                or not -2 <= time.time() - result["time_msc"] / 1000 <= self._quote_max_age):
            return None
        return result

    # ------------------------------------------------------------------
    # 轮询
    # ------------------------------------------------------------------
    def _poll_loop(self) -> None:
        last_snapshot = 0.0
        while not self._stop_evt.is_set():
            try:
                if not self._link_lost:
                    self._poll_ticks()
                now = time.monotonic()
                if now - last_snapshot >= self.SNAPSHOT_INTERVAL:
                    self._poll_snapshot()
                    last_snapshot = now
            except Exception as exc:
                self._push_error("poll", repr(exc))
            finally:
                self._health.mark('poll')
            self._stop_evt.wait(self.TICK_INTERVAL)

    def _poll_ticks(self) -> None:
        items = []
        with self._mt5_lock:
            for symbol in self._symbols | self._automation_symbols:
                raw = mt5.symbol_info_tick(symbol)
                if raw is None:
                    self._push_error(f"tick({symbol})", str(mt5.last_error()))
                    continue
                try:
                    d = self._clock.tick(raw._asdict())
                except (ValueError, TypeError, OverflowError) as exc:
                    self._push_error('quote_clock', f'{symbol}: {exc}')
                    continue
                items.append({
                    "symbol": symbol,
                    "bid": d["bid"],
                    "ask": d["ask"],
                    "time": d["time"],
                    "time_msc": d.get("time_msc", d["time"] * 1000),
                    'received_at': d['received_at'],
                    'quote_max_age': self._quote_max_age,
                })
        if items:
            self._send(EVENT_TICK, items)

    def _poll_snapshot(self) -> None:
        with self._mt5_lock:
            acc = mt5.account_info()
            if self._account_available(acc):
                self._link_fail_n = 0
                self._send(EVENT_ACCOUNT, acc._asdict())
                if self._link_lost:
                    self._link_lost = False
                    self._link_fail_n = 0
                    self._last_reconnect = 0.0
                    self._log("接口已恢复，自动重连成功")
                    self._send(EVENT_STATUS, {"ok": True, "msg": "已连接"})
            else:
                self._link_fail_n += 1
                if acc is not None and not self._identity_matches(acc):
                    self._link_fail_n = self.LINK_FAIL_LIMIT
                if not self._link_lost and self._link_fail_n >= self.LINK_FAIL_LIMIT:
                    self._link_lost = True
                    self._log(f"接口连续 {self._link_fail_n} 次无数据，判定连接丢失；进入自动重连")
                    self._send(EVENT_STATUS, {"ok": False, "msg": "连接丢失，自动重连中"})
                self._push_error("account_info", str(mt5.last_error()))

            if not self._account_available(acc):
                positions, orders = None, None
            else:
                positions = mt5.positions_get()
                orders = mt5.orders_get()
            if positions is not None:
                self._send(EVENT_POSITION, [p._asdict() for p in positions])
            if positions is None:
                self._push_error("positions_get", str(mt5.last_error()))

            if orders is not None:
                self._send(EVENT_ORDER, [o._asdict() for o in orders])
            if orders is None:
                self._push_error("orders_get", str(mt5.last_error()))

        # 连接丢失后的自动重连：每 RECONNECT_EVERY 秒尝试一次 initialize（带锁）
        if self._link_lost and time.monotonic() - self._last_reconnect >= self.RECONNECT_EVERY:
            self._last_reconnect = time.monotonic()
            path = self._config.get("terminal_path")
            self._log("重连尝试: mt5.initialize …")
            with self._mt5_lock:
                try:
                    ok = (mt5.initialize(path=path, timeout=10000)
                          if path and self._terminal_pids_for_path(path) else False)
                    if ok:
                        acc = mt5.account_info()
                    else:
                        acc = None
                except Exception as exc:
                    ok, acc = False, None
                    self._push_error("reconnect", repr(exc))
            with self._mt5_lock:
                available = ok and self._account_available(acc)
            if available:
                self._link_lost = False
                self._link_fail_n = 0
                self._log(f"已重连 {acc.login}@{acc.server}")
                self._send(EVENT_STATUS, {"ok": True, "msg": "已连接"})
            else:
                self._log(f"重连后仍无数据 ({mt5.last_error()})，{self.RECONNECT_EVERY:.0f}s 后再试")

    # ------------------------------------------------------------------
    # 输出
    # ------------------------------------------------------------------
    def _send(self, event_type: str, payload: Any) -> None:
        if event_type in (EVENT_STATUS, EVENT_ACCOUNT) and isinstance(payload, dict):
            payload = {**payload, **self._clock.metadata()}
        self._rep.put({"type": event_type, "payload": payload})

    def _log(self, msg: str) -> None:
        self._send(EVENT_LOG, {"source": self._name, "msg": msg, "level": "INFO"})

    def _push_error(self, context: str, msg: str) -> None:
        now = time.monotonic()
        if now - self._last_error_at.get(context, 0.0) < self.ERROR_COOLDOWN:
            return
        self._last_error_at[context] = now
        self._send(
            EVENT_LOG,
            {"source": f"MT5/{context}", "msg": msg, "level": "ERROR"},
        )

    def _push_result(self, ok: bool, msg: str, request: dict, result: dict | None = None) -> None:
        self._send(EVENT_ORDER_RESULT, {"ok": ok, "msg": msg, "request": request, "result": result})
        self._send(
            EVENT_LOG,
            {"source": "订单", "msg": msg, "level": "INFO" if ok else "ERROR"},
        )
