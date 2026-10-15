"""Authenticated loopback JSON-lines transport for one disposable MT5 worker."""

from __future__ import annotations

import json
import queue
import socket
import sys
import threading
import time

from mt5_trader.event_queue import EventQueue

REP_Q_MAXSIZE = 2048
_ReplyQueue = EventQueue


def drain_replies(reply_queue, timeout=5.0):
    """Bounded shutdown flush; never claim unsent replies were delivered."""
    deadline = time.monotonic() + timeout
    with reply_queue.all_tasks_done:
        while reply_queue.unfinished_tasks:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            reply_queue.all_tasks_done.wait(remaining)
    return True


def _jsonable(value):
    """payload 递归 JSON 化（namedtuple/bytes/datetime 兑底）。"""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    has_asdict = getattr(value, "_asdict", None)
    if has_asdict:
        return _jsonable(dict(has_asdict()))
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return str(value)


def main(port: int) -> None:
    sock = socket.create_connection(("127.0.0.1", port), timeout=10)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    token = sys.stdin.readline().strip()
    if len(token) != 64:
        raise ValueError("Missing worker authentication")
    sock.sendall((token + "\n").encode())
    req_q: "queue.Queue" = queue.Queue(maxsize=256)
    rep_q: "_ReplyQueue" = _ReplyQueue(REP_Q_MAXSIZE)

    # 读 config 一行（首条）
    reader = sock.makefile("r", encoding="utf-8")
    config = json.loads(reader.readline())
    sock.settimeout(None)  # Reader is idle legitimately; send watchdog is separate.
    # MT5 import follows authenticated IPC setup.
    from mt5_trader.worker import _Worker
    worker = _Worker(req_q, rep_q, config)

    def forward_requests():
        """GUI -> worker; stop/ping remain responsive during MT5 initialization."""
        try:
            for line in reader:
                if line.strip():
                    msg = json.loads(line)
                    if msg.get("action") == "stop":
                        worker.request_stop()
                    elif msg.get("action") == "ping":
                        # A live pipe does not prove that execution is advancing.
                        health = worker._health.snapshot()
                        if not health['healthy']:
                            worker.request_stop()
                        rep_q.put({"type": "ePong", "payload": health})
                    else:
                        req_q.put(msg, timeout=1)
        except (OSError, ValueError, queue.Full) as exc:
            print(f"[pipe-req] {exc!r}", file=sys.stderr)
        finally:
            worker.request_stop()

    transport_failed = threading.Event()
    transport_closed = threading.Event()
    send_started: list[float | None] = [None]

    def watch_sender():
        while not transport_closed.wait(.5):
            started = send_started[0]
            if started is not None and time.monotonic() - started >= 10:
                print('[pipe-rep] 发送超过10秒，断开通道；未确认回执须从账本核对', file=sys.stderr)
                transport_failed.set()
                worker.request_stop()
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                return

    def forward_replies():
        """worker -> GUI：rep 一行一条事件（含 ORDER_RESULT 等）。"""
        send_fail_reported = False   # 一次性日志：sendall 失败后线程退出（保持原语义）
        try:
            while True:
                msg = rep_q.get()
                try:
                    if transport_failed.is_set():
                        # The parent is gone: drain to unblock worker shutdown.
                        continue
                    payload = _jsonable(msg.get("payload"))
                    send_started[0] = time.monotonic()
                    sock.sendall(
                        (json.dumps({"type": msg.get("type", "eUnknown"),
                                     "payload": payload}, ensure_ascii=False)
                         + "\n").encode("utf-8"))
                except Exception as exc:
                    if not send_fail_reported:
                        send_fail_reported = True
                        print(
                            f"[pipe-rep] 回复发送失败，停止worker并排空本地队列: {exc!r}",
                            file=sys.stderr,
                        )
                    transport_failed.set()
                    worker.request_stop()
                finally:
                    send_started[0] = None
                    rep_q.task_done()
        except Exception as exc:
            print(f"[pipe-rep] {exc!r}", file=sys.stderr)
            transport_failed.set()
            worker.request_stop()

    threading.Thread(target=forward_requests, name="pipe-req", daemon=True).start()
    threading.Thread(target=forward_replies, name="pipe-rep", daemon=True).start()
    threading.Thread(target=watch_sender, name="pipe-send-watch", daemon=True).start()

    try:
        worker.run()
        if not drain_replies(rep_q):
            print('[pipe-rep] 回执排空超时；执行结果须从持久账本核对', file=sys.stderr)
    finally:
        transport_closed.set()
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()



if __name__ == "__main__":
    if len(sys.argv) > 1:
        main(int(sys.argv[1]))
    else:
        print("usage: python -m mt5_trader.worker_pipe <port>")
