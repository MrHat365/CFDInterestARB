"""Protocol/lifecycle tests; sockets are local pairs and never connect to MT5."""
import json
import socket
import threading

from mt5_trader.account_session import AccountSession
from mt5_trader.connection_state import Phase
from mt5_trader.constants import EVENT_ORDER_REQUEST, EVENT_STATUS
from mt5_trader.constants import EVENT_AUTOMATION_STATE
from mt5_trader.event_queue import EventQueue
from mt5_trader.event_engine import Event, EventEngine


def test_writer_delivers_stop_before_closing_transport():
    session = AccountSession(EventEngine(), {"login": 1, "server": "A"})
    parent, child = socket.socketpair()
    child.settimeout(2)
    session._conn = parent
    writer = threading.Thread(target=session._writer)
    writer.start()
    session._cancel.set()
    assert json.loads(child.makefile("r").readline()) == {"action": "stop"}
    writer.join(2)
    assert not writer.is_alive()
    parent.close()
    child.close()


def test_reader_eof_fails_once_and_rejects_late_connected():
    engine = EventEngine()
    session = AccountSession(engine, {"login": 1, "server": "A"})
    session._transition(Phase.CONNECTING, "start")
    parent, child = socket.socketpair()
    session._conn = parent
    child.close()
    session._reader()
    assert session.state.phase == Phase.FAILED
    revision = session.state.revision
    session._rep(EVENT_STATUS, {"ok": True})
    assert session.state.revision == revision
    parent.close()


def test_duplicate_start_does_not_register_or_spawn_twice(monkeypatch):
    starts = []
    monkeypatch.setattr(threading.Thread, "start", lambda self: starts.append(self))
    engine = EventEngine()
    session = AccountSession(engine, {"login": 1, "server": "A"})
    session.start()
    session.start()
    assert len(starts) == 1
    assert len(engine._handlers[EVENT_ORDER_REQUEST]) == 1


def test_old_generation_order_is_not_forwarded():
    session = AccountSession(EventEngine(), {"login": 1, "server": "A"})
    calls = []
    session._send_raw = lambda message: calls.append(message)
    event = Event(EVENT_ORDER_REQUEST, {"session": session.name, "generation": "old", "volume": .1})
    session._on_event(event)
    assert not calls


def test_task_acknowledgements_are_not_coalesced_with_telemetry():
    telemetry = Event(EVENT_AUTOMATION_STATE, {'session': '1@Demo', 'payload': {'tasks': []}})
    ack = Event(EVENT_AUTOMATION_STATE, {'session': '1@Demo', 'payload': {'request_id': 'x', 'tasks': []}})
    assert EventQueue.key(telemetry) is not None
    assert EventQueue.key(ack) is None
