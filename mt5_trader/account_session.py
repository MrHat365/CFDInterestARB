"""One disposable worker generation per account; registry owns its lifetime.

No MT5 import in the GUI process. start() is nonblocking; stop() waits for the
worker and must run outside Qt's main thread. Replies carry a generation token.
"""
from __future__ import annotations

import copy
import json
import queue
import secrets
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

from .connection_state import ConnectionState, Phase
from .constants import (
    EVENT_AUTOMATION_STATE,
    EVENT_MANAGED_STATE, EVENT_ACCOUNT, EVENT_ALGO_REQUEST, EVENT_DEAL_REQUEST, EVENT_LOG,
    EVENT_ORDER_REQUEST, EVENT_RULES, EVENT_STATUS, EVENT_SWAPS,
    EVENT_SYMBOL_SUBSCRIBE, EVENT_SYMBOLS, EVENT_TICK,
)
from .event_engine import Event, EventEngine

_REQUEST_EVENTS = (EVENT_SYMBOL_SUBSCRIBE, EVENT_ORDER_REQUEST,
                   EVENT_DEAL_REQUEST, EVENT_ALGO_REQUEST)
_CACHE_EVENTS = (EVENT_AUTOMATION_STATE, EVENT_MANAGED_STATE, EVENT_ACCOUNT, EVENT_RULES, EVENT_SWAPS, EVENT_SYMBOLS)
_HEARTBEAT_INTERVAL = 5.0
_HEARTBEAT_TIMEOUT = 20.0
_START_TIMEOUT = 15.0


class AccountSession:
    def __init__(self, event_engine: EventEngine, account: dict,
                 on_exit: Callable[[str], None] | None = None) -> None:
        self._ee = event_engine
        self.account = dict(account)
        self.name = f'{account["login"]}@{account["server"]}'
        self.generation = uuid.uuid4().hex
        self._on_exit = on_exit
        self._lock = threading.RLock()
        self._state = ConnectionState()
        self._cancel = threading.Event()
        self._ping_pending = threading.Event()
        self._reclaim_lock = threading.Lock()
        self.closed = threading.Event()
        self._req_q: queue.Queue = queue.Queue(maxsize=256)
        self._proc: subprocess.Popen | None = None
        self._conn: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._threads: list[threading.Thread] = []
        self._cache: dict[str, dict] = {}
        self._last_pong = time.monotonic()
        self._stderr_tail = b""
        self._subscriptions: frozenset[str] = frozenset()

    @property
    def state(self) -> ConnectionState:
        with self._lock:
            return self._state

    @property
    def subscriptions(self) -> frozenset[str]:
        with self._lock:
            return self._subscriptions

    def cached_events(self) -> list[Event]:
        with self._lock:
            return [Event(t, copy.deepcopy(data)) for t, data in self._cache.items()]

    def status_event(self) -> Event:
        with self._lock:
            return self._event(EVENT_STATUS, self._state.payload())

    def _event(self, etype: str, payload) -> Event:
        return Event(etype, {"session": self.name, "generation": self.generation,
                             "payload": payload})

    def _transition(self, phase: Phase, message: str) -> None:
        with self._lock:
            self._state = self._state.transition(phase, message)
            self._ee.put(self.status_event())

    def _worker_config(self) -> dict:
        # attach-only: passwords are neither needed nor sent across IPC.
        return {"login": self.account["login"], "server": self.account.get("server", ""),
                "symbols": self.account.get("symbols") or [],
                "terminal_path": self.account.get("terminal_path"),
                "execution_db": self.account.get("execution_db"),
                'broker_timezone': self.account.get('broker_timezone', 'UTC'),
                'quote_max_age': self.account.get('quote_max_age', 60)}

    def start(self) -> None:
        with self._lock:
            if self._thread is not None or self.closed.is_set():
                return
            self._transition(Phase.CONNECTING, "正在连接账户")
            for etype in _REQUEST_EVENTS:
                self._ee.register(etype, self._on_event)
            self._thread = threading.Thread(target=self._run,
                                            name=f"session-{self.name}", daemon=True)
            self._thread.start()

    def _spawn_thread(self, target, name: str) -> None:
        thread = threading.Thread(target=target, name=f"{name}-{self.name}", daemon=True)
        self._threads.append(thread)
        thread.start()

    def _run(self) -> None:
        try:
            # Bind before spawning: no free-port race or early connection refusal.
            with socket.socket() as server:
                server.bind(("127.0.0.1", 0))
                server.listen(4)
                server.settimeout(0.2)
                token = secrets.token_hex(32)
                command = ([sys.executable, "--worker"] if getattr(sys, "frozen", False)
                           else [sys.executable, "-m", "mt5_trader.worker_pipe"])
                self._proc = subprocess.Popen(
                    [*command, str(server.getsockname()[1])],
                    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE, close_fds=True,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    cwd=str(Path(__file__).resolve().parent.parent),
                )
                # Secret travels over an inherited pipe, not the command line.
                assert self._proc.stdin is not None
                self._proc.stdin.write((token + "\n").encode())
                self._proc.stdin.close()
                self._spawn_thread(self._drain_stderr, "stderr")
                deadline = time.monotonic() + _START_TIMEOUT
                while not self._cancel.is_set():
                    if self._proc.poll() is not None:
                        raise ConnectionError("worker 启动失败；请检查 Windows/MetaTrader5 依赖")
                    if time.monotonic() >= deadline:
                        raise TimeoutError("worker 连接超时")
                    try:
                        conn, _ = server.accept()
                    except socket.timeout:
                        continue
                    conn.settimeout(1.0)
                    try:
                        auth = b""
                        while len(auth) < 65 and not auth.endswith(b"\n"):
                            chunk = conn.recv(1)
                            if not chunk:
                                break
                            auth += chunk
                        if not secrets.compare_digest(auth, (token + "\n").encode()):
                            conn.close()
                            continue
                    except OSError:
                        conn.close()
                        continue
                    self._conn = conn
                    conn.settimeout(None)
                    conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    conn.sendall((json.dumps(self._worker_config()) + "\n").encode())
                    break
            if self._cancel.is_set():
                return
            self._last_pong = time.monotonic()
            self._spawn_thread(self._reader, "reply")
            self._spawn_thread(self._writer, "request")
            while not self._cancel.wait(_HEARTBEAT_INTERVAL):
                if time.monotonic() - self._last_pong >= _HEARTBEAT_TIMEOUT:
                    raise TimeoutError("worker 心跳超时，连接已失效")
                self._ping_pending.set()
        except Exception as exc:
            if not self._cancel.is_set():
                self._fail(str(exc))
        finally:
            self._cancel.set()
            for etype in _REQUEST_EVENTS:
                self._ee.unregister(etype, self._on_event)
            # Writer owns stop ordering: discard unsent user requests, send stop,
            # then let in-flight broker execution finish before closing transport.
            proc = self._proc
            try:
                if proc is not None:
                    if not any(t.name.startswith("request-") for t in self._threads) and proc.poll() is None:
                        proc.terminate()
                    try:
                        proc.wait(timeout=35)
                    except subprocess.TimeoutExpired:
                        self._push("worker 未在35秒内退出，强制结束；在途委托结果须在终端核对", "ERROR")
                        proc.terminate()
                        try:
                            proc.wait(timeout=3)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                            proc.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired) as exc:
                self._push(f"worker 清理失败，禁止重建会话直到确认进程已退出: {exc}", "ERROR")
            if self._conn is not None:
                try:
                    self._conn.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    self._conn.close()
                except OSError:
                    pass
            for thread in self._threads:
                thread.join(timeout=2)
            with self._lock:
                if proc is None or proc.poll() is not None:
                    if self._state.phase == Phase.STOPPING:
                        self._transition(Phase.DISCONNECTED, "已断开")
                    self.closed.set()
                elif self._state.phase != Phase.FAILED:
                    self._transition(Phase.FAILED, "进程尚未退出，请检查终端与系统进程")
            if self._on_exit:
                self._on_exit(self.name)

    def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        with proc.stderr as stream:
            while chunk := stream.read(4096):
                self._stderr_tail = (self._stderr_tail + chunk)[-65536:]

    def _reader(self) -> None:
        conn = self._conn
        if conn is None:
            return
        try:
            with conn.makefile("r", encoding="utf-8") as stream:
                for line in stream:
                    msg = json.loads(line)
                    if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
                        raise ValueError("worker 协议载荷无效")
                    if msg["type"] == "ePong":
                        self._last_pong = time.monotonic()
                        health = msg.get("payload") or {}
                        if health.get("healthy") is False:
                            stalled = ', '.join(health.get("stalled", []))
                            self._fail(f"worker 执行停滞（{stalled}），已停止会话；请核对持仓后重新连接")
                    else:
                        self._rep(msg["type"], msg.get("payload"))
        except (OSError, ValueError) as exc:
            if not self._cancel.is_set():
                self._fail(f"worker 通道异常: {exc}")
        finally:
            if not self._cancel.is_set():
                self._fail("worker 通道已关闭")

    def _writer(self) -> None:
        conn = self._conn
        if conn is None:
            return
        try:
            while not self._cancel.is_set():
                if self._ping_pending.is_set():
                    self._ping_pending.clear()
                    conn.sendall(b'{"action":"ping"}\n')
                try:
                    msg = self._req_q.get(timeout=0.2)
                except queue.Empty:
                    continue
                if self._cancel.is_set():
                    break
                conn.sendall((json.dumps(msg, allow_nan=False) + "\n").encode())
            conn.sendall(b'{"action":"stop"}\n')
        except (OSError, ValueError) as exc:
            if not self._cancel.is_set():
                self._fail(f"worker 请求失败: {exc}")

    def _fail(self, message: str) -> None:
        with self._lock:
            if self._state.phase in (Phase.FAILED, Phase.DISCONNECTED, Phase.STOPPING):
                return
            self._transition(Phase.FAILED, message)
            self._cancel.set()

    def _rep(self, etype: str, payload) -> None:
        with self._lock:
            if etype == EVENT_STATUS:
                if self._cancel.is_set() or self._state.phase == Phase.STOPPING:
                    return
                phase = Phase.CONNECTED if payload.get("ok") else Phase.RECONNECTING
                self._transition(phase, str(payload.get("msg", "")))
                if phase != Phase.CONNECTED:
                    self._subscriptions = frozenset()
                    self._cache.clear()
                return
            # Keep final execution reports during graceful stop, but never fresh
            # market data from a stopping/failed generation.
            if self._cancel.is_set() and etype in (*_CACHE_EVENTS, EVENT_TICK):
                return
            if etype == EVENT_SYMBOLS:
                self._subscriptions = frozenset(payload)
            event = self._event(etype, payload)
            if etype in _CACHE_EVENTS:
                self._cache[etype] = copy.deepcopy(event.data)
            self._ee.put(event)

    @property
    def worker_pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    def begin_stop(self) -> None:
        """Atomically stop accepting replies/commands before publishing STOPPING."""
        with self._lock:
            if self.closed.is_set():
                return
            self._cancel.set()
            if self._thread is None:
                self.closed.set()
                return
            if self._state.phase != Phase.STOPPING:
                self._transition(Phase.STOPPING, "正在断开，等待在途任务结束")

    def stop(self, close_terminal: bool = False) -> None:
        self.begin_stop()
        if self._thread is None:
            return
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout=42)

    def reclaim(self) -> bool:
        """Retry cleanup of this owned worker only; never kill the MT5 terminal."""
        if not self._reclaim_lock.acquire(blocking=False):
            return False
        try:
            if self._thread is not None and self._thread.is_alive():
                self._push('会话仍在清理，请等待后重试', 'ERROR')
                return False
            self.begin_stop()
            proc = self._proc
            try:
                if proc is not None and proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=3)
                if proc is not None and proc.poll() is None:
                    raise TimeoutError('worker仍未退出')
            except (OSError, subprocess.TimeoutExpired, TimeoutError) as exc:
                self._transition(Phase.FAILED, f'worker回收失败，禁止重连：{exc}')
                return False
            with self._lock:
                self.closed.set()
                if self._state.phase == Phase.STOPPING:
                    self._transition(Phase.DISCONNECTED, 'worker已回收，请核对在途委托后重连')
            return True
        finally:
            self._reclaim_lock.release()

    def _on_event(self, event: Event) -> None:
        data = event.data
        if not isinstance(data, dict) or data.get("session") != self.name:
            return
        if data.get("generation") != self.generation:
            return
        actions = {EVENT_SYMBOL_SUBSCRIBE: "subscribe", EVENT_DEAL_REQUEST: "deals",
                   EVENT_ALGO_REQUEST: "spread_algo"}
        raw = {k: v for k, v in data.items() if k not in ("session", "generation")}
        if event.type == EVENT_ORDER_REQUEST:
            self._send_raw({"action": "order", "request": raw})
        else:
            self._send_raw({**raw, "action": actions[event.type]})

    def _send_raw(self, msg: dict) -> bool:
        if self._cancel.is_set() or self._conn is None:
            self._push(f"请求未接受（会话未就绪）: {msg.get('action')}", "ERROR")
            return False
        if msg.get("action") in ("order", "spread_algo", "managed_trade", "automation") and self._state.phase != Phase.CONNECTED:
            self._push("请求未接受（账户未连接）", "ERROR")
            return False
        try:
            self._req_q.put_nowait(msg)
        except queue.Full:
            self._push("请求未接受（队列已满），请稍后重试", "ERROR")
            return False
        return True


    def request_managed_trade(self, message: dict) -> bool:
        return self._send_raw({**message, "action": "managed_trade"})

    def request_managed_state(self) -> bool:
        return self._send_raw({"action": "managed_snapshot"})

    def request_automation(self, message: dict) -> bool:
        return self._send_raw({**message, "action": "automation"})

    def request_rules(self) -> bool:
        return self._send_raw({"action": "rules"})

    def watch_swaps(self, interval: int = 60) -> bool:
        return self._send_raw({"action": "swap_watch", "interval": interval})

    def unwatch_swaps(self) -> bool:
        return self._send_raw({"action": "swap_unwatch"})

    def set_subscriptions(self, symbols: list[str]) -> bool:
        return self._send_raw({"action": "subscriptions", "symbols": symbols})

    def request_spread_history(self, legs: list[dict], request_id: str = "") -> bool:
        return self._send_raw({"action": "spread_history", "legs": legs, "request_id": request_id})

    def request_deals(self, days: int = 7) -> bool:
        return self._send_raw({"action": "deals", "days": int(days)})

    def _push(self, message: str, level: str = "INFO") -> None:
        self._ee.put(self._event(EVENT_LOG, {"source": self.name, "msg": message, "level": level}))
