"""Static-audit regression suite. Written for Windows execution; not run in this audit."""
import math
import queue

import numpy as np
import pytest

from mt5_trader.account_session import AccountSession
from mt5_trader.connection_state import ConnectionState, Phase
from mt5_trader.constants import EVENT_STATUS, EVENT_TICK, EVENT_ORDER_RESULT
from mt5_trader.event_engine import Event, EventEngine
from mt5_trader.event_queue import EventQueue
from mt5_trader.series_data import SeriesBuffer, normalize_series, quotes_ready
from mt5_trader.session_registry import SessionRegistry
from mt5_trader.strategy import SignalEngine, SpreadSeries, StrategyRunner
from mt5_trader.weights import swap_to_usd_per_day


def test_lifecycle_reconnect_and_illegal_late_connected():
    state = ConnectionState()
    for phase in (Phase.CONNECTING, Phase.CONNECTED, Phase.RECONNECTING,
                  Phase.CONNECTED, Phase.STOPPING, Phase.DISCONNECTED):
        state = state.transition(phase, phase.value)
    assert state.revision == 6
    with pytest.raises(ValueError):
        state.transition(Phase.CONNECTED, "late callback")


def test_registry_reuses_active_session_and_isolates_accounts(monkeypatch):
    starts = []
    monkeypatch.setattr(AccountSession, "start", lambda self: starts.append(self.name))
    registry = SessionRegistry(EventEngine())
    first = registry.connect({"login": 1, "server": "A", "terminal_path": "/one"})
    assert registry.connect(first.account) is first
    second = registry.connect({"login": 2, "server": "A", "terminal_path": "/two"})
    assert len(starts) == 2 and first is not second
    assert registry.is_current(first.status_event())
    assert not registry.is_current(Event(EVENT_STATUS, {"session": first.name, "generation": "old"}))
    with pytest.raises(ValueError):
        registry.connect({"login": 3, "server": "A", "terminal_path": "/one"})


def test_registry_new_generation_after_shutdown(monkeypatch):
    monkeypatch.setattr(AccountSession, "start", lambda self: None)
    registry = SessionRegistry(EventEngine())
    first = registry.connect({"login": 1, "server": "A", "terminal_path": "/one"})
    stale = first.status_event()
    first.closed.set()
    second = registry.connect(first.account)
    assert first.generation != second.generation
    assert not registry.is_current(stale)
    assert SessionRegistry(EventEngine()).get(first.name) is None  # restart never restores connectivity


def test_password_not_sent_to_worker():
    session = AccountSession(EventEngine(), {"login": 1, "server": "A", "password": "private"})
    assert "password" not in session._worker_config()


def test_late_status_cannot_revive_stopping_session():
    session = AccountSession(EventEngine(), {"login": 1, "server": "A"})
    session._transition(Phase.CONNECTING, "start")
    session._transition(Phase.STOPPING, "stop")
    session._cancel.set()
    session._rep(EVENT_STATUS, {"ok": True, "msg": "late"})
    assert session.state.phase == Phase.STOPPING


def test_queue_preserves_order_result_when_tick_floods():
    q = EventQueue(maxsize=2)
    result = Event(EVENT_ORDER_RESULT, {"session": "one", "payload": {"ok": True}})
    q.put(result)
    q.put(Event(EVENT_TICK, {"session": "one", "payload": [1]}))
    q.put(Event(EVENT_TICK, {"session": "one", "payload": [2]}))
    assert q.get_nowait() == result
    assert q.get_nowait().data["payload"] == [2]
    assert q.dropped == 1


def test_queue_critical_overload_is_explicit_not_silent():
    q = EventQueue(maxsize=1)
    q.put({"type": EVENT_ORDER_RESULT, "payload": 1})
    with pytest.raises(queue.Full):
        q.put_nowait({"type": EVENT_STATUS, "payload": 2})
    assert q.get_nowait()["payload"] == 1


def test_history_normalization_pairs_sort_deduplicate_and_filter():
    t, v = normalize_series([3, 1, 2, 2, math.nan, 4, 5], [30, 10, 20, 25, 1, math.inf, "bad"])
    assert t.tolist() == [1, 2, 3]
    assert v.tolist() == [10, 25, 30]
    with pytest.raises(ValueError):
        normalize_series([1], [])


def test_buffer_rejects_old_live_point_instead_of_corrupting_last():
    b = SeriesBuffer(4)
    b.replace([1, 2], [10, 20])
    assert not b.append(1, 999)
    assert b.values.tolist() == [10, 20]
    assert b.append(2, 21)
    assert b.values.tolist() == [10, 21]
    assert not b.append(math.inf, 5)


def test_buffer_is_bounded_through_many_compactions():
    b = SeriesBuffer(32)
    for i in range(2000):
        b.append(i, -i)
    assert b.times.tolist() == list(range(1968, 2000))
    assert b.values.tolist() == list(range(-1968, -2000, -1))
    assert len(b._times) == 64


def test_history_refresh_keeps_newer_live_points():
    b = SeriesBuffer(10)
    b.replace([1, 2, 3, 4], [10, 20, 30, 40])
    b.replace([1, 2, 3], [11, 21, 31], preserve_live=True)
    assert b.values.tolist() == [11, 21, 31, 40]
    b.clear()  # switching instruments resets the merge boundary
    b.replace([1, 2], [100, 200], preserve_live=True)
    assert b.values.tolist() == [100, 200]


def test_large_history_capacity_and_millisecond_rejection():
    b = SeriesBuffer()
    b.replace(np.arange(250000), np.arange(250000))
    assert len(b.times) == 200000 and b.times[0] == 50000
    assert not b.append(1800000000000, 1)


def test_quote_freshness_checks_each_leg_and_timestamp_skew():
    legs = [{"symbol": "A"}, {"symbol": "B"}]
    ticks = {sym: {"bid": 1, "ask": 1.1, "time": ts}
             for sym, ts in (("A", 100), ("B", 99))}
    assert quotes_ready(legs, ticks, 101)
    assert not quotes_ready(legs, ticks, 170)
    ticks["B"]["time"] = 90
    assert not quotes_ready(legs, ticks, 101)
    ticks["B"]["time"] = 100
    ticks["B"]["ask"] = math.nan
    assert not quotes_ready(legs, ticks, 101)


def test_history_replacement_clears_stale_prior():
    series = SpreadSeries()
    series.init_prior({"t": [1, 2], "v": [1, 3]})
    series.init_prior({"t": [], "v": []})
    assert series.prior() == {} and series.arrays() == ([], [])


def test_live_statistics_do_not_mix_h1_prior_with_ticks():
    series = SpreadSeries(window=500)
    series.init_prior({"t": list(range(100)), "v": [1000] * 100})
    for i in range(10):
        series.append(100 + i, float(i))
    assert series.stats()["mean"] == 4.5


def test_chart_only_ticks_do_not_consume_signal_layers():
    runner = StrategyRunner("s", [{"symbol": "A", "side": "sell"},
                                  {"symbol": "B", "side": "buy"}], {})
    runner.set_history({"t": [1, 2, 3], "v": [0, 1, 2]})
    result = runner.on_tick({"A": {"bid": 10, "ask": 11, "time": 1800000000},
                             "B": {"bid": 1, "ask": 2, "time": 1800000000}},
                            emit_signal=False)
    assert result["signal"] is None
    assert runner.signal.open_count() == 0


def test_grid_requires_distinct_thresholds():
    engine = SignalEngine("s", k=2, grid_step=.5, levels=3)
    stats = {"mean": 0, "std": 1, "z": 2.1, "last": 2.1}
    assert engine.on_spread(stats, 1)["count"] == 1
    assert engine.on_spread(stats, 2) is None
    stats.update(z=2.6, last=2.6)
    assert engine.on_spread(stats, 3)["level"] == 2.5
    engine.reset_direction("sell")
    assert engine.open_count() == 0


@pytest.mark.parametrize("mode,currency,expected", [(2, "EUR", 2.4), (3, "JPY", .02), (4, "GBP", 2.6)])
def test_swap_currency_enum(mode, currency, expected):
    info = {"swap_long": 2, "swap_mode": mode,
            "currency_base": "EUR", "currency_margin": "JPY"}
    assert swap_to_usd_per_day(info, "buy", {"EUR": 1.2, "JPY": .01, "GBP": 1.3}, "GBP") == pytest.approx(expected)


def test_points_use_deposit_currency_and_reopen_is_not_interest():
    info = {"swap_long": 2, "swap_mode": 1, "point": .01, "tick_size": .01,
            "tick_value": 3, "currency_profit": "JPY"}
    assert swap_to_usd_per_day(info, "buy", {"EUR": 1.2, "JPY": .01}, "EUR") == pytest.approx(7.2)
    for mode in (6, 7, 8):
        assert swap_to_usd_per_day({**info, "swap_mode": mode}, "buy", {}) is None


def test_scan_preserves_missing_bound_terminal(db, tmp_path):
    terminal = tmp_path / "terminal64.exe"
    terminal.touch()
    tid = db.sync_terminals([{"path": str(terminal)}])[0]["id"]
    db.upsert_account({"login": 1, "server": "A", "password": "", "terminal_id": tid})
    terminal.unlink()
    db.sync_terminals([])
    assert db.terminal_path(tid) == str(terminal)
    assert db.account(1, "A")["terminal_id"] == tid


def test_execution_journal_records_reports_without_a_console(db):
    from mt5_trader.services.db_service import ExecutionJournalService
    engine = EventEngine()
    journal = ExecutionJournalService(db)
    journal.start(engine)
    engine._dispatch(Event(EVENT_ORDER_RESULT, {"session": "1@Demo", "generation": "g1",
        "payload": {"ok": True, "request": {"algoid": "algo-1"}, "result": {"volume": .1}}}))
    rows = db.trades("1@Demo")
    assert len(rows) == 1 and rows[0]["action"] == "execution"
    assert rows[0]["strategy_id"] == "algo-1"
    journal.stop(engine)
    assert EVENT_ORDER_RESULT not in engine._handlers


def test_contract_sizes_change_neutral_lot_weights():
    from mt5_trader.weights import usd_neutral_weights
    legs = [{'symbol': 'EURUSD.a', 'side': 'buy', 'contract_size': 100000},
            {'symbol': 'EURUSD.b', 'side': 'sell', 'contract_size': 10000}]
    weights = usd_neutral_weights(legs, {'EUR': 1.1, 'USD': 1.0})
    assert weights == pytest.approx([.1, 1.])


def test_signal_restore_counts_does_not_confuse_intents_with_fills():
    from mt5_trader.strategy import SignalEngine
    signal = SignalEngine('strategy')
    signal.restore_counts({'buy': 2, 'sell': 0})
    assert signal.open_count('buy') == 2
    signal.restore_counts({})
    assert signal.open_count() == 0
