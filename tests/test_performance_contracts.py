"""Source-only additions for future Windows validation; not executed locally."""
import json
import math
import queue
import socket
import sqlite3

from mt5_trader.account_session import AccountSession
from mt5_trader.connection_state import Phase
from mt5_trader.constants import EVENT_TICK, EVENT_ORDER_RESULT, EVENT_AUTOMATION_STATE
from mt5_trader.event_engine import Event, EventEngine
from mt5_trader.event_queue import EventQueue
from mt5_trader.managed_trading import ExecutionLedger
from mt5_trader.series_data import SeriesBuffer, peak_envelope
from mt5_trader.worker_health import WorkerHealth
from mt5_trader.worker_pipe import drain_replies


def test_snapshots_coalesce_before_saturation_and_follow_critical_event():
    q = EventQueue(5000)
    q.put(Event(EVENT_TICK, {'session': 'a', 'generation': 'g', 'payload': [1]}))
    ack = Event(EVENT_ORDER_RESULT, {'session': 'a', 'payload': {'request_id': 'x'}})
    q.put(ack)
    q.put(Event(EVENT_TICK, {'session': 'a', 'generation': 'g', 'payload': [2]}))
    assert q.qsize() == 2
    assert q.get_nowait() == ack
    q.task_done()
    assert q.get_nowait().data['payload'] == [2]
    q.task_done()
    assert drain_replies(q, 0)


def test_snapshot_coalescing_isolates_accounts_and_generations():
    q = EventQueue(5000)
    for account, generation in [('a', 'old'), ('a', 'new'), ('b', 'new')]:
        q.put(Event(EVENT_TICK, {'session': account, 'generation': generation, 'payload': []}))
    assert q.qsize() == 3 and q.dropped == 0


def test_command_ack_is_retained_between_task_snapshots():
    q = EventQueue(5000)
    q.put({'type': EVENT_AUTOMATION_STATE, 'payload': {'tasks': [1]}})
    ack = {'type': EVENT_AUTOMATION_STATE, 'payload': {'request_id': 'x', 'tasks': [2]}}
    q.put(ack)
    q.put({'type': EVENT_AUTOMATION_STATE, 'payload': {'tasks': [3]}})
    assert q.qsize() == 2
    assert q.get_nowait() == ack
    assert q.get_nowait()['payload']['tasks'] == [3]


def test_bounded_drain_does_not_claim_unacknowledged_delivery():
    q = queue.Queue()
    q.put('report')
    assert not drain_replies(q, 0)
    q.get_nowait()
    assert not drain_replies(q, 0)  # Dequeued is not delivered.
    q.task_done()
    assert drain_replies(q, 0)


def test_buffer_scalar_validation_preserves_last_valid_value():
    buf = SeriesBuffer(3)
    assert buf.append('1', '2')
    for stamp, value in [(math.nan, 1), (2, math.inf), (-1, 1), (1e12, 1), (2, 'bad')]:
        assert not buf.append(stamp, value)
    assert buf.append(1, 3)
    assert not buf.append(0, 4)
    for i in range(2, 15):
        assert buf.append(i, i * 2)
    assert buf.times.tolist() == [12, 13, 14]
    assert buf.values.tolist() == [24, 26, 28]


def test_existing_ledger_gets_active_index_without_losing_history(tmp_path):
    path = tmp_path / 'legacy.db'
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE managed_batches(account TEXT, batch_id TEXT, body TEXT, '
                     'PRIMARY KEY(account, batch_id))')
        for account, batch_id, state in [('a', 'done', 'closed'), ('a', 'live', 'open'),
                                         ('a', 'unknown', 'blocked'), ('b', 'other', 'open')]:
            conn.execute('INSERT INTO managed_batches VALUES(?,?,?)',
                         (account, batch_id, json.dumps({'id': batch_id, 'state': state})))
        conn.execute('INSERT INTO managed_batches VALUES(?,?,?)', ('a', 'missing', '{"id":"missing"}'))
    ledger = ExecutionLedger(str(path), 'a')
    assert [b['id'] for b in ledger.batches(active_only=True)] == ['live', 'unknown', 'missing']
    assert len(ledger.batches()) == 4
    ledger.save({'id': 'live', 'state': 'closed'})
    assert [b['id'] for b in ledger.batches(active_only=True)] == ['unknown', 'missing']
    assert len(ledger.batches()) == 4


def test_health_detects_stalled_orders_despite_poll_progress(monkeypatch):
    clock = [0.]
    monkeypatch.setattr('mt5_trader.worker_health.time.monotonic', lambda: clock[0])
    health = WorkerHealth()
    health.ready()
    clock[0] = 61.
    health.mark('poll')
    report = health.snapshot()
    assert not report['healthy'] and report['stalled'] == ['orders']
    health.mark('orders')
    assert health.snapshot()['healthy']


def test_health_startup_has_separate_deadline(monkeypatch):
    clock = [0.]
    monkeypatch.setattr('mt5_trader.worker_health.time.monotonic', lambda: clock[0])
    health = WorkerHealth()
    clock[0] = 119.
    assert health.snapshot()['healthy']
    clock[0] = 121.
    assert health.snapshot()['stalled'] == ['initialize']


def test_healthy_transport_cannot_hide_stalled_execution():
    session = AccountSession(EventEngine(), {'login': 1, 'server': 'Demo'})
    session._transition(Phase.CONNECTING, 'connecting')
    session._transition(Phase.CONNECTED, 'connected')
    parent, child = socket.socketpair()
    try:
        session._conn = parent
        child.sendall(b'{"type":"ePong","payload":{"healthy":false,"stalled":["orders"]}}\n')
        child.shutdown(socket.SHUT_WR)
        session._reader()
        assert session.state.phase == Phase.FAILED
        assert session._cancel.is_set()
    finally:
        parent.close()
        child.close()


def test_irregular_time_envelope_preserves_extrema_timestamps_and_latest():
    import numpy as np
    times = np.concatenate((np.arange(100.), np.linspace(100., 100.001, 10001)))
    values = np.zeros_like(times)
    values[150], values[250], values[-1] = 123., -456., 3.
    t, v = peak_envelope(times, values, 0., 101., 20)
    assert len(t) <= 42
    assert t[0] == times[0] and t[-1] == times[-1] and v[-1] == 3.
    assert t[np.argmax(v)] == times[150] and t[np.argmin(v)] == times[250]
    assert np.all(np.diff(t) > 0)
    assert len(times) == 10101 and values[150] == 123.  # Raw data unmodified.


def test_envelope_small_series_keeps_all_samples():
    import numpy as np
    times, values = np.array([1., 2., 3.]), np.array([2., 1., 2.])
    t, v = peak_envelope(times, values, 1., 3., 800)
    assert np.array_equal(t, times) and np.array_equal(v, values)
