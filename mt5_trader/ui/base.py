"""EventEngine to Qt bridge; high-rate snapshots have one queued wakeup."""
from __future__ import annotations

import threading
from PySide6 import QtCore

from ..event_engine import Event, EventEngine
from ..event_queue import EventQueue


class EventBridge(QtCore.QObject):
    event_received = QtCore.Signal(object)
    _wake = QtCore.Signal()

    def __init__(self, event_engine: EventEngine, event_type: str) -> None:
        super().__init__()
        self.event_type = event_type
        self._engine = event_engine
        self._alive = True
        self._lock = threading.Lock()
        self._pending = {}
        self._scheduled = False
        self._wake.connect(self._flush, QtCore.Qt.ConnectionType.QueuedConnection)
        self._handler = event_engine.register(event_type, self._on_event)

    def destroy(self) -> None:
        with self._lock:
            if not self._alive:
                return
            self._alive = False
            self._pending.clear()
        self._engine.unregister(self.event_type, self._on_event)
        self.event_received.disconnect()

    @QtCore.Slot()
    def _flush(self) -> None:
        with self._lock:
            events = tuple(self._pending.values()) if self._alive else ()
            self._pending.clear()
            self._scheduled = False
        for event in events:
            if self._alive:
                self.event_received.emit(event)

    def _on_event(self, event: Event) -> None:
        key = EventQueue.key(event)
        with self._lock:
            if not self._alive:
                return
            if key is not None:
                # A new generation replaces the old generation for this account.
                # Never aggregate different accounts into a single snapshot.
                self._pending[key[:2]] = event
                if self._scheduled:
                    return
                self._scheduled = True
        try:
            if key is None:
                self.event_received.emit(event)  # critical events are never coalesced
            else:
                self._wake.emit()
        except RuntimeError:
            self._engine.unregister(self.event_type, self._on_event)
            with self._lock:
                self._alive = False
                self._pending.clear()
