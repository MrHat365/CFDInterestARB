"""Real Qt widgets with injected sessions. No MT5 terminal or real orders."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6 import QtCore, QtWidgets

from mt5_trader.accounts import AccountsStore
from mt5_trader.account_session import AccountSession
from mt5_trader.connection_state import Phase
from mt5_trader.constants import EVENT_SPREAD_HISTORY, EVENT_STATUS
from mt5_trader.event_engine import Event, EventEngine
from mt5_trader.session_registry import SessionRegistry
from mt5_trader.ui.accounts_view import AccountsView
from mt5_trader.ui.chart import SpreadChart
from mt5_trader.ui.console import AccountConsole
from mt5_trader.ui.automation import AutomationPanel


@pytest.fixture(scope="module")
def app():
    instance = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield instance


@pytest.fixture
def chart(app):
    widget = SpreadChart()
    yield widget
    widget.close()
    widget.deleteLater()
    app.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)


def test_chart_empty_history_live_and_clear(chart):
    chart.set_series([], [])
    assert not chart._line_upper.isVisible()
    chart.set_series([3, 1, 2, 2], [30, 10, 20, 22])
    chart.append_point(1, 99)
    chart.append_point(4, 40)
    chart._redraw_now()
    assert chart._buf_v.tolist() == [10, 22, 30, 40]
    chart.set_thresholds(25, 5, 2, .5, 3)
    chart.set_extremes({"max_1y": 100, "min_1y": 0})
    chart.clear_series()
    assert not chart._line_upper.isVisible()
    assert not chart._redraw_timer.isActive()
    chart.set_series([1, 2], [10, 20])
    chart.set_thresholds(25, 5, 2, .5, 3)
    chart.set_extremes({"max_1y": 100, "min_1y": 0})
    assert chart._line_upper.isVisible() and chart._extreme_up.isVisible()


def test_background_controls_wait_for_ack_and_toggle_only_selected_side(app):
    sent = []
    panel = AutomationPanel(lambda msg: sent.append(msg) or True, lambda: {})
    panel.set_available(True)
    panel.update_state({'request_id': sent[-1]['request_id'], 'tasks': [], 'ts': 1.})
    task = {'id': 'one', 'name': 'one', 'legs': [], 'entry_enabled': False,
            'exit_enabled': True, 'target_layers': 2, 'revision': 3, 'runtime': {}}
    panel.update_state({'tasks': [task], 'ts': 2.})
    panel.table.selectRow(0)
    panel._control('entry_enabled')
    assert sent[-1]['field'] == 'entry_enabled' and sent[-1]['enabled'] is True
    assert panel._tasks['one']['entry_enabled'] is False  # no optimistic trading state
    assert panel._tasks['one']['exit_enabled'] is True
    panel.update_state({'request_id': sent[-1]['request_id'], 'tasks': [{**task, 'entry_enabled': True}], 'ts': 3.})
    panel.update_state({'tasks': [task], 'ts': 2.5})
    assert panel._tasks['one']['entry_enabled'] is True  # late snapshots cannot undo ack
    panel.deleteLater()
    app.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)


def test_closing_task_panel_does_not_pause_worker_tasks(app):
    sent = []
    panel = AutomationPanel(lambda msg: sent.append(msg) or True, lambda: {})
    panel.close()
    assert sent == []
    panel.deleteLater()
    app.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)


def test_manual_pan_zoom_survives_live_redraw(chart):
    chart.set_series([100, 200, 300], [1, 2, 3])
    chart._vb.setRange(xRange=(120, 220), yRange=(1, 2), padding=0)
    chart._manual_range()
    before = chart._vb.viewRange()
    chart.append_point(400, 4)
    chart._redraw_now()
    assert chart._vb.viewRange() == before
    chart._resume_follow()
    assert chart._vb.viewRange()[0][1] > 400
    chart._win_combo.setCurrentIndex(0)
    assert chart._follow


def test_dense_chart_keeps_extreme_spikes_and_two_point_mean(chart, app):
    import numpy as np
    times = np.arange(1, 100001, dtype=float)
    values = np.zeros_like(times)
    values[50000], values[50001] = 100., -100.
    chart.resize(800, 400)
    chart.show()
    chart._window_sec = None
    chart.set_series(times, values)
    chart.set_thresholds(1., .2, 2., .5, 3)
    chart._redraw_now()
    app.processEvents()
    shown_t, shown_v = chart._curve_spread.getData()
    assert len(shown_t) < len(times)
    assert max(shown_v) == 100. and min(shown_v) == -100.
    mean_t, mean_v = chart._curve_mean.getData()
    assert len(mean_t) == 2 and list(mean_v) == [1., 1.]


def test_task_refresh_reuses_cells_and_keeps_selection(app):
    panel = AutomationPanel(lambda msg: True, lambda: {})
    task = {'id': 'one', 'name': 'one', 'legs': [], 'entry_enabled': False,
            'exit_enabled': True, 'target_layers': 2, 'revision': 1, 'runtime': {}}
    panel.update_state({'tasks': [task], 'ts': 1.})
    panel.table.selectRow(0)
    item = panel.table.item(0, 0)
    panel.update_state({'tasks': [{**task, 'name': 'renamed'}], 'ts': 2.})
    assert panel.table.item(0, 0) is item
    assert item.text() == 'renamed' and panel._selected()['id'] == 'one'
    panel.deleteLater()
    app.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)


def test_threshold_objects_reused_and_timer_disposed(chart):
    chart.set_series([1, 2], [1, 2])
    chart.set_thresholds(1, .2, 2, .5, 3)
    ids = [id(x) for x in chart._tier_items]
    chart.set_thresholds(2, .3, 2, .5, 3)
    assert [id(x) for x in chart._tier_items] == ids
    chart.append_point(3, 3)
    assert chart._redraw_timer.isActive()
    chart.dispose()
    assert not chart._redraw_timer.isActive()


@pytest.fixture
def console(app, db, monkeypatch):
    monkeypatch.setattr(AccountSession, "start", lambda self: self._transition(Phase.CONNECTING, "connecting"))
    engine = EventEngine()
    registry = SessionRegistry(engine)
    account = {"login": 1, "server": "Demo", "terminal_path": "/terminal"}
    widget = AccountConsole(engine, account, AccountsStore(db), db, registry)
    yield widget, registry, engine
    widget.close()
    app.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)


def test_disconnect_keeps_view_subscriptions(console):
    widget, registry, engine = console
    before = len(widget._bridges)
    widget.stop_session_and_close()
    assert len(widget._bridges) == before
    assert all(bridge._alive for bridge in widget._bridges)


def test_same_combo_old_history_request_is_rejected(console):
    widget, registry, engine = console
    widget._current_legs = [{"symbol": "A", "side": "sell", "weight": 1},
                            {"symbol": "B", "side": "buy", "weight": 1}]
    widget._ensure_runner()
    widget._history_request = "new-request"
    payload = {"request_id": "old-request", "legs": widget._current_legs,
               "ok": True, "datasets": {"h1": {"t": [1, 2], "v": [1, 2]}}}
    widget._on_spread_history(widget._session._event(EVENT_SPREAD_HISTORY, payload))
    assert not widget._hist_ready
    payload["request_id"] = "new-request"
    payload["ok"] = False
    payload["error"] = "network error"
    widget._on_spread_history(widget._session._event(EVENT_SPREAD_HISTORY, payload))
    assert widget._history_request is None
    assert not widget._hist_ready
    assert widget._btn_start.isEnabled()


def test_accounts_reload_reads_authoritative_state(app, db, monkeypatch, tmp_path):
    monkeypatch.setattr(AccountSession, "start", lambda self: self._transition(Phase.CONNECTING, "正在连接"))
    engine, store = EventEngine(), AccountsStore(db)
    registry = SessionRegistry(engine)
    terminal = tmp_path / "terminal64.exe"
    terminal.touch()
    store.add({"login": 1, "server": "Demo", "terminal_path": str(terminal)})
    session = registry.connect(store.all()[0])
    session._transition(Phase.CONNECTED, "已连接")
    view = AccountsView(engine, store, db, registry)
    view._reload()
    assert view._table.item(0, 4).text() == "已连接"
    old = Event(EVENT_STATUS, {"session": session.name, "generation": "old", "payload": {"ok": False}})
    view._on_status(old)
    assert view._table.item(0, 4).text() == "已连接"
    view.close_all_consoles()
    view.deleteLater()


def test_repeated_console_close_removes_event_handlers(app, db, monkeypatch):
    monkeypatch.setattr(AccountSession, "start", lambda self: self._transition(Phase.CONNECTING, "connecting"))
    engine = EventEngine()
    registry = SessionRegistry(engine)
    store = AccountsStore(db)
    account = {"login": 1, "server": "Demo", "terminal_path": "/terminal"}
    for _ in range(4):
        widget = AccountConsole(engine, account, store, db, registry)
        session = widget._session
        widget.close()
        app.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)
        assert not engine._handlers
        assert registry.get(session.name) is session


def test_tick_bridge_coalesces_before_qt_delivery(app):
    from mt5_trader.constants import EVENT_TICK
    from mt5_trader.ui.base import EventBridge
    engine = EventEngine()
    bridge = EventBridge(engine, EVENT_TICK)
    received = []
    bridge.event_received.connect(received.append)
    for i in range(10000):
        bridge._on_event(Event(EVENT_TICK, {'session': 'A', 'generation': 'g', 'payload': [i]}))
    assert len(bridge._pending) == 1
    app.processEvents()
    assert len(received) == 1 and received[0].data['payload'] == [9999]
    bridge.destroy()


def test_live_signal_counts_are_rebuilt_from_worker_ledger(console):
    from mt5_trader.managed_trading import strategy_key
    widget, _, _ = console
    widget._current_legs = [{'symbol': 'A', 'side': 'buy', 'weight': 1},
                            {'symbol': 'B', 'side': 'sell', 'weight': 1}]
    widget._ensure_runner()
    key = strategy_key(widget._current_legs)
    widget._managed_state = {'ready': True, 'blocked': False,
                             'batches': [{'strategy': key, 'direction': 'sell'},
                                         {'strategy': 'other', 'direction': 'buy'}]}
    widget._restore_live_counts()
    assert widget._runner.signal.open_count('sell') == 1
    assert widget._runner.signal.open_count('buy') == 0
