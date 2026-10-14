"""子进程 worker：每个 MT5 账户一个进程，直接调用 mt5.xxx()。

与主进程的协议（multiprocessing.Queue，全部可 JSON 序列化的 dict）：

请求（主 -> 子）：
    {"action": "subscribe", "symbol": str}
    {"action": "order",     "request": dict}   # kind: market/close/cancel
    {"action": "deals",     "days": int}
    {"action": "stop"}

事件（子 -> 主）：
    {"type": EVENT_XXX, "payload": ...}        # MT5 namedtuples 已转 dict
"""

from __future__ import annotations

import math
import queue as _queue
import threading
import time
from pathlib import Path
from typing import Any

import MetaTrader5 as mt5

from .constants import (
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
    TRADE_RETCODE_DONE,
)
from .spreads import round_to_step

MAGIC_DEFAULT = 260100


def _f(v, default: float = 0.0) -> float:
    """payload 数值安全取值：None/非法 -> default（bool 视为非法）。"""
    try:
        if v is None or isinstance(v, bool):
            return default
        return float(v)
    except (TypeError, ValueError):
        return default


class _SpreadTaker:
    """价差算法执行器（模式引 vnpy SpreaderMaker/TakerAlgo，IOC 市价简化）：

    顺序执行：主动腿（先吃单锁定价差）→ 成交后立即对冲被动腿（腿腿 IOC）。
    任一腿被拒 → stage=failed 并大声报错（裸腿暴露风险），不再自动重试。
    全部成交 → stage=done，由调用方移除。
    """

    def __init__(
        self,
        algoid: str,
        legs: dict[str, float],       # {symbol: 带符号目标手数}
        comment: str = "",
    ) -> None:
        self.algoid = algoid
        self.targets = dict(legs)     # 带符号（购买为正）
        self.comment = comment
        self.done: dict[str, float] = {sym: 0.0 for sym in legs}
        self.active = max(legs, key=lambda s: abs(legs[s]))
        self.stage = "active"        # active -> hedge -> done/failed
        self.started_ts = time.time()
        self.stage_ts = time.time()   # 当前阶段起始（停滞告警用）
        self.warned_stall = False
        self.audit_due: float | None = None   # done 后裸腿复核时间（None=未排）
        self.audited = False

    def pending_hedges(self) -> dict[str, float]:
        """已成交部分需要补的对冲量（按已成交比例逐腿对冲，防裸腿）。"""
        done_active = abs(self.done.get(self.active, 0.0))
        ratio = done_active / abs(self.targets.get(self.active, 1.0) or 1.0)
        out: dict[str, float] = {}
        for sym, target in self.targets.items():
            if sym == self.active:
                continue
            remain = target - self.done.get(sym, 0.0)
            if abs(target) > 1e-12:
                out[sym] = remain * ratio
            else:
                out[sym] = remain
        return out

    def order_done(self, symbol: str, ok: bool) -> None:
        """单腿订单回调（成功与否 + 已成交部分由调用方注入 done）。"""
        if not ok:
            self.stage = "failed"


def worker_main(req_queue: Any, rep_queue: Any, config: dict) -> None:
    """multiprocessing spawn 入口（必须为模块级函数）。"""
    _Worker(req_queue, rep_queue, config).run()


class _Worker:
    TICK_INTERVAL = 0.2
    SNAPSHOT_INTERVAL = 1.0
    ERROR_COOLDOWN = 5.0
    LINK_FAIL_LIMIT = 3          # 连续 N 次 account_info 无数据 → 判定连接丢失
    ALGO_STALL_SECONDS = 30.0    # 价差算法阶段无回执阈值
    ALGO_AUDIT_DELAY = 5.0       # 全部成交后 N 秒做裸腿复核（等 broker 端同步）
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
        self._terminal_pid: int | None = None   # 本次会话拉起的终端 PID
        # fx_rates 快照的品种名缓存 {小写: 真实名}（首次 symbols_get 后懒加载）
        self._fx_symbol_cache: dict[str, str] | None = None
        # 连接丢失检测/自动重连状态（_poll_snapshot 维护）
        self._link_lost = False
        self._link_fail_n = 0
        self._last_reconnect = 0.0
        # 价差算法执行器状态（_start_spread_algo 维护，订单结果驱动）
        self._algos: dict[str, _SpreadTaker] = {}
        self._vol_step_cache: dict[str, float] = {}
        self._algo_audits: list[_SpreadTaker] = []   # done 后等裸腿复核

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------
    def run(self) -> None:
        # 连接流程（两段式：先直接 attach，失败则写凭据+拉终端自动登录）
        if not self._connect():
            self._send(EVENT_STATUS, {"ok": False, "msg": "会话已停止"})
            return

        poller = threading.Thread(target=self._poll_loop, daemon=True)
        poller.start()

        while not self._stop_evt.is_set():
            self._process_requests(0.2)

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
            with self._mt5_lock:
                try:
                    ok = mt5.initialize(path=path)
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
            if acc is not None and int(acc.login) == login:
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

        if action == "subscribe":
            self._subscribe(str(msg["symbol"]))
        elif action == "order":
            self._handle_order(msg["request"])
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
            self._start_spread_algo(msg)
        elif action == "stop":
            self._stop_evt.set()
            self._swap_watch_evt.set()
            # 价差算法执行中不剔除（IOC 单已发，未完成对冲的裸腿必须补齐）
            if self._algos:
                self._log(f"注意：退出时仍有 {len(self._algos)} 个价差算法在执行，将跑完对冲阶段")
            if msg.get("close_terminal"):
                self._close_terminal_processes()
        elif action is None:
            pass   # config 首条消息（worker_pipe 握手），忽略
        else:
            self._log(f"未知请求 action={action!r}")

    # ------------------------------------------------------------------
    # 价差算法执行器（模式引 vnpy SpreadTakerAlgo，IOC 市价简化）
    # ------------------------------------------------------------------
    def _start_spread_algo(self, msg: dict) -> None:
        """自动价差交易：GUI 传 [{symbol, side, volume(正手数，方向在 side)}]，
        主动腿（手数最大的腿）先吃单，成交回调立即对冲其余腿
        （vnpy take_active_leg -> hedge_passive_legs 两阶段语义）。
        任一腿被拒 -> 停止并大声报错（防裸腿暴露永不自动重试）。"""
        legs_cfg = msg.get("legs", [])
        comment = str(msg.get("comment", ""))
        if not legs_cfg:
            self._push_error("spread_algo", "legs 为空")
            return
        if not self._connected:
            self._push_error("spread_algo", "未连接，算法拒绝")
            return

        # 带符号目标手数（步长环整；柜台只接受步长整数倍手数）
        targets: dict[str, float] = {}
        for item in legs_cfg:
            symbol = str(item.get("symbol", "")).strip()
            side = str(item.get("side", "buy")).lower()
            volume = _f(item.get("volume"))
            if not symbol or volume <= 0:
                continue
            signed = volume if side == "buy" else -volume
            targets[symbol] = round_to_step(signed, self._volume_step(symbol))
        targets = {sym: v for sym, v in targets.items() if abs(v) > 1e-12}
        if not targets:
            self._push_error("spread_algo", "目标手数环整后全为 0")
            return

        algoid = f"ALGO-{int(time.time() * 1000) % 10**8}"
        algo = _SpreadTaker(algoid, targets, comment)
        self._algos[algoid] = algo
        self._log(
            f"[价差算法 {algoid}] 启动：主动腿 {algo.active}，"
            f"目标手数 {targets}"
        )
        self._algo_event(algo, "active", f"主动腿 {algo.active} 吃单")
        self._algo_take_active(algo)

    def _algo_event(self, algo: _SpreadTaker, stage: str, msg: str) -> None:
        self._send(EVENT_SPREAD_ALGO, {
            "algoid": algo.algoid, "stage": stage, "msg": msg,
        })

    def _algo_take_active(self, algo: _SpreadTaker) -> None:
        """阶段1：主动腿吃单（IOC 市价）。"""
        symbol = algo.active
        volume = abs(algo.targets[symbol])
        self._log(f"[价差算法 {algo.algoid}] 主动腿 {symbol} 吃单 {volume:.2f} 手")
        request = {
            "kind": "market",
            "symbol": symbol,
            "side": "buy" if algo.targets[symbol] > 0 else "sell",
            "volume": volume,
            "comment": (algo.comment or "spread")[:31],
            "algoid": algo.algoid,
        }
        self._handle_order(request)

    def _algo_hedge_passive(self, algo: _SpreadTaker) -> None:
        """阶段2：按已成交比例对冲其余腿（vnpy hedge_passive_legs 语义）。
        hedge 事件必须先发再发单——最后一腿的成交回调会同步触发 done，
        事件流顺序：active -> hedge -> done。"""
        algo.stage = "hedge"
        algo.stage_ts = time.time()
        algo.warned_stall = False
        self._algo_event(algo, "hedge", "对冲指令已发出")
        hedges = algo.pending_hedges()
        for symbol, signed_volume in hedges.items():
            volume = abs(signed_volume)
            if volume <= 1e-9:
                continue
            self._log(
                f"[价差算法 {algo.algoid}] 对冲腿 {symbol} "
                f"{'买' if signed_volume > 0 else '卖'} {volume:.2f} 手"
            )
            request = {
                "kind": "market",
                "symbol": symbol,
                "side": "buy" if signed_volume > 0 else "sell",
                "volume": volume,
                "comment": (algo.comment or "hedge")[:31],
                "algoid": algo.algoid,
            }
            self._handle_order(request)

    def _on_algo_leg_result(
        self, algoid: str, symbol: str, request: dict, ok: bool, filled: float,
    ) -> None:
        """订单结果记账（成交/被拒都进入）。done 按实际成交量累计（带方向）。"""
        algo = self._algos.get(algoid)
        if algo is None:
            return
        sign = 1 if str(request.get("side")).lower() == "buy" else -1
        if ok:
            algo.done[symbol] += sign * abs(filled)
            self._log(
                f"[价差算法 {algoid}] {symbol} 成交 {abs(filled):.2f} 手 "
                f"（累计 {algo.done[symbol]:+.2f} / 目标 {algo.targets[symbol]:+.2f}）"
            )
        else:
            algo.order_done(symbol, False)   # stage=failed
            self._algos.pop(algoid, None)
            self._algo_event(
                algo, "failed",
                f"腿 {symbol} 被拒——存在裸腿暴露，请人工检查持仓！",
            )
            self._push_error(
                "spread_algo", f"[{algoid}] 腿 {symbol} 执行失败，请立即人工检查持仓"
            )
            return
        if all(
            abs(algo.done.get(sym, 0.0) - target) <= max(self._volume_step(sym) / 2, 1e-9)
            for sym, target in algo.targets.items()
        ):
            algo.stage = "done"
            algo.audit_due = time.time() + self.ALGO_AUDIT_DELAY
            algo.audited = False
            self._algo_audits.append(algo)   # 从 active 表移除，进入复核队列
            self._algos.pop(algoid, None)
            self._log(f"[价差算法 {algoid}] 全部腿成交完毕（待裸腿复核）")
            self._algo_event(algo, "done", "全部腿成交完毕（待复核）")
            return
        if algo.stage == "active" and abs(algo.done.get(algo.active, 0.0)) > 1e-9:
            self._algo_hedge_passive(algo)

    def _volume_step(self, symbol: str) -> float:
        """品种手数步长（缓存 symbol_info.volume_step，零/不存在则 0.01）。"""
        if symbol not in self._vol_step_cache:
            with self._mt5_lock:
                info = mt5.symbol_info(symbol)
            step = float(info.volume_step) if info is not None else 0.01
            self._vol_step_cache[symbol] = step if step > 0 else 0.01
        return self._vol_step_cache[symbol]

    def _close_terminal_processes(self) -> None:
        """纯 detach：只断开 Python API 连接，绝不杀 MT5 终端进程。

        本项目唯一路线 = 用户手动管理终端生命周期（登录/退出都由用户在
        终端里操作），程序侧只负责 attach/detach。"""
        with self._mt5_lock:
            try:
                mt5.shutdown()
            except Exception:
                pass
        self._log("已断开接口连接（终端保持运行）")

    # ------------------------------------------------------------------
    # 交易规则 / 隔夜利息检测
    # ------------------------------------------------------------------
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
        if self._swap_watch_thread and self._swap_watch_thread.is_alive():
            return
        self._swap_watch_evt.clear()

        def watch() -> None:
            while not self._swap_watch_evt.is_set() and not self._stop_evt.is_set() and self._connected:
                self._swap_scan()
                self._swap_watch_evt.wait(interval)

        self._swap_watch_thread = threading.Thread(target=watch, daemon=True)
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
        fetched_at = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(fetched_at_s))

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
                "currency_margin": getattr(s, "currency_margin", ""),
                "currency_deposit": getattr(s, "currency_deposit", ""),
            })
            prev = self._swap_fingerprint.get(s.name)
            if prev != fp:
                changed.append({**all_items[-1], "first": prev is None})

        self._swap_fingerprint = new_state
        payload = {
            "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
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
        """历史价差（乘法合成，用户公式）：

            合成买 USDJPY(Ask) = USDCHF Ask × CHFJPY Ask
            价差 = 实际卖出 USDJPY (Bid) − 合成买入 USDJPY (Ask)

        目标腿 = 卖出方向 |权重| 最大（实际报价基准），其余腿在 log
        空间按带符号权重合成（w>0 取 ask 侧、w<0 取 bid 侧），
        合成价 = exp(Σ s·log(side))；w=1 时严格等于逐项乘积。
        序列与蜡烛同用 H1 对齐时间轴；bid 基 OHLC + 每腿当前点差近似 ask。"""
        legs = msg.get("legs", [])
        if not legs:
            self._push_error("spread_history", "legs 为空")
            return
        if len({leg["symbol"] for leg in legs}) != len(legs):
            self._push_error("spread_history", "legs 含重复 symbol")
            return
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
        # 图与实时信号口径 = M1（用户要求，断开最少）；
        # H1（≥1 年）仅供统计先验/长窗极值参考。
        for tf_name, tf, count in (
            ("h1", mt5.TIMEFRAME_H1, 9600),
            ("m1", mt5.TIMEFRAME_M1, 14400),   # ≈10 个交易日 M1
        ):
            data = self._build_spread_dataset(legs, target_sym, tf_name, tf, count)
            if data is None:
                return
            datasets[tf_name] = data[0]
            datasets[tf_name + "_candles"] = data[1]

        self._send(EVENT_SPREAD_HISTORY, {
            "datasets": datasets,
            "legs": legs,
            "fetched_at": time.time(),
        })

    def _build_spread_dataset(self, legs, target_sym, tf_name, tf, count):
        """单周期的乘法合成价差序列 + 蜡烛（用户公式）。

        只在 legs 各自同时存在该时间戳时才出点（交集），
        M1 交集缺口远小于 H1（周末/结市更短），缺口更少、图更连贯。"""
        with self._mt5_lock:
            leg_ohlc: dict[str, dict[str, list]] = {}
            for leg in legs:
                rates = mt5.copy_rates_from_pos(leg["symbol"], tf, 0, count)
                if rates is None or len(rates) == 0:
                    self._push_error(
                        "copy_rates", f"{leg['symbol']} {tf_name}: {mt5.last_error()}"
                    )
                    return None
                ohlc = leg_ohlc[leg["symbol"]] = {k: [] for k in ("t", "o", "h", "l", "c")}
                for r in rates:
                    ohlc["t"].append(int(r["time"]))
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
        tp_adj = leg_adj[tp_idx]

        def _synth(vals: list[float]) -> float:
            """vals[i] = 该腿当前限价侧价（含点差），按 ±1 幂乘法合成。"""
            s = 0.0
            for (_, pw, _adj), v in zip(comp, vals):
                if v <= 0:
                    return float("nan")
                s += pw * math.log(v)
            return math.exp(s)

        t_out, v_out = [], []
        c_t, c_o, c_h, c_l, c_c = [], [], [], [], []
        for t in common_t:
            ti = idx_by_sym[tp_idx][t]
            t_bid_o = tp_part["o"][ti]
            t_bid_h = tp_part["h"][ti]
            t_bid_l = tp_part["l"][ti]
            t_bid_c = tp_part["c"][ti]
            close_side, open_side = [], []
            min_side, max_side = [], []
            for (pi, pw, adj) in comp:
                i = idx_by_sym[pi][t]
                bid = ohlc_parts[pi]["c"][i]
                if pw > 0:
                    close_side.append(bid + adj)
                    open_side.append(ohlc_parts[pi]["o"][i] + adj)
                    min_side.append(ohlc_parts[pi]["l"][i] + adj)
                    max_side.append(ohlc_parts[pi]["h"][i] + adj)
                else:
                    close_side.append(bid)
                    open_side.append(ohlc_parts[pi]["o"][i])
                    min_side.append(ohlc_parts[pi]["h"][i])
                    max_side.append(ohlc_parts[pi]["l"][i])
            synth_c = _synth(close_side)
            synth_o = _synth(open_side)
            synth_lo = _synth(min_side)
            synth_hi = _synth(max_side)
            spread_c = t_bid_c - synth_c
            spread_o = t_bid_o - synth_o
            spread_h = t_bid_h - synth_lo
            spread_l = t_bid_l - synth_hi
            v_out.append(spread_c)
            t_out.append(t)
            c_t.append(t)
            c_o.append(spread_o)
            c_h.append(spread_h)
            c_l.append(spread_l)
            c_c.append(spread_c)
        return ({"t": t_out, "v": v_out},
                {"t": c_t, "o": c_o, "h": c_h, "l": c_l, "c": c_c})

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

    def _handle_order(self, request: dict) -> None:
        kind = request.get("kind", "market")

        if kind == "market":
            result = self._build_market(request)
        elif kind == "close":
            result = self._build_close(request)
        elif kind == "cancel":
            result = self._build_cancel(request)
        else:
            self._push_result(False, f"未知订单类型: {kind}", request)
            return

        if result is None:
            self._push_result(False, f"order_send 返回 None: {mt5.last_error()}", request)
            self._notify_algo_failure(request)
            return
        if result.retcode != TRADE_RETCODE_DONE:
            self._push_result(
                False,
                f"被服务器拒绝 retcode={result.retcode} comment={result.comment}",
                request,
            )
            self._notify_algo_failure(request)
            return
        self._push_result(
            True, f"成交单号 {result.order} / {result.deal}", request
        )
        self._notify_algo_fill(request, _f(result.volume))

    # ------------------------------------------------------------------
    # 价差算法结果钩子（有 algoid 标记的订单回调进算法状态机）
    # ------------------------------------------------------------------
    def _notify_algo_fill(self, request: dict, filled: float) -> None:
        algoid = request.get("algoid")
        if algoid:
            self._on_algo_leg_result(algoid, request.get("symbol", ""), request, True, filled)

    def _notify_algo_failure(self, request: dict) -> None:
        algoid = request.get("algoid")
        if algoid:
            self._on_algo_leg_result(algoid, request.get("symbol", ""), request, False, 0.0)

    # ------------------------------------------------------------------
    # 订单构造（直接拼 mt5 request）
    # ------------------------------------------------------------------
    def _build_market(self, req: dict):
        tick = self._tick(req["symbol"])
        if tick is None:
            return None
        side = mt5.ORDER_TYPE_BUY if req.get("side", "buy") == "buy" else mt5.ORDER_TYPE_SELL
        with self._mt5_lock:
            return mt5.order_send({
                "action": mt5.TRADE_ACTION_DEAL,
                "symbol": req["symbol"],
                "volume": float(req["volume"]),
                "type": side,
                "price": tick["bid"],
                "sl": float(req.get("sl") or 0.0),
                "tp": float(req.get("tp") or 0.0),
                "deviation": int(req.get("deviation", 20)),
                "magic": int(req.get("magic", MAGIC_DEFAULT)),
                "comment": str(req.get("comment", "light-trader"))[:31],
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": int(req.get("type_filling", mt5.ORDER_FILLING_IOC)),
            })

    def _build_close(self, req: dict):
        tick = self._tick(req["symbol"])
        if tick is None:
            return None
        # 持仓为多(POSITION_TYPE_BUY=0) -> 反向卖出
        close_side = (
            mt5.ORDER_TYPE_SELL if req.get("side", "buy") == "buy" else mt5.ORDER_TYPE_BUY
        )
        with self._mt5_lock:
            return mt5.order_send({
                "action": mt5.TRADE_ACTION_DEAL,
                "position": int(req["ticket"]),
                "symbol": req["symbol"],
                "volume": float(req["volume"]),
                "type": close_side,
                "price": tick["bid"],
                "deviation": int(req.get("deviation", 20)),
                "magic": int(req.get("magic", MAGIC_DEFAULT)),
                "comment": "close",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": int(req.get("type_filling", mt5.ORDER_FILLING_IOC)),
            })

    def _build_cancel(self, req: dict):
        with self._mt5_lock:
            return mt5.order_send({
                "action": mt5.TRADE_ACTION_REMOVE,
                "order": int(req["ticket"]),
            })

    def _tick(self, symbol: str) -> dict | None:
        with self._mt5_lock:
            raw = mt5.symbol_info_tick(symbol)
        if raw is None:
            self._push_error("symbol_info_tick", f"{symbol}: {mt5.last_error()}")
            return None
        return raw._asdict()

    # ------------------------------------------------------------------
    # 轮询
    # ------------------------------------------------------------------
    def _poll_loop(self) -> None:
        last_snapshot = 0.0
        while not self._stop_evt.is_set() and self._connected:
            try:
                self._poll_ticks()
                now = time.monotonic()
                if now - last_snapshot >= self.SNAPSHOT_INTERVAL:
                    self._poll_snapshot()
                    last_snapshot = now
            except Exception as exc:
                self._push_error("poll", repr(exc))
            self._stop_evt.wait(self.TICK_INTERVAL)

    def _poll_ticks(self) -> None:
        items = []
        with self._mt5_lock:
            for symbol in self._symbols:
                raw = mt5.symbol_info_tick(symbol)
                if raw is None:
                    self._push_error(f"tick({symbol})", str(mt5.last_error()))
                    continue
                d = raw._asdict()
                items.append({
                    "symbol": symbol,
                    "bid": d["bid"],
                    "ask": d["ask"],
                    "time": d["time"],
                })
        if items:
            self._send(EVENT_TICK, items)

    def _algo_watchdogs(self) -> None:
        """价差算法期后巡检：阶段停滞告警 + 裸腿复核（positions 由调用者 fetch）。"""
        now = time.time()
        for algoid, algo in list(self._algos.items()):
            if algo.stage in ("active", "hedge") and not algo.warned_stall:
                stall = now - algo.stage_ts
                if stall >= self.ALGO_STALL_SECONDS:
                    algo.warned_stall = True
                    self._algo_event(
                        algo, "stalled",
                        f"阶段 {algo.stage} 已 {stall:.0f}s 无回执，请检查订单/连接")
                    self._push_error(
                        "spread_algo",
                        f"[{algoid}] {algo.stage} 阶段停滞 {stall:.0f}s，请人工介入")
        for algo in list(getattr(self, "_algo_audits", [])):
            if algo.audit_due is not None and now >= algo.audit_due:
                self._algo_audits.remove(algo)
                self._algo_leg_audit(algo)

    def _algo_leg_audit(self, algo: _SpreadTaker) -> None:
        """裸腿复核：对比 target 与 broker 实际持仓（同 symbol net）。"""
        with self._mt5_lock:
            positions = mt5.positions_get()
        actual: dict[str, float] = {}
        for p in (positions or []):
            sign = 1 if p.type == 0 else -1     # POSITION_TYPE_BUY=0
            sym = p.symbol
            actual[sym] = actual.get(sym, 0.0) + sign * float(p.volume)
        mismatches = []
        for sym, target in algo.targets.items():
            got = actual.get(sym, 0.0)
            step = self._volume_step(sym)
            if abs(got - target) > max(step / 2, 1e-9):
                mismatches.append(f"{sym}: 目标 {target:+.2f}/实际 {got:+.2f}")
        if mismatches:
            algo.stage = "audit_mismatch"
            self._algo_event(
                algo, "audit_mismatch",
                "裸腿不齐: " + " | ".join(mismatches))
            self._push_error(
                "spread_algo",
                f"[{algo.algoid}] 裸腿复核不齐: " + " | ".join(mismatches))
        else:
            algo.stage = "audit_ok"
            algo.audited = True
            self._log(f"[价差算法 {algo.algoid}] 裸腿复核通过（对冲完全）")
            self._algo_event(algo, "audit_ok", "裸腿复核通过（对冲完全）")

    def _poll_snapshot(self) -> None:
        self._algo_watchdogs()   # 停滞告警 + 到期裸腿复核（不涉及 mt5 直调）
        with self._mt5_lock:
            acc = mt5.account_info()
            if acc is not None:
                self._send(EVENT_ACCOUNT, acc._asdict())
                if self._link_lost:
                    self._link_lost = False
                    self._link_fail_n = 0
                    self._last_reconnect = 0.0
                    self._log("接口已恢复，自动重连成功")
                    self._send(EVENT_STATUS, {"ok": True, "msg": "已连接"})
            else:
                self._link_fail_n += 1
                if not self._link_lost and self._link_fail_n >= self.LINK_FAIL_LIMIT:
                    self._link_lost = True
                    self._log(f"接口连续 {self._link_fail_n} 次无数据，判定连接丢失；进入自动重连")
                    self._send(EVENT_STATUS, {"ok": False, "msg": "连接丢失，自动重连中"})
                self._push_error("account_info", str(mt5.last_error()))

            positions = mt5.positions_get()
            self._send(EVENT_POSITION, [p._asdict() for p in positions] if positions else [])
            if positions is None:
                self._push_error("positions_get", str(mt5.last_error()))

            orders = mt5.orders_get()
            self._send(EVENT_ORDER, [o._asdict() for o in orders] if orders else [])
            if orders is None:
                self._push_error("orders_get", str(mt5.last_error()))

        # 连接丢失后的自动重连：每 RECONNECT_EVERY 秒尝试一次 initialize（带锁）
        if self._link_lost and time.monotonic() - self._last_reconnect >= self.RECONNECT_EVERY:
            self._last_reconnect = time.monotonic()
            path = self._config.get("terminal_path")
            self._log("重连尝试: mt5.initialize …")
            with self._mt5_lock:
                try:
                    ok = mt5.initialize(path=path) if path else False
                    if ok:
                        acc = mt5.account_info()
                    else:
                        acc = None
                except Exception as exc:
                    ok, acc = False, None
                    self._push_error("reconnect", repr(exc))
            if ok and acc is not None:
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

    def _push_result(self, ok: bool, msg: str, request: dict) -> None:
        self._send(EVENT_ORDER_RESULT, {"ok": ok, "msg": msg, "request": request})
        self._send(
            EVENT_LOG,
            {"source": "订单", "msg": msg, "level": "INFO" if ok else "ERROR"},
        )
