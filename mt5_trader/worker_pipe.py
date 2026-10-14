"""worker 子进程入口（DPG 迁移配套）：subprocess + JSON 管道。

为什么不用 multiprocessing.spawn：DearPyGui 在 Windows 上创建 DirectX 渲染
上下文后，spawn 的管道句柄复制会因句柄继承策略产生 WinError 5（拒绝访问），
multiprocessing.Queue 完全不可用。改用独立窗口子进程 + 双向 JSON-lines 管道：
  父进程（GUI）→ {port} 传参 → 子进程连回 → 父发 config 一行 →
  子进程 worker_main(pair of queue.Queue)；每行一条 {"type","payload"} 消息。
worker 内部代码与 Qt 版 100% 复用（worker_main 不感知 IPC 载体）。
"""

from __future__ import annotations

import json
import queue
import socket
import sys
import threading
from pathlib import Path

from mt5_trader.event_engine import Event  # noqa: F401
from mt5_trader.constants import EVENT_DEAL_REQUEST  # noqa: F401
from mt5_trader.worker import worker_main


def _jsonable(value):
    """payload 递归 JSON 化（namedtuple/bytes/datetime 兑底）。"""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    has_asdict = getattr(value, "_asdict", None)
    if has_asdict:
        return _jsonable(dict(has_asdict()))
    return str(value)


def main(port: int) -> None:
    sock = socket.create_connection(("127.0.0.1", port))
    req_q: "queue.Queue" = queue.Queue()
    rep_q: "queue.Queue" = queue.Queue()

    # 读 config 一行（首条）
    reader = sock.makefile("r", encoding="utf-8")
    config = json.loads(reader.readline())
    req_q.put(config)   # worker 首条请求忽略（worker 不读握手）

    def forward_requests():
        """GUI -> worker：req_from_parent 一行一个 action。
        （config 由 worker_main 自行从 req_q 接收——首条消息。）"""
        try:
            for line in reader:
                if line.strip():
                    req_q.put(json.loads(line))
        except Exception:
            pass

    def forward_replies():
        """worker -> GUI：rep 一行一条事件（含 ORDER_RESULT 等）。"""
        try:
            while True:
                msg = rep_q.get()
                payload = _jsonable(msg.get("payload"))
                sock.sendall(
                    (json.dumps({"type": msg.get("type", "eUnknown"),
                                 "payload": payload}, ensure_ascii=False)
                     + "\n").encode("utf-8"))
        except Exception:
            pass

    threading.Thread(target=forward_requests, name="pipe-req", daemon=True).start()
    threading.Thread(target=forward_replies, name="pipe-rep", daemon=True).start()

    worker_main(req_q, rep_q, config)  # 阻塞运行 worker



if __name__ == "__main__":
    if len(sys.argv) > 1:
        main(int(sys.argv[1]))
    else:
        print("usage: python -m mt5_trader.worker_pipe <port>")
