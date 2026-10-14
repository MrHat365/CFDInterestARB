"""SQLite 数据层。

存储内容（建表即建）：
  terminals      扫描到的本机 MT5 客户端（程序路径/数据目录）
  accounts       账户凭据 + 与终端的一对一绑定
  swap_snapshots 仅当 swap 发生变化时入库，附带本库检测时间 => 自建"更新时间"
  trades         策略信号/纸面/实盘交易记录
  spread_points  策略跑动时的价差序列缓存（ts/spread/zscore/boundary）

线程模型：EventEngine 分发线程内调用，配 check_same_thread=False + 锁。
"""

from __future__ import annotations

import json
import sqlite3
import datetime as dt
import threading
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS terminals(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT UNIQUE NOT NULL,
    name TEXT DEFAULT '',
    data_folder TEXT DEFAULT '',
    scanned_at TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS accounts(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    login INTEGER NOT NULL,
    server TEXT NOT NULL,
    password TEXT NOT NULL DEFAULT '',
    terminal_id INTEGER,
    symbols TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    UNIQUE(login, server));

CREATE TABLE IF NOT EXISTS swap_snapshots(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_key TEXT NOT NULL,
    symbol TEXT NOT NULL,
    swap_long REAL,
    swap_short REAL,
    rollover3days INTEGER,
    fetched_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_swap_key
    ON swap_snapshots(account_key, symbol, fetched_at DESC);

CREATE TABLE IF NOT EXISTS trades(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_key TEXT NOT NULL,
    strategy_id TEXT NOT NULL DEFAULT '',
    ts TEXT NOT NULL,
    mode TEXT NOT NULL,            -- paper / live / signal
    action TEXT NOT NULL,          -- open / close / signal
    legs TEXT NOT NULL DEFAULT '[]',
    info TEXT NOT NULL DEFAULT '{}');

CREATE TABLE IF NOT EXISTS spread_points(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_key TEXT NOT NULL,
    strategy_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    spread REAL, zscore REAL, mean REAL, upper REAL, lower REAL,
    hedge TEXT NOT NULL DEFAULT '[]');
CREATE INDEX IF NOT EXISTS idx_spread_key
    ON spread_points(account_key, strategy_id, ts DESC);
"""


def _now() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


class Database:
    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else Path(__file__).parent / "mt5_trader.db"
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    # ------------------------------------------------------------------
    # terminals
    # ------------------------------------------------------------------
    def sync_terminals(self, found: list[dict]) -> list[dict]:
        """扫描结果 upsert（去重 + 存在性校验）；返回完整列表。

        注入前先用 Path(path).exists() 过滤幽灵路径（文件夹/文件已删但仍被
        游留 AppData origin / 旧注册表项 / 运行中进程指向的终端），并顺手
        清理库中已失效的旧行 —— 保证 terminals 表永远只给真实存在的路径。"""
        with self._lock:
            for item in found:
                if not Path(item["path"]).exists():
                    continue  # 幽灵终端不入库
                existing = self._conn.execute(
                    "SELECT id FROM terminals WHERE path = ?", (item["path"],)
                ).fetchone()
                if existing:
                    self._conn.execute(
                        "UPDATE terminals SET data_folder = COALESCE(NULLIF(?, ''), data_folder), "
                        "scanned_at = ? WHERE id = ?",
                        (item.get("data_folder", ""), _now(), existing["id"]),
                    )
                else:
                    self._conn.execute(
                        "INSERT INTO terminals(path, name, data_folder, scanned_at) VALUES(?,?,?,?)",
                        (item["path"], item.get("name", ""), item.get("data_folder", ""), _now()),
                    )
            # 清理幽灵行：路径已不存在的终端记录直接删除
            stale = [
                r["id"] for r in self._conn.execute(
                    "SELECT id, path FROM terminals"
                ).fetchall()
                if not Path(r["path"]).exists()
            ]
            if stale:
                self._conn.executemany(
                    "DELETE FROM terminals WHERE id = ?", [(i,) for i in stale]
                )
            self._conn.commit()
            rows = self._conn.execute(
                "SELECT id, path, name, data_folder, scanned_at FROM terminals ORDER BY id"
            ).fetchall()
        return [dict(r) for r in rows]

    def terminals(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, path, name, data_folder, scanned_at FROM terminals ORDER BY id"
            ).fetchall()
        return [dict(r) for r in rows]

    def terminal_path(self, terminal_id: int) -> str | None:
        row = self._one("SELECT path FROM terminals WHERE id = ?", (terminal_id,))
        return row["path"] if row and row["path"] else None

    # ------------------------------------------------------------------
    # accounts
    # ------------------------------------------------------------------
    def accounts(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM accounts ORDER BY id"
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["symbols"] = [s for s in d["symbols"].split(",") if s]
            out.append(d)
        return out

    def account(self, login: int, server: str) -> dict | None:
        row = self._one("SELECT * FROM accounts WHERE login=? AND server=?", (login, server))
        if row is None:
            return None
        d = dict(row)
        d["symbols"] = [s for s in d.get("symbols", "").split(",") if s]
        return d

    def upsert_account(self, item: dict) -> int:
        with self._lock:
            cur = self._conn.execute(
                "SELECT id FROM accounts WHERE login=? AND server=?",
                (item["login"], item["server"]),
            ).fetchone()
            symbols = ",".join(item.get("symbols", []))
            if cur:
                self._conn.execute(
                    "UPDATE accounts SET password=?, terminal_id=?, symbols=? WHERE id=?",
                    (item["password"], item.get("terminal_id") or None,
                     symbols, cur["id"]),
                )
                account_id = cur["id"]
            else:
                cur = self._conn.execute(
                    "INSERT INTO accounts(login, server, password, terminal_id, symbols, created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (item["login"], item["server"], item["password"],
                     item.get("terminal_id") or None, symbols, _now()),
                )
                account_id = cur.lastrowid
            self._conn.commit()
        return account_id

    def delete_account(self, login: int, server: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM accounts WHERE login=? AND server=?", (login, server))
            self._conn.commit()

    # ------------------------------------------------------------------
    # swap 快照（变化时才入库 => 自建更新时间）
    # ------------------------------------------------------------------
    def insert_swap_changes(self, account_key: str, items: list[dict], fetched_at: str) -> int:
        n = 0
        with self._lock:
            for it in items:
                last = self._conn.execute(
                    "SELECT swap_long, swap_short, rollover3days FROM swap_snapshots "
                    "WHERE account_key=? AND symbol=? ORDER BY id DESC LIMIT 1",
                    (account_key, it["symbol"]),
                ).fetchone()
                same = (
                    last is not None
                    and last["swap_long"] == round(it["swap_long"], 10)
                    and last["swap_short"] == round(it["swap_short"], 10)
                    and last["rollover3days"] == it["rollover3days"]
                )
                if last is None or not same:
                    self._conn.execute(
                        "INSERT INTO swap_snapshots(account_key, symbol, swap_long, swap_short, "
                        "rollover3days, fetched_at) VALUES(?,?,?,?,?,?)",
                        (account_key, it["symbol"], it["swap_long"], it["swap_short"],
                         it["rollover3days"], fetched_at),
                    )
                    n += 1
            self._conn.commit()
        return n

    def last_swap_time(self, account_key: str, symbol: str) -> str | None:
        row = self._one(
            "SELECT fetched_at FROM swap_snapshots WHERE account_key=? AND symbol=? "
            "ORDER BY id DESC LIMIT 1", (account_key, symbol),
        )
        return row["fetched_at"] if row else None

    def last_swap_change_time(self, account_key: str) -> str | None:
        row = self._one(
            "SELECT MAX(fetched_at) AS t FROM swap_snapshots WHERE account_key=?",
            (account_key,),
        )
        return row["t"] if row else None

    # ------------------------------------------------------------------
    # 策略记录 / 价差缓存
    # ------------------------------------------------------------------
    def insert_trade(self, account_key: str, ts: str, mode: str, action: str,
                     strategy_id: str, legs: list | dict, info: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO trades(account_key, strategy_id, ts, mode, action, legs, info) "
                "VALUES(?,?,?,?,?,?,?)",
                (account_key, strategy_id, ts, mode, action,
                 json.dumps(legs, ensure_ascii=False),
                 json.dumps(info, ensure_ascii=False, default=str)),
            )
            self._conn.commit()

    def insert_spread_point(self, account_key: str, strategy_id: str, ts: str,
                            spread: float, zscore: float, mean: float,
                            upper: float, lower: float, hedge: list | None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO spread_points(account_key, strategy_id, ts, spread, zscore, mean, "
                "upper, lower, hedge) VALUES(?,?,?,?,?,?,?,?,?)",
                (account_key, strategy_id, ts, spread, zscore, mean, upper, lower,
                 json.dumps(hedge or [], ensure_ascii=False)),
            )
            self._conn.commit()

    def trades(self, account_key: str | None = None, limit: int = 500) -> list[dict]:
        if account_key:
            rows = self._many(
                "SELECT * FROM trades WHERE account_key=? ORDER BY id DESC LIMIT ?",
                (account_key, limit),
            )
        else:
            rows = self._many("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------
    def _one(self, sql: str, params: tuple) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def _many(self, sql: str, params: tuple) -> list:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()
