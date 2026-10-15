"""账户与终端绑定管理（SQLite-backed，原 JSON 存储已废弃）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .database import Database


class AccountsStore:
    def __init__(self, db: Database) -> None:
        self._db = db

    # ------------------------------------------------------------------
    def all(self) -> list[dict]:
        """[{login, server, password, terminal_id, symbols, terminal_*}]"""
        out = []
        all_t = {t["id"]: t for t in self._db.terminals()}
        for row in self._db.accounts():
            row = dict(row)
            t = all_t.get(row.get("terminal_id"))
            row["terminal_path"] = t.get("path") if t else None
            row["terminal_data"] = t.get("data_folder", "") if t else ""
            row["terminal_name"] = (
                (t.get("path") or "(仅数据目录绑定)") if t else "(未绑定终端)"
            )
            out.append(row)
        return out

    def get(self, login: Any, server: str) -> dict | None:
        for row in self.all():
            if int(row["login"]) == int(login) and row["server"] == server:
                return row
        return None

    def add(self, item: dict) -> int:
        item = dict(item)
        terminal_id = item.get("terminal_id")
        terminal_path = item.get("terminal_path")
        # 浏览产生的未入库路径：先入库 terminals 再绑定
        if terminal_id is None and terminal_path:
            cached = {
                t["path"]: t["id"] for t in self._db.terminals()
            }
            if terminal_path in cached:
                terminal_id = cached[terminal_path]
            else:
                rows = self._db.sync_terminals([
                    {
                        "path": terminal_path,
                        "data_folder": item.get("terminal_data", ""),
                        "name": Path(terminal_path).parent.name,
                    }
                ])
                terminal_id = next(
                    t["id"] for t in rows if t["path"] == terminal_path
                )
        item = {
            "login": item["login"],
            "password": item.get("password", ""),
            "server": item.get("server", ""),
            "symbols": item.get("symbols", []),
            "terminal_id": terminal_id,
            "broker_timezone": item.get('broker_timezone', 'UTC'),
            "quote_max_age": item.get('quote_max_age', 60),
        }
        return self._db.upsert_account(item)

    def remove(self, login: Any, server: str) -> None:
        self._db.delete_account(int(login), server)

    # ------------------------------------------------------------------
    def terminals(self) -> list[dict]:
        return self._db.terminals()

    def rebind_terminal(self, login: Any, server: str, terminal_id: int | None) -> None:
        record = self.get(login, server)
        if record is None:
            return
        self._db.upsert_account({
            **record,
            "symbols": record.get("symbols", []),
            "terminal_id": terminal_id,
        })

    def rescan_terminals(self, deep: bool = False, should_cancel=None) -> list[dict]:
        from .terminals import scan

        return self._db.sync_terminals(scan(deep=deep, should_cancel=should_cancel))

    def sync_terminals(self, items: list[dict]) -> list[dict]:
        return self._db.sync_terminals(items)
