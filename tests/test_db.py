"""database.py SQLite 数据层测试（tempfile 隔离，真实方法签名）。

注意：本模块不 import pytest，以便 tests/run_smoke.py 在无 pytest 环境下
直接收集运行（tmp_path 由 runner 注入；pytest 自带 tmp_path fixture）。
"""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

from mt5_trader.database import Database


# ---------------------------------------------------------------------------
# 实例化
# ---------------------------------------------------------------------------
def test_database_instantiation_at_tempfile_path(tmp_path):
    path = Path(tmp_path) / "mt5_test.db"
    instance = Database(path)
    try:
        assert instance.path == path
        assert path.exists()
        # 建表即建：核心表存在
        tables = {
            r["name"] for r in instance._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {"terminals", "accounts", "swap_snapshots", "trades",
                "spread_points"} <= tables
    finally:
        instance.close()


def test_database_default_path_falls_back_to_package_dir():
    instance = Database()
    try:
        assert instance.path.name == "mt5_trader.db"
    finally:
        instance.close()


# ---------------------------------------------------------------------------
# terminals
# ---------------------------------------------------------------------------
def test_terminals_upsert_and_ghost_filter(db):
    real_dir = tempfile.mkdtemp()          # 真实存在的路径
    fake = str(Path(tempfile.gettempdir()) / "no_such_terminal_dir_xyz")
    rows = db.sync_terminals([
        {"path": real_dir, "name": "T1", "data_folder": "C:/data1"},
        {"path": fake, "name": "Ghost", "data_folder": ""},
    ])
    # 幽灵路径不入库
    assert [r["path"] for r in rows] == [real_dir]
    assert rows[0]["name"] == "T1"
    assert rows[0]["data_folder"] == "C:/data1"
    # 再次 sync：upsert 更新 data_folder，不产生重复行
    rows2 = db.sync_terminals([{"path": real_dir, "name": "T1b",
                                "data_folder": "C:/data2"}])
    assert len(rows2) == 1
    assert rows2[0]["data_folder"] == "C:/data2"
    assert db.terminals() == rows2


def test_terminal_path_lookup(db):
    real_dir = tempfile.mkdtemp()
    rows = db.sync_terminals([{"path": real_dir}])
    tid = rows[0]["id"]
    assert db.terminal_path(tid) == real_dir
    assert db.terminal_path(999999) is None


# ---------------------------------------------------------------------------
# accounts
# ---------------------------------------------------------------------------
def test_upsert_account_insert_then_update(db):
    item = {"login": 1001, "server": "Demo-1", "password": "pw1",
            "symbols": ["EURUSD", "USDJPY"]}
    aid = db.upsert_account(item)
    got = db.account(1001, "Demo-1")
    assert got is not None
    assert got["id"] == aid
    assert got["password"] == "pw1"
    assert got["symbols"] == ["EURUSD", "USDJPY"]

    # 同 (login, server) 再 upsert：更新而非新增
    aid2 = db.upsert_account({"login": 1001, "server": "Demo-1",
                              "password": "pw2", "terminal_id": 7,
                              "symbols": ["XAUUSD"]})
    assert aid2 == aid
    assert len(db.accounts()) == 1
    got2 = db.account(1001, "Demo-1")
    assert got2["password"] == "pw2"
    assert got2["terminal_id"] == 7
    assert got2["symbols"] == ["XAUUSD"]


def test_upsert_account_symbols_empty_default(db):
    db.upsert_account({"login": 1, "server": "S", "password": "p"})
    got = db.account(1, "S")
    assert got["symbols"] == []


def test_account_missing_returns_none(db):
    assert db.account(42, "Nope") is None


# ---------------------------------------------------------------------------
# swap 快照 + last_swap_time
# ---------------------------------------------------------------------------
def test_last_swap_time_and_change_only_writes(db):
    items = [{"symbol": "EURUSD", "swap_long": -0.7, "swap_short": 0.3,
              "rollover3days": 3}]
    n1 = db.insert_swap_changes("1001@Demo", items, "2024-01-01T00:00:00")
    assert n1 == 1
    assert db.last_swap_time("1001@Demo", "EURUSD") == "2024-01-01T00:00:00"
    assert db.last_swap_time("1001@Demo", "XAUUSD") is None

    # 相同快照：不再入库
    n2 = db.insert_swap_changes("1001@Demo", items, "2024-01-02T00:00:00")
    assert n2 == 0
    assert db.last_swap_time("1001@Demo", "EURUSD") == "2024-01-01T00:00:00"

    # 数值变化：入库并刷新 last_swap_time
    items2 = [{"symbol": "EURUSD", "swap_long": -0.9, "swap_short": 0.3,
               "rollover3days": 3}]
    n3 = db.insert_swap_changes("1001@Demo", items2, "2024-01-03T00:00:00")
    assert n3 == 1
    assert db.last_swap_time("1001@Demo", "EURUSD") == "2024-01-03T00:00:00"


# ---------------------------------------------------------------------------
# trades / spread_points 冒烟
# ---------------------------------------------------------------------------
def test_insert_trade_and_query(db):
    db.insert_trade("k@S", "2024-01-01T00:00:00", "paper", "open",
                    "strat-1", [{"symbol": "EURUSD", "side": "buy"}],
                    {"z": 2.3})
    rows = db.trades("k@S")
    assert len(rows) == 1
    assert rows[0]["action"] == "open"
    assert rows[0]["strategy_id"] == "strat-1"


def test_insert_spread_point(db):
    db.insert_spread_point("k@S", "strat-1", "2024-01-01T00:00:00",
                           0.5, 2.1, 0.1, 1.9, -1.7,
                           [{"symbol": "EURUSD", "price": 1.08}])
    row = db._one(
        "SELECT * FROM spread_points WHERE account_key=? AND strategy_id=?",
        ("k@S", "strat-1"),
    )
    assert row is not None
    assert row["spread"] == 0.5 and row["zscore"] == 2.1


# ---------------------------------------------------------------------------
# close
# ---------------------------------------------------------------------------
def test_close_releases_connection(tmp_path):
    instance = Database(Path(tmp_path) / "close_me.db")
    instance.close()
    # 关闭后再执行任何查询都应抛 ProgrammingError（连接已关闭）
    try:
        instance.terminals()
    except sqlite3.ProgrammingError:
        pass
    else:
        raise AssertionError("关闭后 terminals() 应抛 sqlite3.ProgrammingError")
