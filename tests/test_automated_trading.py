"""Behavioral scenarios with injected broker/clock. Source only; not run on macOS."""
import copy
import threading
import time
from types import SimpleNamespace as NS

import pytest

from mt5_trader.automated_trading import (
    AutomationEngine, entry_reason, exit_reason, validate_task,
)
from mt5_trader.managed_trading import ExecutionLedger


def policy(task_id='one', **changes):
    return {'id': task_id, 'name': task_id,
            'legs': [{'symbol': 'EURUSD', 'side': 'sell', 'volume': .1},
                     {'symbol': 'EURGBP', 'side': 'buy', 'volume': .1}],
            'target_layers': 2, 'entry_op': 'gte', 'entry_spread': 0.,
            'exit_op': 'lte', 'exit_spread': 0., 'exit_spread_enabled': False,
            'max_roundtrip_cost': 5., 'fee_per_lot_side': 1., 'slippage_per_lot_side': .5,
            'min_carry': .1, 'profit_target': 1., 'max_loss': 5., 'max_hold_s': 3600.,
            'interval_s': .2, 'improvement_step': 0., 'entry_timeout_s': 300.,
            'max_account_lots': 10., 'max_margin_ratio': .5, 'costs_confirmed': True, **changes}


class FakeExecutor:
    def __init__(self, ledger):
        self.ledger = ledger
        self.state = {'batches': [], 'blocked': False}
        self.calls = []
        self.worker = NS(_mt5_lock=threading.RLock(), _stop_evt=threading.Event(),
                         _fx_rates_snapshot=lambda: {'USD': 1.})
        self.worker._tick = lambda symbol: {'bid': 1.1, 'ask': 1.1001, 'time': time.time()}
        info = NS(trade_calc_mode=0, volume_min=.01, volume_max=10., volume_step=.01)
        self.api = NS(symbol_info=lambda symbol: info, symbol_select=lambda symbol, yes: True,
                      ORDER_TYPE_BUY=0, ORDER_TYPE_SELL=1)

    def _account(self):
        return NS(currency='USD')

    def _positions(self):
        return {}

    def snapshot(self):
        return copy.deepcopy(self.state)

    def execute(self, message, guard=None):
        if guard:
            guard(self.snapshot())
        self.calls.append(copy.deepcopy(message))
        if message['operation'] == 'open':
            self.state['batches'].append({'id': message['request_id'], 'strategy': message['strategy_id'],
                'direction': 'buy', 'created': time.time(), 'positions': [], 'state': 'open'})
        else:
            chosen = message.get('batch_ids')
            self.state['batches'] = [b for b in self.state['batches'] if
                b['strategy'] != message['strategy_id'] or (chosen and b['id'] not in chosen)]
        return {'state': self.snapshot()}


@pytest.fixture
def engine(tmp_path):
    executor = FakeExecutor(ExecutionLedger(str(tmp_path / 'execution.db'), '1@demo'))
    engine = AutomationEngine(executor)
    engine.spread = lambda task, quotes: 1.
    engine.entry_metrics = lambda task, quotes: {'spread': 1., 'roundtrip_cost': 2., 'carry': .5, 'currency': 'USD'}
    engine.liquidation = lambda *args, **kwargs: 2.
    return engine


def install(engine, task_id='one', **changes):
    task = validate_task(policy(task_id, **changes))
    engine._save(task)
    engine.runtime[task_id] = {}
    return task


def control(engine, task_id, field, enabled):
    engine.command({'operation': 'control', 'task_id': task_id, 'field': field, 'enabled': enabled,
                    'revision': engine.tasks[task_id]['revision'], 'request_id': f'{task_id}-{field}-{enabled}-{time.time_ns()}'})


def pulse(engine):
    engine._next_scan = 0.
    engine._state_at = 0.
    for runtime in engine.runtime.values():
        runtime['next_entry'] = 0.
    engine.pulse()


def test_saved_task_does_not_trade_until_entry_is_enabled(engine):
    install(engine)
    pulse(engine)
    assert not engine.executor.calls
    control(engine, 'one', 'entry_enabled', True)
    pulse(engine)
    assert engine.executor.calls[0]['operation'] == 'open'
    assert engine.executor.calls[0]['legs'][0]['side'] == 'sell'
    assert not engine.tasks['one']['exit_enabled']


def test_target_is_a_cap_and_stops_entry(engine):
    install(engine, target_layers=1)
    control(engine, 'one', 'entry_enabled', True)
    for _ in range(5):
        pulse(engine)
    assert len(engine.executor.calls) == 1
    assert not engine.tasks['one']['entry_enabled']


def test_exit_switch_is_independent_and_prevents_refill(engine):
    install(engine)
    control(engine, 'one', 'entry_enabled', True)
    pulse(engine)
    control(engine, 'one', 'entry_enabled', False)
    pulse(engine)
    assert len(engine.executor.state['batches']) == 1
    control(engine, 'one', 'exit_enabled', True)
    pulse(engine)
    assert engine.executor.calls[-1]['operation'] == 'close'
    pulse(engine)
    assert not engine.tasks['one']['entry_enabled']
    assert not engine.tasks['one']['exit_enabled']
    assert not engine.executor.state['batches']


def test_two_pairs_share_fair_scheduler_and_exit_has_priority(engine):
    for task_id in ('one', 'two'):
        install(engine, task_id)
        control(engine, task_id, 'entry_enabled', True)
    pulse(engine)
    pulse(engine)
    assert {c['strategy_id'] for c in engine.executor.calls} == {'auto:one', 'auto:two'}
    control(engine, 'one', 'exit_enabled', True)
    pulse(engine)
    assert engine.executor.calls[-1]['strategy_id'] == 'auto:one'
    assert engine.executor.calls[-1]['operation'] == 'close'


def test_high_cost_waits_without_creating_an_order(engine):
    install(engine, max_roundtrip_cost=1.)
    control(engine, 'one', 'entry_enabled', True)
    pulse(engine)
    assert not engine.executor.calls
    assert '成本' in engine.runtime['one']['status']


def test_revalidation_rejects_an_opportunity_that_disappeared(engine):
    install(engine)
    control(engine, 'one', 'entry_enabled', True)
    calls = []
    def metrics(task, quotes):
        calls.append(1)
        return {'spread': 1., 'roundtrip_cost': 2. if len(calls) == 1 else 20., 'carry': .5}
    engine.entry_metrics = metrics
    pulse(engine)
    assert not engine.executor.calls
    assert engine.tasks['one']['entry_enabled']


def test_restart_restores_configuration_but_not_execution_switches(engine):
    install(engine)
    control(engine, 'one', 'entry_enabled', True)
    control(engine, 'one', 'exit_enabled', True)
    restarted = AutomationEngine(engine.executor)
    assert not restarted.tasks['one']['entry_enabled']
    assert not restarted.tasks['one']['exit_enabled']
    assert restarted.tasks['one']['target_layers'] == 2


def test_pause_does_not_need_broker_reconciliation(engine):
    install(engine)
    control(engine, 'one', 'entry_enabled', True)
    engine.executor.snapshot = lambda: (_ for _ in ()).throw(ValueError('offline'))
    control(engine, 'one', 'entry_enabled', False)
    assert not engine.tasks['one']['entry_enabled']


def test_account_error_pauses_all_tasks_and_never_resends(engine):
    install(engine)
    control(engine, 'one', 'entry_enabled', True)
    engine.executor.state['blocked'] = True
    pulse(engine)
    assert not engine.tasks['one']['entry_enabled']
    assert not engine.executor.calls


def test_exit_time_limit_can_act_without_profit_estimate(engine):
    install(engine, max_hold_s=1)
    engine.executor.state['batches'] = [{'id': 'owned', 'strategy': 'auto:one',
        'direction': 'buy', 'created': time.time() - 10, 'positions': []}]
    control(engine, 'one', 'exit_enabled', True)
    engine.liquidation = lambda *args, **kwargs: (_ for _ in ()).throw(ValueError('missing fees'))
    pulse(engine)
    assert engine.executor.calls[-1]['operation'] == 'close'


def test_holding_task_keeps_its_account_cap_when_paused(engine):
    install(engine, max_account_lots=.5)
    install(engine, 'two', max_account_lots=5.)
    control(engine, 'two', 'entry_enabled', True)
    limits = engine.account_limits({'batches': [{'strategy': 'auto:one'}]})
    assert limits['max_account_lots'] == .5


def test_stale_control_revision_cannot_overwrite_new_state(engine):
    task = install(engine)
    previous = task['revision']
    control(engine, 'one', 'entry_enabled', True)
    with pytest.raises(ValueError, match='已更新'):
        engine.command({'operation': 'control', 'task_id': 'one', 'revision': previous,
                        'field': 'entry_enabled', 'enabled': False, 'request_id': 'stale'})
    assert engine.tasks['one']['entry_enabled']


def test_expired_entry_instruction_does_not_trade(engine):
    install(engine)
    control(engine, 'one', 'entry_enabled', True)
    engine.tasks['one']['entry_deadline'] = time.time() - 1
    pulse(engine)
    assert not engine.executor.calls
    assert not engine.tasks['one']['entry_enabled']


def test_manual_exit_does_not_require_profit(engine):
    task = install(engine)
    engine.executor.state['batches'] = [{'id': 'b', 'strategy': 'auto:one'}]
    engine.command({'operation': 'close_now', 'task_id': 'one', 'revision': task['revision'], 'request_id': 'manual'})
    assert not engine.executor.state['batches']
    assert not engine.tasks['one']['entry_enabled']


def test_missing_cost_confirmation_and_nonfinite_inputs_are_rejected():
    with pytest.raises(ValueError):
        validate_task(policy(costs_confirmed=False))
    with pytest.raises(ValueError):
        validate_task(policy(fee_per_lot_side=float('nan')))


def test_additional_layers_require_better_prices_and_positive_carry():
    task = validate_task(policy(entry_spread=1, improvement_step=.1))
    metric = {'spread': 1.05, 'roundtrip_cost': 1., 'carry': .5}
    assert not entry_reason(task, metric, 0)
    assert entry_reason(task, metric, 1)
    assert entry_reason(task, {**metric, 'carry': -.1}, 0)


def test_profit_exit_requires_fees_covered_and_optional_spread():
    task = validate_task(policy(exit_spread_enabled=True, exit_spread=.2))
    assert not exit_reason(task, -.1, 1, 0.)
    assert not exit_reason(task, 2., 1, .5)
    assert exit_reason(task, 2., 1, .1) == '净利润及出场条件达标'
    assert exit_reason(task, -6., 1, .5) == '亏损限额'


def test_no_double_spread_charge_in_roundtrip_estimate(engine):
    task = validate_task(policy())
    info = NS(trade_calc_mode=0, swap_mode=4, swap_long=5., swap_short=5.,
              point=.00001, trade_tick_size=.00001, trade_tick_value=1.)
    info._asdict = lambda: vars(info)
    engine.api.symbol_info = lambda symbol: info
    engine.api.order_calc_profit = lambda *args: -1.
    quotes = engine._quotes(x['symbol'] for x in task['legs'])
    metrics = AutomationEngine.entry_metrics(engine, task, quotes)
    assert metrics['roundtrip_cost'] == pytest.approx(2. + 2 * .2 * 1.5)
    assert metrics['carry'] == pytest.approx(1.)


def test_liquidation_includes_signed_costs_and_deduplicates_deals(engine):
    task = validate_task(policy())
    pos = NS(identifier=1, ticket=101, symbol='EURUSD', type=0, price_open=1., volume=.1, swap=2.)
    deal = NS(ticket=1, profit=0., commission=-.5, fee=-.1, swap=0.)
    engine.api.order_calc_profit = lambda *args: 3.
    engine.api.history_deals_get = lambda **kwargs: [deal, deal]
    pnl = AutomationEngine.liquidation(engine, task, {'positions': [{'identifier': 1, 'ticket': 101}]},
        {1: pos}, {'EURUSD': {'bid': 1.1, 'ask': 1.2}})
    assert pnl == pytest.approx(3 + 2 - .5 - .1 - .15)


def test_pause_all_persistence_failure_fails_closed(engine, monkeypatch):
    for task_id in ('one', 'two'):
        install(engine, task_id)
        control(engine, task_id, 'entry_enabled', True)
    monkeypatch.setattr(engine.ledger, 'save_task', lambda task: (_ for _ in ()).throw(OSError('disk full')))
    with pytest.raises(OSError):
        engine.command({'operation': 'pause_all', 'request_id': 'stop'})
    assert all(not t['entry_enabled'] and not t['exit_enabled'] for t in engine.tasks.values())


def test_paused_held_task_can_edit_exit_policy_but_not_legs(engine):
    task = install(engine)
    engine.executor.state['batches'] = [{'id': 'held', 'strategy': 'auto:one'}]
    engine.command({'operation': 'save', 'task_id': 'one', 'revision': task['revision'],
                    'request_id': 'edit-exit', 'task': policy(profit_target=3.)})
    assert engine.tasks['one']['profit_target'] == 3.
    changed = policy()
    changed['legs'][0]['volume'] = .2
    with pytest.raises(ValueError, match='不可更改'):
        engine.command({'operation': 'save', 'task_id': 'one', 'revision': engine.tasks['one']['revision'],
                        'request_id': 'edit-legs', 'task': changed})


def test_duplicate_manual_exit_command_is_not_executed_twice(engine):
    task = install(engine)
    engine.executor.state['batches'] = [{'id': 'held', 'strategy': 'auto:one'}]
    request = {'operation': 'close_now', 'task_id': 'one', 'revision': task['revision'], 'request_id': 'once'}
    engine.command(request)
    engine.command(request)
    assert len(engine.executor.calls) == 1


def test_stale_quotes_and_cross_leg_skew_are_rejected(engine):
    engine.worker._tick = lambda symbol: {'bid': 1., 'ask': 1.1, 'time': time.time() - 5}
    assert 'EURUSD' in engine._quotes(['EURUSD'])
    engine.worker._tick = lambda symbol: {'bid': 1., 'ask': 1.1, 'time': time.time() - 600}
    assert engine._quotes(['EURUSD']) == {}
    task = validate_task(policy())
    now = time.monotonic()
    quotes = {'EURUSD': {'read_monotonic': now - 2, 'bid': 1., 'ask': 1.1},
              'EURGBP': {'read_monotonic': now, 'bid': .8, 'ask': .9}}
    with pytest.raises(ValueError, match='读取超过1秒'):
        AutomationEngine.spread(engine, task, quotes)


def test_disconnect_requires_manual_rearm_after_recovery(engine):
    install(engine)
    control(engine, 'one', 'entry_enabled', True)
    control(engine, 'one', 'exit_enabled', True)
    original = engine.executor._account
    def disconnected():
        raise ValueError('终端已断线')
    engine.executor._account = disconnected
    pulse(engine)
    assert not engine.tasks['one']['entry_enabled']
    assert not engine.tasks['one']['exit_enabled']
    assert '人工分别启用' in engine.runtime['one']['status']
    engine.executor._account = original
    pulse(engine)
    assert not engine.executor.calls
