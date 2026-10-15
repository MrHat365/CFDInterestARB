"""Account-owned conditional basket tasks; no Qt or MT5 module import.

Called only by the account's order thread. Many tasks observe the same quote
snapshot; at most one basket is dispatched per pass. Broker execution remains
serialized by ManagedExecutor, including its durable intent and account lease.
"""
from __future__ import annotations

import copy
import math
import time
import uuid

from .spreads import pick_target_symbol, synthetic_price
from .weights import swap_to_usd_per_day


class ConditionsChanged(ValueError):
    """No order sent: a conditional instruction is no longer executable."""


def number(value, name, low=0., high=1e12):
    if isinstance(value, bool):
        raise ValueError(f'{name}必须是数值')
    value = float(value)
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f'{name}不在允许范围内')
    return value


def validate_task(raw: dict) -> dict:
    """Normalize the complete policy. Unknown costs never silently become zero."""
    task = {'id': str(raw['id']), 'name': str(raw.get('name', '')).strip()[:80]}
    if not task['id'] or len(task['id']) > 64:
        raise ValueError('任务编号无效')
    legs = raw.get('legs', [])
    if not 2 <= len(legs) <= 5 or len({x['symbol'] for x in legs}) != len(legs):
        raise ValueError('每个配对任务必须包含2至5个不同品种')
    task['legs'] = []
    for leg in legs:
        if leg['side'] not in ('buy', 'sell') or not str(leg['symbol']).strip():
            raise ValueError('交易腿方向/品种无效')
        task['legs'].append({'symbol': str(leg['symbol']), 'side': leg['side'],
                             'volume': number(leg['volume'], '每层手数', 1e-10)})
    task['target_layers'] = int(number(raw['target_layers'], '目标层数', 1, 100))
    if task['target_layers'] != float(raw['target_layers']):
        raise ValueError('目标层数必须为整数')
    for key in ('entry_op', 'exit_op'):
        if raw[key] not in ('gte', 'lte'):
            raise ValueError('价差比较条件无效')
        task[key] = raw[key]
    for key in ('entry_spread', 'exit_spread'):
        task[key] = number(raw[key], key, -1e12)
    for key in ('max_roundtrip_cost', 'fee_per_lot_side', 'slippage_per_lot_side',
                'min_carry', 'profit_target', 'max_loss', 'max_hold_s', 'improvement_step',
                'entry_timeout_s'):
        task[key] = number(raw[key], key)
    task['interval_s'] = number(raw['interval_s'], '分批间隔', .2, 86400)
    task['max_account_lots'] = number(raw['max_account_lots'], '账户总手数上限', 1e-10)
    task['max_margin_ratio'] = number(raw['max_margin_ratio'], '保证金使用率', 1e-10, 1)
    for key in ('exit_spread_enabled', 'costs_confirmed'):
        if not isinstance(raw.get(key), bool):
            raise ValueError(f'{key}必须明确设置')
        task[key] = raw[key]
    if not task['costs_confirmed']:
        raise ValueError('请先核实佣金、其他费用和标准持仓swap记账口径')
    # Switches are separate commands; saving never implicitly starts live trading.
    task.update(entry_enabled=False, exit_enabled=False, exit_latched=False,
                entry_deadline=0., revision=0)
    return task


def meets(value, op, threshold):
    return value >= threshold if op == 'gte' else value <= threshold


def entry_reason(task, metrics, count):
    threshold = task['entry_spread'] + (1 if task['entry_op'] == 'gte' else -1) * count * task['improvement_step']
    if not meets(metrics['spread'], task['entry_op'], threshold):
        return '等待有利入场价差'
    if metrics['roundtrip_cost'] > task['max_roundtrip_cost']:
        return '往返成本超过上限'
    if metrics['carry'] < task['min_carry'] or metrics['carry'] <= 0:
        return '当前方向净息未达到正息门槛'
    return ''


def exit_reason(task, pnl, age, spread):
    # Time/loss exits explicitly permit a loss. Price exits require net profit.
    if task['max_hold_s'] > 0 and age >= task['max_hold_s']:
        return '持有时间上限'
    if pnl is None:
        return ''
    if task['max_loss'] > 0 and pnl <= -task['max_loss']:
        return '亏损限额'
    if pnl >= task['profit_target'] and (
        not task['exit_spread_enabled'] or
        (spread is not None and meets(spread, task['exit_op'], task['exit_spread']))
    ):
        return '净利润及出场条件达标'
    return ''


class AutomationEngine:
    MAX_TASKS = 32
    QUOTE_AGE = 60.0  # Last market update, distinct from local read freshness.
    QUOTE_SKEW = 1.0  # Maximum age/skew of reads within this process.

    def __init__(self, executor):
        self.executor = executor
        self.worker, self.api, self.ledger = executor.worker, executor.api, executor.ledger
        self.tasks: dict[str, dict] = {}
        self.runtime: dict[str, dict] = {}
        self._next_scan = 0.
        self._state_at = 0.
        self._state: dict | None = None
        self._cursor = 0
        self._rates_at = 0.
        self._rates: dict[str, float] = {}
        self._deal_cache: dict = {}
        self._entry_cache: dict = {}
        self._last_error = ''
        for saved in self.ledger.tasks():
            task = validate_task(saved)
            task['exit_latched'] = bool(saved.get('exit_latched', False))
            task['revision'] = int(saved.get('revision', 0)) + 1
            self.tasks[task['id']] = task
            self.ledger.save_task(task)  # Process restart always requires manual rearming.
            self.runtime[task['id']] = {'status': '重启后已暂停，请核对条件后分别启用'}

    def symbols(self):
        return frozenset(leg['symbol'] for t in self.tasks.values() for leg in t['legs'])

    def view(self):
        tasks = []
        for key, task in self.tasks.items():
            runtime = copy.deepcopy(self.runtime.get(key, {}))
            if time.time() - runtime.get('observed_at', 0.) > 3:
                runtime.update(spread=None, roundtrip_cost=None, carry=None, net_liquidation=None)
            tasks.append({**copy.deepcopy(task), 'runtime': runtime})
        return {'tasks': tasks, 'error': self._last_error,
                'ts': time.time(), 'running': not self.worker._stop_evt.is_set()}

    def _save(self, task):
        task['revision'] += 1
        try:
            self.ledger.save_task(task)
        except Exception:
            for existing in self.tasks.values():
                existing.update(entry_enabled=False, exit_enabled=False)
            raise
        self.tasks[task['id']] = task

    def account_limits(self, state):
        held = {b['strategy'] for b in state['batches']}
        relevant = [t for t in self.tasks.values() if t['entry_enabled'] or t['exit_enabled']
                    or 'auto:' + t['id'] in held]
        if not relevant:
            return {}
        return {'max_account_lots': min(t['max_account_lots'] for t in relevant),
                'max_margin_ratio': min(t['max_margin_ratio'] for t in relevant)}

    def command(self, message):
        op = message.get('operation', 'snapshot')
        if op == 'snapshot':
            return
        request_id = str(message.get('request_id', ''))
        if not request_id or len(request_id) > 80:
            raise ValueError('缺少任务请求编号')
        with self.ledger.exclusive(), self.worker._mt5_lock:
            if not self.ledger.claim(message):
                return
            if op == 'pause_all':
                for task in list(self.tasks.values()):
                    self._save({**task, 'entry_enabled': False, 'exit_enabled': False})
                    self.runtime.setdefault(task['id'], {})['status'] = '入场与出场已全部暂停'
                return
            task_id = str(message.get('task_id', ''))
            old = self.tasks.get(task_id)
            if old is not None and message.get('revision') != old['revision']:
                raise ValueError('任务已更新，请刷新后操作')
            if op == 'control' and message.get('enabled') is False:
                field = message.get('field')
                if old is None or field not in ('entry_enabled', 'exit_enabled'):
                    raise ValueError('无效任务或执行开关')
                self._save({**old, field: False})
                self.runtime.setdefault(task_id, {})['status'] = '执行开关已暂停'
                return  # A human stop never depends on broker availability.
            state = self.executor.snapshot()
            batches = [b for b in state['batches'] if b['strategy'] == 'auto:' + task_id]
            if op == 'save':
                task = validate_task({**message['task'], 'id': task_id})
                if old and (old['entry_enabled'] or old['exit_enabled']):
                    raise ValueError('请先暂停入场和出场，再修改条件')
                if old and batches:
                    if task['legs'] != old['legs'] or task['target_layers'] < len(batches):
                        raise ValueError('有持仓时不可更改交易腿/每层手数，目标层数不能小于现有层数')
                    task['exit_latched'] = old['exit_latched']
                if not old and len(self.tasks) >= self.MAX_TASKS:
                    raise ValueError('最多32个配对任务')
                signature = sorted((x['symbol'], x['side']) for x in task['legs'])
                if any(k != task_id and sorted((x['symbol'], x['side']) for x in t['legs']) == signature
                       for k, t in self.tasks.items()):
                    raise ValueError('该方向已存在任务，请编辑已有任务，避免重复累计风险')
                for leg in task['legs']:
                    info = self.api.symbol_info(leg['symbol'])
                    if (info is None or info.trade_calc_mode not in (0, 5) or
                        info.volume_step <= 0 or not info.volume_min <= leg['volume'] <= info.volume_max or
                        abs(leg['volume'] / info.volume_step - round(leg['volume'] / info.volume_step)) > 1e-7):
                        raise ValueError(f"{leg['symbol']}规格/手数不符合当前FOREX支持范围")
                    if not self.api.symbol_select(leg['symbol'], True):
                        raise ValueError(f"{leg['symbol']}行情订阅失败")
                task['revision'] = old['revision'] if old else 0
                self._save(task)
                self.runtime[task_id] = {'status': '已保存，入场/出场均暂停'}
            elif op == 'control':
                if old is None:
                    raise ValueError('任务不存在')
                field = message.get('field')
                if field not in ('entry_enabled', 'exit_enabled') or not isinstance(message.get('enabled'), bool):
                    raise ValueError('无效执行开关')
                enabled = message['enabled']
                if enabled and state['blocked']:
                    raise ValueError('持仓归属未核清，请先处理受管账本异常')
                task = {**old, field: enabled}
                if field == 'entry_enabled' and enabled:
                    if old['exit_latched'] and batches:
                        raise ValueError('任务仍在退出周期，不能同时重启入场')
                    task['exit_latched'] = False
                    task['entry_deadline'] = (time.time() + task['entry_timeout_s']
                                              if task['entry_timeout_s'] else 0.)
                self._save(task)
                self.runtime.setdefault(task_id, {})['status'] = '执行开关已更新'
            elif op == 'delete':
                if old is None:
                    return
                if batches or old['entry_enabled'] or old['exit_enabled']:
                    raise ValueError('先暂停任务并处理持仓，再删除')
                self.ledger.delete_task(task_id)
                self.tasks.pop(task_id)
                self.runtime.pop(task_id, None)
                self._entry_cache.pop(task_id, None)
            elif op == 'close_now':
                if old is None:
                    raise ValueError('任务不存在')
                task = {**old, 'entry_enabled': False, 'exit_enabled': False, 'exit_latched': True}
                self._save(task)
                self.executor.execute({'request_id': uuid.uuid5(uuid.NAMESPACE_OID, request_id).hex,
                    'operation': 'close', 'strategy_id': 'auto:' + task_id,
                    'direction': 'buy', 'legs': []})
                self.runtime.setdefault(task_id, {})['status'] = '手动退出已处理，请核对剩余持仓'
            else:
                raise ValueError('未知任务操作')
            self._state_at = 0.

    def _quotes(self, symbols):
        quotes = {}
        for symbol in symbols:
            tick = self.worker._tick(symbol)
            if tick is None:
                continue
            stamp = (tick.get('time_msc') or tick['time'] * 1000) / 1000
            if not -2 <= time.time() - stamp <= getattr(self.worker, '_quote_max_age', self.QUOTE_AGE):
                continue
            quotes[symbol] = {**tick, 'stamp': stamp,
                              'read_monotonic': tick.get('read_monotonic', time.monotonic())}
        return quotes

    def spread(self, task, quotes):
        legs = [{**x, 'weight': x['volume']} for x in task['legs']]
        ticks = [quotes[x['symbol']] for x in legs]
        reads = [x['read_monotonic'] for x in ticks]
        now = time.monotonic()
        if (max(reads) - min(reads) > self.QUOTE_SKEW
                or any(not 0 <= now - stamp <= self.QUOTE_SKEW for stamp in reads)):
            raise ValueError('本轮跨腿行情读取超过1秒，请等待新快照')
        prices = {x['symbol']: quotes[x['symbol']]['ask' if x['side'] == 'buy' else 'bid'] for x in legs}
        target = pick_target_symbol(legs)
        value = prices[target] - synthetic_price(prices, legs, target)
        if not math.isfinite(value):
            raise ValueError('价差不可用')
        return value

    def entry_metrics(self, task, quotes):
        spread = self.spread(task, quotes)
        account = self.executor._account()
        if time.monotonic() - self._rates_at > 5:
            self._rates = self.worker._fx_rates_snapshot()
            self._rates_at = time.monotonic()
        currency = account.currency
        usd = 1. if currency == 'USD' else self._rates.get(currency)
        if not usd or not math.isfinite(usd) or usd <= 0:
            raise ValueError('账户币种汇率不可用')
        cost, carry, lots = 0., 0., 0.
        for leg in task['legs']:
            symbol, side, volume = leg['symbol'], leg['side'], leg['volume']
            tick = quotes[symbol]
            info = self.api.symbol_info(symbol)
            if info is None or info.trade_calc_mode not in (0, 5):
                raise ValueError('合约规格不可用')
            typ = self.api.ORDER_TYPE_BUY if side == 'buy' else self.api.ORDER_TYPE_SELL
            pnl = self.api.order_calc_profit(typ, symbol, volume,
                tick['ask' if side == 'buy' else 'bid'], tick['bid' if side == 'buy' else 'ask'])
            if pnl is None or not math.isfinite(pnl):
                raise ValueError('交易成本无法估值')
            cost += max(0., -pnl)
            data = info._asdict()
            data.update(point=info.point, tick_size=info.trade_tick_size, tick_value=info.trade_tick_value)
            per_lot = (0. if info.swap_mode == 0 else
                       swap_to_usd_per_day(data, side, self._rates, currency))
            if per_lot is None:
                raise ValueError('该方向隔夜利息机制暂不支持估值')
            carry += per_lot / usd * volume
            lots += volume
        cost += 2 * lots * (task['fee_per_lot_side'] + task['slippage_per_lot_side'])
        return {'spread': spread, 'roundtrip_cost': cost, 'carry': carry, 'currency': currency}

    def _entry_observation(self, task, quotes):
        # Reuse observations for unchanged ticks for at most one second. The
        # execution guard always bypasses this cache and reads the broker again.
        key = (task['revision'], tuple((x['symbol'], quotes[x['symbol']]['stamp'],
            quotes[x['symbol']]['bid'], quotes[x['symbol']]['ask']) for x in task['legs']))
        cached = self._entry_cache.get(task['id'])
        if cached and cached[0] == key and time.monotonic() - cached[1] < 1:
            return cached[2]
        metrics = self.entry_metrics(task, quotes)
        self._entry_cache[task['id']] = (key, time.monotonic(), metrics)
        return metrics

    def liquidation(self, task, batch, positions, quotes, *, fresh=False):
        """Standard position/deal swap accounting; separate account fees excluded.

        User explicitly confirms this broker convention and configures a fee
        reserve. Unknown fields are errors, never silently zeroed financial data.
        """
        total, seen = 0., set()
        for owned in batch['positions']:
            pos = positions.get(owned['identifier'])
            if pos is None or pos.ticket != owned['ticket']:
                raise ValueError('持仓已变化，等待对账')
            tick = quotes[pos.symbol]
            typ = self.api.ORDER_TYPE_BUY if pos.type == 0 else self.api.ORDER_TYPE_SELL
            price = tick['bid' if pos.type == 0 else 'ask']
            value = self.api.order_calc_profit(typ, pos.symbol, pos.volume, pos.price_open, price)
            if value is None or not math.isfinite(value):
                raise ValueError('平仓盈亏无法估值')
            total += value + number(pos.swap, '累计swap', -1e12)
            cached = self._deal_cache.get(pos.identifier)
            if fresh or cached is None or time.monotonic() - cached[0] >= 1:
                history = self.api.history_deals_get(position=pos.identifier)
                if history:
                    self._deal_cache[pos.identifier] = (time.monotonic(), history)
            else:
                history = cached[1]
            if not history:
                raise ValueError('收益所需成交历史缺失')
            for deal in history:
                if deal.ticket in seen:
                    continue
                seen.add(deal.ticket)
                for field in ('profit', 'commission', 'fee', 'swap'):
                    total += number(getattr(deal, field), field, -1e12)
            total -= pos.volume * (task['fee_per_lot_side'] + task['slippage_per_lot_side'])
        return total

    def _pause_error(self, task_id, error):
        task = {**self.tasks[task_id], 'entry_enabled': False, 'exit_enabled': False}
        self.runtime.setdefault(task_id, {})['status'] = '执行异常，入场/出场均已暂停；恢复连接后须人工分别启用，仓位保留：' + str(error)
        # Fail closed in memory even when storage itself fails.
        self.tasks[task_id] = task
        self._save(task)

    def _dispatch(self, task, action, batch=None, reason=''):
        started = time.monotonic()
        message = {'request_id': uuid.uuid4().hex, 'operation': action,
                   'strategy_id': 'auto:' + task['id'], 'direction': 'buy',
                   'legs': task['legs'] if action == 'open' else [],
                   'max_layers': task['target_layers'],
                   'automation_policy': copy.deepcopy(task), 'trigger': reason or '入场条件达标',
                   'max_account_lots': task['max_account_lots'],
                   'max_margin_ratio': task['max_margin_ratio']}
        # Retain the budget of paused tasks while their holdings still exist.
        message.update(self.account_limits(self._state or self.executor.snapshot()))
        if batch:
            message['batch_ids'] = [batch['id']]

        def guard(state):
            if self.worker._stop_evt.is_set() or time.monotonic() - started > 2:
                raise ConditionsChanged('执行前条件已过期')
            current = self.tasks[task['id']]
            if not current['entry_enabled' if action == 'open' else 'exit_enabled']:
                raise ConditionsChanged('任务已暂停')
            quotes = self._quotes(x['symbol'] for x in task['legs'])
            if action == 'open' or reason == '净利润及出场条件达标':
                if any(x['symbol'] not in quotes for x in task['legs']):
                    raise ConditionsChanged('执行前行情已过期')
            if action == 'open':
                if task['entry_deadline'] and time.time() >= task['entry_deadline']:
                    raise ConditionsChanged('入场等待期限已到')
                count = sum(b['strategy'] == message['strategy_id'] for b in state['batches'])
                why = entry_reason(task, self.entry_metrics(task, quotes), count)
                if count >= task['target_layers'] or why:
                    raise ConditionsChanged(why or '已达到目标持仓')
            elif reason == '净利润及出场条件达标':
                current_batch = next((b for b in state['batches'] if b['id'] == batch['id']), None)
                if current_batch is None:
                    raise ConditionsChanged('待平批次已变化')
                pnl = self.liquidation(task, current_batch, self.executor._positions(), quotes, fresh=True)
                if exit_reason(task, pnl, 0., self.spread(task, quotes)) != reason:
                    raise ConditionsChanged('平仓前净利润/价差条件已变化')
            if time.monotonic() - started > 2:
                raise ConditionsChanged('执行前估值用时超过有效期')
        result = self.executor.execute(message, guard=guard)
        self._state, self._state_at = result['state'], time.monotonic()
        self._deal_cache.clear()
        layers = sum(b['strategy'] == message['strategy_id'] for b in result['state']['batches'])
        self.runtime[task['id']].update(last_action=action, last_reason=reason,
            execution_ms=round((time.monotonic() - started) * 1000, 1),
            status='已完成本次' + ('入场' if action == 'open' else '出场'),
            next_entry=time.monotonic() + task['interval_s'], layers=layers)
        if result['state']['blocked']:
            raise ValueError('部分成交或归属未核清，需要核验后处置')
        if action == 'open' and layers >= task['target_layers']:
            self._save({**self.tasks[task['id']], 'entry_enabled': False})
            self.runtime[task['id']]['status'] = '已达到目标持仓，自动入场停止'

    def pulse(self):
        now = time.monotonic()
        if now < self._next_scan or self.worker._stop_evt.is_set():
            return
        self._next_scan = now + .2
        active = [t for t in self.tasks.values() if t['entry_enabled'] or t['exit_enabled']]
        if not active:
            return
        try:
            with self.worker._mt5_lock:
                self.executor._account()
                if self._state is None or now - self._state_at >= 1:
                    self._state = self.executor.snapshot()
                    self._state_at = now
                state = self._state
                if state['blocked']:
                    raise ValueError('账户存在未核清批次；任务暂停，请核验受管持仓')
                quotes = self._quotes({x['symbol'] for t in active for x in t['legs']})
                positions = self.executor._positions()
                self._deal_cache = {k: v for k, v in self._deal_cache.items() if k in positions}
                exits, opens = [], []
                ordered = active[self._cursor % len(active):] + active[:self._cursor % len(active)]
                self._cursor += 1
                for task in ordered:
                    runtime = self.runtime.setdefault(task['id'], {})
                    batches = [b for b in state['batches'] if b['strategy'] == 'auto:' + task['id']]
                    runtime.update(layers=len(batches), status='等待条件', observed_at=time.time())
                    if not batches:
                        runtime['net_liquidation'] = 0.
                    if task['entry_enabled'] and (len(batches) >= task['target_layers'] or
                        (task['entry_deadline'] and time.time() >= task['entry_deadline'])):
                        task = {**task, 'entry_enabled': False}
                        self._save(task)
                        runtime['status'] = '目标已达到或入场期限已到，入场已停止'
                    if task['exit_latched'] and not batches:
                        self._save({**task, 'entry_enabled': False, 'exit_enabled': False, 'exit_latched': False})
                        runtime['status'] = '退出周期完成，请人工重新启用下一轮'
                        continue
                    spread = None
                    try:
                        spread = self.spread(task, quotes)
                        runtime['spread'] = spread
                    except (KeyError, ValueError) as exc:
                        runtime['spread'] = None
                        runtime['status'] = '等待同步行情：' + str(exc)
                    if task['exit_enabled']:
                        pnls = []
                        for batch in batches:
                            pnl = None
                            try:
                                if spread is None:
                                    raise ValueError('等待同步报价')
                                pnl = self.liquidation(task, batch, positions, quotes)
                                pnls.append(pnl)
                            except (KeyError, ValueError, AttributeError) as exc:
                                runtime['status'] = '净收益不可用：' + str(exc)
                            reason = exit_reason(task, pnl, time.time() - batch['created'], spread)
                            if reason:
                                exits.append((task, batch, reason))
                        runtime['net_liquidation'] = sum(pnls) if len(pnls) == len(batches) else None
                    if task['entry_enabled'] and not task['exit_latched'] and now >= runtime.get('next_entry', 0):
                        try:
                            metrics = self._entry_observation(task, quotes)
                            runtime.update(metrics)
                            why = entry_reason(task, metrics, len(batches))
                            runtime['status'] = why or '入场条件达标，等待执行'
                            if not why:
                                opens.append(task)
                        except (KeyError, ValueError, AttributeError) as exc:
                            runtime.update(roundtrip_cost=None, carry=None)
                            runtime['status'] = '入场估值不可用：' + str(exc)
                self._last_error = ''
                if exits:
                    task, batch, reason = exits[0]
                    # Once any exit starts, prohibit refilling this cycle.
                    if task['entry_enabled'] or not task['exit_latched']:
                        task = {**task, 'entry_enabled': False, 'exit_latched': True}
                        self._save(task)
                    self._run_one(task, 'close', batch, reason)
                elif opens:
                    self._run_one(opens[0], 'open')
        except Exception as exc:
            self._last_error = str(exc)
            for task in list(self.tasks.values()):
                if task['entry_enabled'] or task['exit_enabled']:
                    try:
                        self._pause_error(task['id'], exc)
                    except Exception:
                        pass  # Memory switches were already cleared before persistence.

    def _run_one(self, task, action, batch=None, reason=''):
        try:
            self._dispatch(task, action, batch, reason)
        except ConditionsChanged as exc:
            self.runtime[task['id']]['status'] = '等待重新满足条件：' + str(exc)
        except Exception as exc:
            self._state_at = 0.
            self._pause_error(task['id'], exc)
