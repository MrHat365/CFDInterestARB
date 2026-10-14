"""AccountSession —— 主进程侧的账户会话代理。

每个账户对应一个实例，内部以 **subprocess + JSON 管道** 拉起 worker 子进程
（直接调 mt5；不用 multiprocessing.spawn —— DPG 的 DirectX 渲染上下文会让
spawn 的管道句柄复制产生 WinError 5，multiprocessing.Queue 不可用）。
职责：
- 子进程 / 双队列生命周期管理（同旧接口，调用方无感）
- GUI 请求（事件总线）→ req 管道转发（按 session=login@server 过滤）
- rep 管道 → 事件总线回流（打上 session 标签）
- 自身不 import MetaTrader5，主 GUI 进程零 mt5 调用

会话键 = "login@server"，同一登录号连接不同服务器也是两个会话。
扩展请求（rules/swap_watch/swap_unwatch/spread_history/spread_algo）在同一通道透传。
"""

from __future__ import annotations

import json
import queue as _queue
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .constants import (
    EVENT_ALGO_REQUEST,
    EVENT_DEAL_REQUEST,
    EVENT_ORDER_REQUEST,
    EVENT_STATUS,
    EVENT_SYMBOL_SUBSCRIBE,
)

# stderr 保留尾部大小（供调试排查 worker 崩溃原因）
_STDERR_TAIL_LIMIT = 64 * 1024

# 心跳参数：每 5s ping 一次；15s 无任何 rep 回包记一次未命中，连续 3 次判死
_HEARTBEAT_INTERVAL = 5.0
_HEARTBEAT_TIMEOUT = 15.0
_HEARTBEAT_MAX_MISSES = 3
from .event_engine import Event, EventEngine

# 直接透传给子进程 action 的事件（data 内带 session 与参数）
PASSTHROUGH_ACTIONS = (
    EVENT_SYMBOL_SUBSCRIBE,    # {symbol}
    EVENT_ORDER_REQUEST,       # {kind, ...}
    EVENT_DEAL_REQUEST,        # {days}
)


class AccountSession:
    """账户会话：GUI ↔ worker 子进程（subprocess + JSON 管道）。"""

    def __init__(
        self,
        event_engine: EventEngine,
        account: dict,
        on_exit: Callable[[str], None] | None = None,
    ) -> None:
        self._ee = event_engine
        self.account = dict(account)
        self.name = f'{account["login"]}@{account["server"]}'
        self._on_exit = on_exit or (lambda name: None)

        # GUI→worker 请求队列（本地）；rep 由管道线程推进事件引擎
        self._req_q: _queue.Queue = _queue.Queue()
        self._stopped = False
        self._proc: subprocess.Popen | None = None
        self._conn: socket.socket | None = None
        self._reader: threading.Thread | None = None
        self._req_writer: threading.Thread | None = None
        # 心跳线程与其 wakeup/退出事件；_last_rep_at 最近一次收到 rep 行的单调钟
        self._heartbeat: threading.Thread | None = None
        self._hb_stop = threading.Event()
        self._last_rep_at: float = time.monotonic()
        # stderr 尾部缓存（drain 线程写入，供崩溃后诊断）与丢弃行计数
        self._stderr_tail: bytes = b""
        self._stderr_lines = 0

        self._ee.register(EVENT_SYMBOL_SUBSCRIBE, self._on_event)
        self._ee.register(EVENT_ORDER_REQUEST, self._on_event)
        self._ee.register(EVENT_DEAL_REQUEST, self._on_event)
        self._ee.register(EVENT_ALGO_REQUEST, self._on_event)

    # ------------------------------------------------------------------
    def _worker_config(self) -> dict:
        path = self.account.get("terminal_path") or None
        return {
            "login": self.account["login"],
            "password": self.account.get("password", ""),
            "server": self.account.get("server", ""),
            "symbols": self.account.get("symbols", []),
            "terminal_path": path,
        }

    @staticmethod
    def _free_port() -> int:
        """本地空闲端口。"""
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        return port

    # ------------------------------------------------------------------
    def start(self) -> None:
        """启动 worker 子进程（subprocess + 双向 JSON 管道，DPG 兼容）。"""
        if self._stopped:
            return
        port = self._free_port()
        self._proc = subprocess.Popen(
            [sys.executable, "-m", "mt5_trader.worker_pipe", str(port)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            close_fds=True,
            cwd=str(Path(__file__).resolve().parent.parent),
        )

        # stderr drain：PIPE 不读会把子进程写满阻塞（P0-3）。
        # 常驻 daemon 线程持续丢弃并计数，仅保留最后 64KB 尾部在内存；
        # 子进程退出时 read() 返回空，线程自然结束，stop() 不 join。
        def drain_stderr():
            buf = bytearray()
            proc_stderr = self._proc.stderr
            try:
                while True:
                    chunk = proc_stderr.read(8192)
                    if not chunk:
                        break
                    self._stderr_lines += 1
                    buf.extend(chunk)
                    if len(buf) > _STDERR_TAIL_LIMIT:
                        del buf[:len(buf) - _STDERR_TAIL_LIMIT]
                    self._stderr_tail = bytes(buf)
            except Exception:
                pass

        threading.Thread(target=drain_stderr,
                         name=f"stderr-{self.name}",
                         daemon=True).start()

        server = socket.socket()
        server.bind(("127.0.0.1", port))
        server.listen(1)
        server.settimeout(15)
        try:
            conn, _ = server.accept()
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self._conn = conn
            # 首条：config（JSON 一行）
            conn.sendall((json.dumps(self._worker_config(),
                                      ensure_ascii=False) + "\n").encode("utf-8"))
        except (socket.timeout, OSError) as exc:
            self._push(f"worker 子进程连接失败: {exc!r}", "ERROR")
            self._stopped = True
            try:
                self._proc.terminate()
            except Exception:
                pass
            server.close()
            return

        # rep 线程（worker → 本地 rep 队列 → _forward_loop → 事件引擎）。
        # 坏行只跳过不弃通道；EOF/OSError 在未主动 stop 时上报断线（R0-1）。
        def read_rep_lines():
            bad_lines = 0
            warned_bad = False

            def notify_closed(detail: str) -> None:
                if self._stopped:
                    return
                self._push(f"worker 读取通道断开(EOF/{detail})", "ERROR")
                self._ee.put(Event(
                    EVENT_STATUS,
                    {"session": self.name,
                     "payload": {"ok": False,
                                 "msg": "worker 通道断开（子进程可能已退出）"}},
                ))

            try:
                # errors="replace"：编码坏行降级为占位符，交由下方解析跳过，
                # 避免解码异常在迭代中直接炸掉整条读线程
                for line in conn.makefile("r", encoding="utf-8",
                                          errors="replace"):
                    # 任一行（合法/坏行/空行）都证明 worker 侧还活着 → 喂心跳
                    self._last_rep_at = time.monotonic()
                    if not line.strip():
                        continue
                    try:
                        msg = json.loads(line)
                    except ValueError:
                        bad_lines += 1
                        if bad_lines > 50 and not warned_bad:
                            warned_bad = True
                            self._push(
                                f"worker 输出通道已跳过 {bad_lines} 条"
                                "无法解析的行", "WARNING")
                        continue
                    self._rep(msg.get("type", "eUnknown"),
                              msg.get("payload"))
            except OSError as exc:
                notify_closed(repr(exc))
            else:
                notify_closed("")

        self._reader = threading.Thread(
            target=read_rep_lines, name=f"pipe-{self.name}", daemon=True)
        self._reader.start()

        # req 线程（本地队列 → 管道）
        def write_req_lines():
            while not self._stopped:
                try:
                    raw = self._req_q.get()
                except Exception:
                    break
                try:
                    self._conn.sendall((json.dumps(raw, ensure_ascii=False) + "\n")
                                       .encode("utf-8"))
                except Exception:
                    break
        self._req_writer = threading.Thread(
            target=write_req_lines, name=f"req-{self.name}", daemon=True)
        self._req_writer.start()

        # 心跳线程：定期 ping worker，连续 15s 无 rep 回包 ×3 判死（不 join 子进程）
        self._last_rep_at = time.monotonic()
        self._hb_stop.clear()

        def heartbeat_loop():
            misses = 0
            while True:
                if self._hb_stop.wait(_HEARTBEAT_INTERVAL):
                    return
                if self._stopped:
                    return
                self._send_raw({"action": "ping"})
                if time.monotonic() - self._last_rep_at >= _HEARTBEAT_TIMEOUT:
                    misses += 1
                    if misses >= _HEARTBEAT_MAX_MISSES:
                        if not self._stopped:
                            self._stopped = True
                            self._push("心跳超时（15s+ 无 worker 回包）", "ERROR")
                            self._ee.put(Event(
                                EVENT_STATUS,
                                {"session": self.name,
                                 "payload": {"ok": False,
                                             "msg": ("worker 心跳超时，"
                                                     "会话可能已死亡")}},
                            ))
                            # 关读端让 pipe 线程自然退出（仿 stop，不 join 子进程）
                            try:
                                if self._conn is not None:
                                    self._conn.shutdown(socket.SHUT_RDWR)
                            except Exception:
                                pass
                        return
                else:
                    misses = 0

        self._heartbeat = threading.Thread(
            target=heartbeat_loop, name=f"hb-{self.name}", daemon=True)
        self._heartbeat.start()

        self._push("会话已启动（subprocess+管道）", "INFO")

    def _rep(self, etype: str, payload: Any) -> None:
        """rep 事件入事件引擎（打上 session 标签）。"""
        if self._stopped:
            return
        self._ee.put(Event(etype, {"session": self.name, "payload": payload}))

    # ------------------------------------------------------------------
    def stop(self, close_terminal: bool = False) -> None:
        """停止会话（纯 detach，终端进程保持运行；未完成对冲的算法会跑完）。"""
        if self._stopped:
            return
        self._stopped = True
        # 收心跳线程（Event 置位 + 短 join，避免线程再 ping/误告警）
        self._hb_stop.set()
        hb = self._heartbeat
        if hb is not None and hb.is_alive():
            hb.join(timeout=0.2)
        self._send_raw({"action": "stop", "close_terminal": close_terminal})
        try:
            conn = self._conn
            if conn is not None:
                conn.shutdown(socket.SHUT_RDWR)   # 半关（req 写完 stop 后关读端）
        except Exception:
            pass
        proc = self._proc
        if proc is not None:
            try:
                proc.join(timeout=8)
            except Exception:
                pass
            if proc.poll() is None:
                proc.terminate()
        self._push("会话已停止", "INFO")

    # ------------------------------------------------------------------
    # 事件处理（GUI 请求，全部过滤自身会话）
    # ------------------------------------------------------------------
    def _on_event(self, event: Event) -> None:
        data = event.data
        if not (isinstance(data, dict) and data.get("session") == self.name):
            return
        if event.type == EVENT_SYMBOL_SUBSCRIBE:
            self._send_raw({"action": "subscribe",
                            "symbol": data.get("symbol", "")})
        elif event.type == EVENT_ORDER_REQUEST:
            request = {k: v for k, v in data.items() if k != "session"}
            self._send_raw({"action": "order", "request": request})
        elif event.type == EVENT_DEAL_REQUEST:
            self._send_raw({"action": "deals", "days": int(data.get("days", 7))})
        elif event.type == EVENT_ALGO_REQUEST:
            # 价差算法执行：legs=[{symbol, side, volume(正手数)}]
            self._send_raw({
                "action": "spread_algo",
                "legs": list(data.get("legs", [])),
                "comment": str(data.get("comment", "")),
            })

    # 扩展 action 直发接口（控制台以方法方式调用）
    def request_rules(self) -> None:
        self._send_raw({"action": "rules"})

    def watch_swaps(self, interval: int = 60) -> None:
        self._send_raw({"action": "swap_watch", "interval": interval})

    def unwatch_swaps(self) -> None:
        self._send_raw({"action": "swap_unwatch"})

    def request_spread_history(self, legs: list[dict]) -> None:
        self._send_raw({"action": "spread_history", "legs": legs})

    def request_deals(self, days: int = 7) -> None:
        self._send_raw({"action": "deals", "days": int(days)})

    # ------------------------------------------------------------------
    def _send_raw(self, msg: dict) -> None:
        # 仅入队：json 编码 + sendall 由 write_req_lines 写线程完成，
        # 避免调用方线程（常为 UI）在管道写满时被 sendall 同步阻塞（P0-4）
        if self._conn is None or self._stopped:
            self._push(f"消息发送失败（会话未准）: {msg.get('action', '')}", "ERROR")
            return
        self._req_q.put(msg)

    def _push(self, msg: str, level: str = "INFO") -> None:
        from .constants import EVENT_LOG
        self._ee.put(Event(
            EVENT_LOG,
            {"session": self.name,
             "payload": {"source": self.name, "msg": msg, "level": level}},
        ))
