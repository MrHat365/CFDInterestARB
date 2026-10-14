"""事件总线上的持久化服务：swap 快照 -> SQLite（变化才存）。"""

from __future__ import annotations

from ..constants import EVENT_SWAPS
from ..database import Database


class SwapSnapshotService:
    """订阅 EVENT_SWAPS，把 changed 品种写入 swap_snapshots。
    由此得到自建"更新时间"：变更入库的 fetched_at 即生效时刻。
    """

    def __init__(self, db: Database) -> None:
        self._db = db

    def start(self, engine) -> None:
        engine.register(EVENT_SWAPS, self._on_swaps)

    def _on_swaps(self, event) -> None:
        payload = event.data.get("payload", {})
        session = event.data.get("session", "")
        changed = payload.get("changed") or []
        fetched = payload.get("fetched_at", "")
        if changed and fetched:
            try:
                self._db.insert_swap_changes(session, changed, fetched)
            except Exception as exc:  # 不允许打穿事件线程
                import traceback

                traceback.print_exc()
                _ = exc
