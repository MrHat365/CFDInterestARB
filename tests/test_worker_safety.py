"""Isolated worker contracts: injected MT5 API, never contacts a broker."""
import importlib.util
from pathlib import Path
import queue
import sys
import time
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.fixture
def worker_api(monkeypatch):
    api = ModuleType("MetaTrader5")
    constants = {
        "TRADE_RETCODE_REQUOTE": 10004, "TRADE_RETCODE_PRICE_OFF": 10021,
        "TRADE_RETCODE_PRICE_CHANGED": 10020, "TRADE_RETCODE_DONE_PARTIAL": 10010,
        "ORDER_TYPE_BUY": 0, "ORDER_TYPE_SELL": 1, "TRADE_ACTION_DEAL": 1,
        "ORDER_TIME_GTC": 0, "ORDER_FILLING_IOC": 1,
    }
    for key, value in constants.items():
        setattr(api, key, value)
    api.account_info = Mock(return_value=SimpleNamespace(login=1, server="Demo"))
    api.terminal_info = Mock(return_value=SimpleNamespace(connected=True))
    api.symbol_info = Mock(return_value=SimpleNamespace(volume_min=.01, volume_max=100, volume_step=.01))
    api.last_error = Mock(return_value=(0, ""))
    api.order_send = Mock(return_value=None)
    monkeypatch.setitem(sys.modules, "MetaTrader5", api)
    # Load a separate module to avoid contaminating other tests' module cache.
    source = Path(__file__).resolve().parents[1] / "mt5_trader" / "worker.py"
    spec = importlib.util.spec_from_file_location("mt5_trader._worker_under_test", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    worker = module._Worker(queue.Queue(), queue.Queue(), {"login": 1, "server": "Demo"})
    worker._connected = True
    worker._tick = Mock(return_value={"bid": 1.0, "ask": 1.1, "time": time.time()})
    return worker, api, module


def test_buy_uses_ask_sell_uses_bid(worker_api):
    worker, api, _ = worker_api
    worker._build_market({"symbol": "A", "side": "buy", "volume": .1})
    assert api.order_send.call_args.args[0]["price"] == 1.1
    worker._build_market({"symbol": "A", "side": "sell", "volume": .1})
    assert api.order_send.call_args.args[0]["price"] == 1.0


def test_unknown_order_outcome_is_never_retried(worker_api):
    worker, api, _ = worker_api
    worker._build_market({"symbol": "A", "side": "buy", "volume": .1})
    assert api.order_send.call_count == 1


def test_same_login_on_wrong_server_cannot_trade(worker_api):
    worker, api, _ = worker_api
    api.account_info.return_value = SimpleNamespace(login=1, server="Other")
    worker._build_market({"symbol": "A", "side": "buy", "volume": .1})
    api.order_send.assert_not_called()


def test_disconnected_terminal_cannot_trade(worker_api):
    worker, api, _ = worker_api
    api.terminal_info.return_value.connected = False
    worker._build_market({"symbol": "A", "side": "buy", "volume": .1})
    api.order_send.assert_not_called()


def test_legacy_algo_request_is_rejected_before_first_leg(worker_api):
    worker, api, _ = worker_api
    worker._req.put({"action": "spread_algo", "legs": [{"symbol": "A", "side": "buy", "volume": .1},
                                       {"symbol": "B", "side": "sell", "volume": 0}]})
    worker._process_requests(0)
    api.order_send.assert_not_called()


def test_stop_cancels_queued_orders(worker_api):
    worker, api, _ = worker_api
    worker._order_q.put({"action": "order", "request": {"symbol": "A", "volume": .1}})
    worker.request_stop()
    worker._order_loop()
    api.order_send.assert_not_called()


def test_history_exception_emits_correlated_failure_and_thread_recovers(worker_api):
    worker, _, _ = worker_api
    worker._fetch_spread_history = Mock(side_effect=ValueError("bad history"))
    worker._hist_pending = {"legs": [{"symbol": "A"}], "request_id": "request-1"}
    worker._hist_evt.set()
    # Stop after the error reply, so this deterministic test doesn't wait forever.
    original = worker._send
    def send(kind, payload):
        original(kind, payload)
        worker.request_stop()
    worker._send = send
    worker._history_loop()
    reply = worker._rep.get_nowait()
    assert reply["payload"]["ok"] is False
    assert reply["payload"]["request_id"] == "request-1"
