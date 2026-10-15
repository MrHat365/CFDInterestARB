"""Review regression source; not executed during the macOS static-only repair."""
import datetime as dt
import json
import queue
import subprocess
import threading
from types import SimpleNamespace as NS

import pytest

from mt5_trader.account_session import AccountSession
from mt5_trader.broker_clock import BrokerClock
from mt5_trader.connection_state import Phase
from mt5_trader.constants import EVENT_STATUS, EVENT_TICK, EVENT_LOG
from mt5_trader.database import Database
from mt5_trader.event_engine import Event, EventEngine
from mt5_trader.event_queue import EventQueue
from mt5_trader.minute_statistics import MinuteStatistics
from mt5_trader.series_data import quotes_ready
from mt5_trader.session_registry import SessionRegistry
from mt5_trader.worker_health import WorkerHealth


@pytest.mark.parametrize('zone,offset', [('UTC', 0), ('UTC+02:00', 7200),
    ('UTC+03:00', 10800), ('UTC+05:30', 19800), ('UTC-04:00', -14400)])
def test_explicit_clock_normalizes_history_and_ticks(zone, offset):
    now = 1800000000.125
    clock = BrokerClock(zone)
    raw = now + offset
    tick = clock.tick({'time': int(raw), 'time_msc': round(raw * 1000)})
    assert tick['time_msc'] / 1000 == pytest.approx(now, rel=0, abs=1e-6)
    assert clock.to_utc(raw) == pytest.approx(now, rel=0, abs=1e-6)


@pytest.mark.parametrize('age', [1800, 3480, 10800])
def test_stale_quotes_never_recalibrate_clock(age):
    clock = BrokerClock()
    now = 1800000000.
    tick = clock.tick({'time': now - age, 'bid': 1., 'ask': 1.1})
    tick['received_at'] = now
    assert tick['time'] == now - age
    assert not quotes_ready([{'symbol': 'A'}], {'A': tick}, now)
    assert clock.metadata()['server_utc_offset'] == 0


def test_iana_clock_uses_historical_dst_and_rejects_ambiguous_wall_times():
    clock = BrokerClock('Europe/Helsinki')
    def stamp(month, day=15, hour=12):
        return dt.datetime(2026, month, day, hour, tzinfo=dt.timezone.utc).timestamp()
    assert clock.to_utc(stamp(1)) == stamp(1) - 7200
    assert clock.to_utc(stamp(7)) == stamp(7) - 10800
    for value in (stamp(3, 29, 3), stamp(10, 25, 3)):
        with pytest.raises(ValueError, match='重复/缺失'):
            clock.to_utc(value)


@pytest.mark.parametrize('zone', ['UTC+14:01', 'UTC+03:60', 'not/a/timezone'])
def test_invalid_clock_configuration_is_rejected(zone):
    with pytest.raises(ValueError):
        BrokerClock(zone)


def test_quiet_markets_use_read_time_for_cross_leg_freshness():
    ticks = {'A': {'bid': 1, 'ask': 1.1, 'time': 95, 'received_at': 100},
             'B': {'bid': 1, 'ask': 1.1, 'time': 70, 'received_at': 100}}
    legs = [{'symbol': key} for key in ticks]
    assert quotes_ready(legs, ticks, 100)
    ticks['B']['received_at'] = 98
    assert not quotes_ready(legs, ticks, 100)
    ticks['B']['received_at'] = 100
    ticks['B']['quote_max_age'] = 'invalid'
    assert not quotes_ready(legs, ticks, 100)


def test_m1_statistics_ignore_poll_frequency_and_unclosed_minute():
    prior = {'mean': 1., 'std': .02}
    slow, fast = MinuteStatistics(10), MinuteStatistics(10)
    for minute in range(11):
        slow.append(1800000000 + minute * 60, 1.)
        for second in range(60):
            fast.append(1800000000 + minute * 60 + second, 1.)
    assert slow.stats(1., prior) == fast.stats(1., prior)
    stats = fast.stats(1., prior)
    assert stats['n_points'] == 10
    assert stats['std'] == pytest.approx(.002)
    assert not stats['prior']
    fast.append(1800000000 + 11 * 60 - 1, 100.)
    assert fast.stats(1., prior) == stats


def test_m1_window_excludes_gaps_without_fabricating_bars():
    series = MinuteStatistics(10)
    for i in range(12):
        series.append(1800000000 + i * 60, i)
    series.append(1800000000 + 40 * 60, 40)
    assert series.stats(40, {'mean': 1., 'std': 2.})['n_points'] == 0


def test_m1_refresh_preserves_live_close(monkeypatch):
    monkeypatch.setattr('mt5_trader.minute_statistics.time.time', lambda: 1800000061)
    series = MinuteStatistics(10)
    series.append(1800000005, 3)
    series.append(1800000061, 4)
    series.seed({'t': [1800000000, 1800000060], 'v': [2, 99]})
    series.append(1800000120, 5)
    assert series.stats(5, {}) == {}  # only two closed minutes, no prior
    assert series.points[1800000060 // 60] == 4


def test_login_wait_advances_health_but_blocked_api_does_not(monkeypatch):
    now = [0.]
    monkeypatch.setattr('mt5_trader.worker_health.time.monotonic', lambda: now[0])
    health = WorkerHealth()
    for second in range(0, 601, 10):
        now[0] = second
        health.mark('initialize')
        assert health.snapshot()['healthy']
    now[0] += 121
    assert health.snapshot()['stalled'] == ['initialize']


def test_unbound_accounts_never_spawn_workers(monkeypatch):
    calls = []
    monkeypatch.setattr(AccountSession, 'start', lambda self: calls.append(self))
    registry = SessionRegistry(EventEngine())
    for login in (1, 2):
        with pytest.raises(ValueError, match='先绑定终端'):
            registry.connect({'login': login, 'server': 'demo'})
    assert not calls


def test_stop_and_late_status_share_atomic_state_transition():
    session = AccountSession(EventEngine(), {'login': 1, 'server': 'demo'})
    session._transition(Phase.CONNECTING, 'start')
    session._thread = NS(is_alive=lambda: False)
    errors = []
    gate = threading.Barrier(2)
    def reply():
        gate.wait()
        try:
            session._rep(EVENT_STATUS, {'ok': True})
        except Exception as exc:
            errors.append(exc)
    reader = threading.Thread(target=reply)
    reader.start()
    gate.wait()
    session.begin_stop()
    reader.join(2)
    assert not reader.is_alive() and not errors
    assert session.state.phase == Phase.STOPPING and session._cancel.is_set()
    session._rep(EVENT_STATUS, {'ok': True})
    assert session.state.phase == Phase.STOPPING


def test_failed_owned_worker_can_be_reclaimed_before_reconnect(monkeypatch):
    class Process:
        pid = 123
        dead = False
        def poll(self):
            return 0 if self.dead else None
        def terminate(self):
            pass
        def wait(self, timeout):
            if not self.dead:
                raise subprocess.TimeoutExpired('owned-worker', timeout)
            return 0
        def kill(self):
            self.dead = True
    monkeypatch.setattr(AccountSession, 'start', lambda self: self._transition(Phase.CONNECTING, 'start'))
    registry = SessionRegistry(EventEngine())
    account = {'login': 1, 'server': 'demo', 'terminal_path': '/owned'}
    session = registry.connect(account)
    session._thread = NS(is_alive=lambda: False)
    session._proc = Process()
    session._fail('cleanup failed')
    assert registry.connect(account) is session
    assert session.reclaim()
    assert session.closed.is_set() and session.state.phase == Phase.DISCONNECTED
    assert registry.connect(account) is not session


def test_ping_precedes_full_user_queue():
    session = AccountSession(EventEngine(), {'login': 1, 'server': 'demo'})
    sent = []
    def send(data):
        sent.append(json.loads(data))
        session._cancel.set()
    session._conn = NS(sendall=send)
    for _ in range(session._req_q.maxsize):
        session._req_q.put_nowait({'action': 'rules'})
    session._ping_pending.set()
    session._writer()
    assert sent == [{'action': 'ping'}, {'action': 'stop'}]


def test_swap_fingerprint_survives_worker_and_database_restart(tmp_path):
    path = tmp_path / 'swaps.db'
    item = {'symbol': 'A', 'swap_long': 1.123456789012, 'swap_short': -2., 'rollover3days': 3}
    with_db = Database(path)
    assert with_db.insert_swap_changes('1@demo', [item], '2026-10-15T00:00:00+00:00') == 1
    with_db.close()
    reopened = Database(path)
    try:
        assert reopened.insert_swap_changes('1@demo', [item], '2026-10-15T01:00:00+00:00') == 0
        assert reopened.insert_swap_changes('1@demo', [{**item, 'swap_long': 2.}], '2026-10-15T02:00:00+00:00') == 1
    finally:
        reopened.close()


def test_indexed_queue_preserves_ack_order_and_task_accounting():
    events = EventQueue(3)
    def tick(value):
        return Event(EVENT_TICK, {'session': 'a', 'generation': 'g', 'payload': value})
    ack = Event(EVENT_LOG, {'payload': 'ack'})
    events.put(tick(1))
    events.put(ack)
    events.put(tick(2))
    assert events.qsize() == 2 and events.dropped == 1
    assert events.get_nowait() is ack
    events.task_done()
    assert events.get_nowait().data['payload'] == 2
    events.task_done()
    assert events.unfinished_tasks == 0
    with pytest.raises(queue.Empty):
        events.get_nowait()


def test_account_clock_config_survives_rebinding(db, tmp_path):
    from mt5_trader.accounts import AccountsStore
    store = AccountsStore(db)
    store.add({'login': 1, 'server': 'demo', 'broker_timezone': 'UTC+05:30', 'quote_max_age': 90})
    terminal = tmp_path / 'terminal64.exe'
    terminal.touch()
    terminal_id = db.sync_terminals([{'path': str(terminal), 'name': 'test', 'data_folder': ''}])[0]['id']
    store.rebind_terminal(1, 'demo', terminal_id)
    account = store.get(1, 'demo')
    config = AccountSession(EventEngine(), account)._worker_config()
    assert config['broker_timezone'] == 'UTC+05:30'
    assert config['quote_max_age'] == 90
