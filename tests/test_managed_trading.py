"""Durable lifecycle regressions. Injected API, no terminal or broker calls.

These tests are source-only in this audit; execution is deferred to Windows.
"""
import threading
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from mt5_trader.constants import MAGIC_DEFAULT
from mt5_trader.managed_trading import ExecutionLedger, ManagedExecutor, strategy_key


class Broker:
    ACCOUNT_MARGIN_MODE_RETAIL_HEDGING = 2
    TRADE_RETCODE_DONE = 10009
    TRADE_RETCODE_DONE_PARTIAL = 10010
    ORDER_TYPE_BUY, ORDER_TYPE_SELL = 0, 1
    DEAL_ENTRY_IN, DEAL_ENTRY_OUT, DEAL_ENTRY_OUT_BY = 0, 1, 3
    ORDER_STATE_FILLED, ORDER_STATE_CANCELED = 4, 2
    ORDER_STATE_REJECTED, ORDER_STATE_EXPIRED = 5, 6
    TRADE_ACTION_DEAL, ORDER_FILLING_IOC, ORDER_TIME_GTC = 1, 1, 0

    def __init__(self):
        self.account = NS(margin_mode=2, trade_allowed=True, trade_expert=True,
                          equity=10000., margin=0., margin_free=10000.)
        self.positions, self.deals, self.orders = {}, [], []
        self.sent = []
        self.fill_ratio = 1.0
        self.return_unknown = False

    def account_info(self):
        return self.account

    def positions_get(self):
        return tuple(self.positions.values())

    def orders_get(self, **kwargs):
        return ()

    def symbol_info(self, symbol):
        return NS(volume_min=.01, volume_max=100., volume_step=.01, filling_mode=2,
                  currency_base="EUR", currency_profit="USD", trade_contract_size=100000., trade_calc_mode=0)

    def order_calc_margin(self, *args):
        return 10.

    def order_check(self, request):
        return NS(retcode=0)

    def history_orders_get(self, *args):
        return tuple(self.orders)

    def history_deals_get(self, *args, ticket=None, position=None):
        if ticket is not None:
            return tuple(d for d in self.deals if d.ticket == ticket)
        if position is not None:
            return tuple(d for d in self.deals if d.position_id == position)
        return tuple(self.deals)

    def send(self, request):
        self.sent.append(dict(request))
        number = len(self.sent)
        volume = request['volume'] * self.fill_ratio
        typ = 0 if request['side'] == 'buy' else 1
        if request['kind'] == 'market':
            pid = 100 + number
            self.positions[pid] = NS(identifier=pid, ticket=pid + 1000,
                                     symbol=request['symbol'], magic=MAGIC_DEFAULT,
                                     type=typ, volume=volume)
            entry = 0
        else:
            pid = request['identifier']
            self.positions[pid].volume -= volume
            if self.positions[pid].volume <= 1e-8:
                del self.positions[pid]
            typ, entry = 1 - typ, 1
        self.deals.append(NS(ticket=number, order=number, position_id=pid,
                             symbol=request['symbol'], magic=MAGIC_DEFAULT,
                             entry=entry, type=typ, volume=volume, price=1.))
        self.orders.append(NS(ticket=number, comment=request['comment'], magic=MAGIC_DEFAULT,
                              symbol=request['symbol'], type=typ, volume_initial=request['volume'],
                              volume_current=request['volume'] - volume, state=4))
        if self.return_unknown:
            return None
        return NS(retcode=10009 if volume == request['volume'] else 10010,
                  order=number, deal=number, volume=volume, price=1.)


@pytest.fixture
def managed(tmp_path):
    api = Broker()
    worker = NS(_mt5_lock=threading.RLock(), _stop_evt=threading.Event(),
                _account_available=lambda account: account is api.account,
                _tick=lambda symbol: {'bid': 1., 'ask': 1.01},
                _build_market=api.send, _build_close=api.send)
    ledger = ExecutionLedger(str(tmp_path / 'ledger.db'), '1@Demo')
    return ManagedExecutor(worker, api, ledger), api


def message(request_id='open-1', **changes):
    return {'request_id': request_id, 'operation': 'open', 'strategy_id': 'strategy-1',
            'direction': 'buy', 'legs': [{'symbol': 'A', 'side': 'buy', 'volume': .1},
                                       {'symbol': 'B', 'side': 'sell', 'volume': .1}],
            'max_layers': 3, 'max_account_lots': 2., 'max_margin_ratio': .5, **changes}


def test_open_restart_close_only_owned_positions(managed):
    executor, api = managed
    api.positions[999] = NS(identifier=999, ticket=1999, symbol='A', magic=42, type=0, volume=.4)
    state = executor.execute(message())['state']
    assert not state['blocked'] and len(state['batches']) == 1
    restarted = ManagedExecutor(executor.worker, api, ExecutionLedger(executor.ledger.path, '1@Demo'))
    state = restarted.execute(message('close-1', operation='close', legs=[]))['state']
    assert state['batches'] == []
    assert set(api.positions) == {999}
    assert len(api.sent) == 4


def test_duplicate_request_survives_restart(managed):
    executor, api = managed
    executor.execute(message())
    restarted = ManagedExecutor(executor.worker, api, ExecutionLedger(executor.ledger.path, '1@Demo'))
    assert restarted.execute(message())['duplicate']
    assert len(api.sent) == 2


def test_economic_guard_rejects_before_first_broker_order(managed):
    executor, api = managed
    def reject(state):
        raise ValueError('cost budget exceeded')
    with pytest.raises(ValueError, match='cost budget'):
        executor.execute(message(), guard=reject)
    assert api.sent == []


def test_selected_batch_exit_leaves_other_layers_open(managed):
    executor, api = managed
    executor.execute(message('first'))
    executor.execute(message('second'))
    state = executor.execute(message('close-first', operation='close', legs=[], batch_ids=['first']))['state']
    assert [batch['id'] for batch in state['batches']] == ['second']
    assert len(api.positions) == 2


def test_selected_batch_cannot_close_another_strategy(managed):
    executor, api = managed
    executor.execute(message('first'))
    with pytest.raises(ValueError, match='不属于'):
        executor.execute(message('wrong-close', operation='close', strategy_id='other', batch_ids=['first']))
    assert len(api.sent) == 2


def test_intent_is_committed_before_broker_call(managed):
    executor, api = managed
    def inspect(request):
        batches = ExecutionLedger(executor.ledger.path, '1@Demo').batches()
        assert batches[-1]['orders'][-1]['result'] is None
        return api.send(request)
    executor.worker._build_market = inspect
    executor.execute(message())
    assert len(api.sent) == 2


def test_persistence_failure_prevents_any_order(managed, monkeypatch):
    executor, api = managed
    monkeypatch.setattr(executor.ledger, 'save', Mock(side_effect=OSError('disk full')))
    with pytest.raises(OSError):
        executor.execute(message())
    assert api.sent == []


def test_unknown_fill_recovered_from_positive_evidence_without_resend(managed):
    executor, api = managed
    api.return_unknown = True
    with pytest.raises(ValueError):
        executor.execute(message())
    state = executor.snapshot()
    assert state['blocked']  # incomplete basket is not a completed layer
    assert len(state['batches'][0]['positions']) == 1
    assert len(api.sent) == 1
    api.return_unknown = False
    assert executor.execute(message('close-1', operation='close', legs=[]))['state']['batches'] == []


def test_missing_history_never_proves_unknown_order_was_rejected(managed):
    executor, api = managed
    executor.worker._build_market = Mock(return_value=None)
    with pytest.raises(ValueError):
        executor.execute(message())
    assert executor.snapshot()['blocked']
    with pytest.raises(ValueError, match='未核清'):
        executor.execute(message('open-2'))
    assert executor.worker._build_market.call_count == 1


def test_broker_comment_rewrite_cannot_be_guessed_for_recovery(managed):
    executor, api = managed
    api.return_unknown = True
    with pytest.raises(ValueError):
        executor.execute(message())
    api.orders[0].comment = 'broker modified'
    state = executor.snapshot()
    assert state['blocked'] and not state['batches'][0]['positions']
    with pytest.raises(ValueError, match='无法核验'):
        executor.execute(message('close', operation='close', legs=[]))
    assert len(api.sent) == 1


def test_netting_account_rejected_before_send(managed):
    executor, api = managed
    api.account.margin_mode = 0
    with pytest.raises(ValueError, match='对冲账户'):
        executor.execute(message())
    assert not api.sent


@pytest.mark.parametrize('changes', [{'max_account_lots': .1}, {'max_margin_ratio': .001}, {'max_layers': 0}])
def test_risk_budget_rejects_entire_basket(managed, changes):
    executor, api = managed
    with pytest.raises(ValueError):
        executor.execute(message(**changes))
    assert not api.sent


def test_real_layer_limit_is_counted_from_ledger(managed):
    executor, api = managed
    executor.execute(message(max_layers=1))
    with pytest.raises(ValueError, match='真实持仓层数'):
        executor.execute(message('second', max_layers=1))
    assert len(api.sent) == 2


def test_partial_passive_fill_blocks_more_entries_but_can_close_verified_remainder(managed):
    executor, api = managed
    original = api.send
    def send(request):
        api.fill_ratio = .5 if len(api.sent) == 1 else 1.
        return original(request)
    executor.worker._build_market = send
    with pytest.raises(ValueError, match='对冲腿部分成交'):
        executor.execute(message())
    assert executor.snapshot()['blocked']
    api.fill_ratio = 1.
    assert executor.execute(message('close', operation='close', legs=[]))['state']['batches'] == []


def test_modified_position_volume_blocks_automatic_close(managed):
    executor, api = managed
    executor.execute(message())
    api.positions[101].volume = .5  # no matching deal evidence
    with pytest.raises(ValueError, match='无法核验'):
        executor.execute(message('close', operation='close', legs=[]))
    assert len(api.sent) == 2


def test_other_strategy_is_not_closed(managed):
    executor, api = managed
    executor.execute(message())
    executor.execute(message('other', strategy_id='strategy-2'))
    state = executor.execute(message('close', operation='close', legs=[]))['state']
    assert len(state['batches']) == 1 and state['batches'][0]['strategy'] == 'strategy-2'
    assert len(api.positions) == 2


def test_account_ledgers_are_isolated(managed):
    executor, api = managed
    executor.execute(message())
    other = ExecutionLedger(executor.ledger.path, '1@OtherServer')
    assert other.batches() == []
    assert other.claim(message())


def test_nan_account_equity_is_rejected(managed):
    executor, api = managed
    api.account.equity = float('nan')
    with pytest.raises(ValueError, match='资金数据'):
        executor.execute(message())
    assert not api.sent


def test_strategy_identity_stable_under_leg_reordering():
    legs = [{'symbol': 'A', 'side': 'buy', 'weight': .4}, {'symbol': 'B', 'side': 'sell', 'weight': 1}]
    assert strategy_key(legs) == strategy_key(list(reversed(legs)))
    assert strategy_key(legs) != strategy_key([{**legs[0], 'weight': .5}, legs[1]])


def test_account_lease_excludes_another_worker_with_same_database(managed):
    executor, _ = managed
    other = ExecutionLedger(executor.ledger.path, executor.ledger.account)
    with executor.ledger.exclusive():
        with pytest.raises(OSError):
            with other.exclusive():
                raise AssertionError('second worker acquired an occupied lease')
    with other.exclusive():
        assert other.claim(message('after-release'))


def test_rounded_currency_exposure_rejected_before_first_order(managed):
    executor, api = managed
    legs = [{'symbol': 'A', 'side': 'buy', 'volume': .1},
            {'symbol': 'B', 'side': 'sell', 'volume': .01}]
    with pytest.raises(ValueError, match='残余敞口'):
        executor.execute(message(legs=legs))
    assert not api.sent


def test_local_rejection_does_not_become_a_permanent_unknown_intent(managed):
    from mt5_trader.managed_trading import OrderNotSent
    executor, api = managed
    executor.worker._build_market = Mock(side_effect=OrderNotSent('quote expired before send'))
    with pytest.raises(ValueError, match='quote expired'):
        executor.execute(message())
    state = executor.snapshot()
    assert not state['blocked'] and state['batches'] == []
    assert not api.sent


def test_unrelated_manual_pending_order_blocks_new_exposure(managed):
    executor, api = managed
    api.orders_get = lambda **kwargs: (NS(ticket=987, symbol='UNRELATED', magic=0),)
    with pytest.raises(ValueError, match='987.*保守规则'):
        executor.execute(message())
    assert not api.sent
