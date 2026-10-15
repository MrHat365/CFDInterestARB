"""Durable execution for hedging accounts. Never infer ownership from net lots.

This module has no MT5 import. The worker supplies API/lock/order builders.
Every broker call has a committed intent first; uncertain outcomes are never
resent. Recovery is read-only and requires positive broker evidence.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import sys
import threading
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager
from functools import wraps
from pathlib import Path

from .constants import MAGIC_DEFAULT


def strategy_key(legs: list[dict]) -> str:
    data = sorted((str(x['symbol']), str(x['side']), float(x.get('weight', 1))) for x in legs)
    return hashlib.sha256(json.dumps(data, allow_nan=False).encode()).hexdigest()[:24]


class OrderNotSent(ValueError):
    """Local validation failed before reaching the broker submission call."""


class ExecutionLedger:
    """Short, fully synchronous transactions; independent connection per call."""
    def __init__(self, path: str, account: str):
        self.path, self.account = str(Path(path).resolve()), account
        self._lease_lock = threading.RLock()
        self._lease_depth = 0
        with closing(self._connect()) as conn, conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS managed_requests(
                account TEXT NOT NULL, request_id TEXT NOT NULL, body TEXT NOT NULL,
                PRIMARY KEY(account, request_id))''')
            conn.execute('''CREATE TABLE IF NOT EXISTS managed_batches(
                account TEXT NOT NULL, batch_id TEXT NOT NULL, body TEXT NOT NULL,
                PRIMARY KEY(account, batch_id))''')
            conn.execute('''CREATE TABLE IF NOT EXISTS automation_tasks(
                account TEXT NOT NULL, task_id TEXT NOT NULL, body TEXT NOT NULL,
                PRIMARY KEY(account, task_id))''')
            # Retain audit history, but keep it out of periodic reconciliation.
            # Missing/unknown states stay active and block new risk.
            conn.execute('''CREATE INDEX IF NOT EXISTS managed_active_account
                ON managed_batches(account)
                WHERE COALESCE(json_extract(body, '$.state'), '') <> 'closed' ''')

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=5)
        conn.execute('PRAGMA synchronous=FULL')
        return conn

    @contextmanager
    def exclusive(self):
        """Kernel-owned account lease, released even if the worker is killed.

        Prevents two application instances from racing their risk checks against
        the same ledger. No SQLite transaction is held over a broker API call.
        """
        with self._lease_lock:
            if self._lease_depth:
                self._lease_depth += 1
                try:
                    yield
                finally:
                    self._lease_depth -= 1
                return
            suffix = hashlib.sha256(self.account.encode()).hexdigest()[:24]
            lock_path = self.path + '.' + suffix + '.execution-lock'
            with open(lock_path, 'a+b') as stream:
                if stream.seek(0, 2) == 0:
                    stream.write(b'0')
                    stream.flush()
                stream.seek(0)
                if sys.platform == 'win32':
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self._lease_depth = 1
                try:
                    yield
                finally:
                    self._lease_depth = 0
                    stream.seek(0)
                    if sys.platform == 'win32':
                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def claim(self, message: dict) -> bool:
        # First durable write before any broker call. Duplicate request IDs
        # remain consumed even after rejection or process restart.
        with closing(self._connect()) as conn, conn:
            cursor = conn.execute('INSERT OR IGNORE INTO managed_requests VALUES(?,?,?)',
                                  (self.account, message['request_id'], json.dumps(message, allow_nan=False)))
            return cursor.rowcount == 1

    def save(self, batch: dict):
        body = json.dumps(batch, allow_nan=False)
        with closing(self._connect()) as conn, conn:
            conn.execute('INSERT INTO managed_batches VALUES(?,?,?) '
                         'ON CONFLICT(account,batch_id) DO UPDATE SET body=excluded.body '
                         'WHERE managed_batches.body <> excluded.body',
                         (self.account, batch['id'], body))

    def batches(self, *, active_only=False) -> list[dict]:
        predicate = (" AND COALESCE(json_extract(body, '$.state'), '') <> 'closed'"
                     if active_only else "")
        with closing(self._connect()) as conn:
            return [json.loads(row[0]) for row in conn.execute(
                'SELECT body FROM managed_batches WHERE account=?' + predicate + ' ORDER BY rowid',
                (self.account,))]

    def tasks(self) -> list[dict]:
        with closing(self._connect()) as conn:
            return [json.loads(row[0]) for row in conn.execute(
                'SELECT body FROM automation_tasks WHERE account=? ORDER BY rowid', (self.account,))]

    def save_task(self, task: dict):
        with closing(self._connect()) as conn, conn:
            conn.execute('INSERT INTO automation_tasks VALUES(?,?,?) '
                         'ON CONFLICT(account,task_id) DO UPDATE SET body=excluded.body',
                         (self.account, task['id'], json.dumps(task, allow_nan=False)))

    def delete_task(self, task_id: str):
        with closing(self._connect()) as conn, conn:
            conn.execute('DELETE FROM automation_tasks WHERE account=? AND task_id=?',
                         (self.account, task_id))


def _exclusive(method):
    @wraps(method)
    def guarded(self, *args, **kwargs):
        with self.ledger.exclusive():
            return method(self, *args, **kwargs)
    return guarded


class ManagedExecutor:
    def __init__(self, worker, api, ledger: ExecutionLedger):
        self.worker, self.api, self.ledger = worker, api, ledger

    def _account(self):
        account = self.api.account_info()
        if not self.worker._account_available(account):
            raise ValueError('账户身份不匹配或终端未连接')
        if getattr(account, 'margin_mode', None) != self.api.ACCOUNT_MARGIN_MODE_RETAIL_HEDGING:
            raise ValueError('受管策略仅支持对冲账户；净持仓账户无法隔离人工与策略仓位')
        return account

    def _positions(self):
        positions = self.api.positions_get()
        if positions is None:
            raise ValueError('持仓查询失败，不能将失败当成空仓')
        if any(int(p.identifier) <= 0 or not math.isfinite(float(p.volume)) or p.volume <= 0 for p in positions):
            raise ValueError('持仓标识或手数无效')
        if len({int(p.identifier) for p in positions}) != len(positions):
            raise ValueError('重复持仓标识，无法确定归属')
        return {int(p.identifier): p for p in positions}

    @staticmethod
    def _number(value, label):
        value = float(value)
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f'{label}必须为有限正数')
        return value

    def _reconcile(self, batch: dict, positions: dict):
        """Verify all opening fills, then compare only their position identifiers.

        Unknown send results stay blocked: absence from history is NOT evidence
        of rejection. A crash after a recorded fill can recover read-only.
        """
        if not batch['orders']:
            batch.update(state='closed', error='无发单意图，无仓位')
            self.ledger.save(batch)
            return
        owned = {}
        for order in batch['orders']:
            result = order.get('result')
            if result and result.get('not_sent'):
                continue
            if result is None or result['retcode'] not in (self.api.TRADE_RETCODE_DONE, self.api.TRADE_RETCODE_DONE_PARTIAL, *self._rejected_codes()):
                result = self._recover_order(order)
                if result is None:
                    raise ValueError('存在结果未知的发单意图，禁止自动重发；需终端人工对账')
                order['result'] = result
                self.ledger.save(batch)
            if result['retcode'] not in (self.api.TRADE_RETCODE_DONE, self.api.TRADE_RETCODE_DONE_PARTIAL):
                # TIMEOUT/CONNECTION/PLACED are not definitive rejections.
                if result['retcode'] not in self._rejected_codes():
                    raise ValueError('存在未确认终态的委托，需人工对账')
                continue
            if order['kind'] != 'market':
                continue
            ticket = int(result.get('deal') or 0)
            deals = self.api.history_deals_get(ticket=ticket) if ticket else None
            if not deals or len(deals) != 1:
                raise ValueError('开仓成交记录尚不可用')
            first = deals[0]
            history = self.api.history_deals_get(position=int(first.position_id))
            if history is None:
                raise ValueError('持仓成交历史不可用')
            opening = [d for d in history if int(d.order) == int(result['order'])]
            if (not opening or abs(sum(float(d.volume) for d in opening) - result['volume']) > 1e-8
                    or any(int(d.entry) != self.api.DEAL_ENTRY_IN or d.symbol != order['symbol']
                           or int(d.magic) != MAGIC_DEFAULT
                           or int(d.type) != (self.api.ORDER_TYPE_BUY if order['side'] == 'buy' else self.api.ORDER_TYPE_SELL)
                           for d in opening)):
                raise ValueError('成交身份、方向或成交量与回执不一致')
            pid = int(first.position_id)
            if pid in owned or any(int(d.position_id) != pid for d in opening):
                raise ValueError('持仓标识归属冲突')
            incoming = sum(float(d.volume) for d in history if int(d.entry) == self.api.DEAL_ENTRY_IN)
            outgoing = sum(float(d.volume) for d in history if int(d.entry) in (self.api.DEAL_ENTRY_OUT, self.api.DEAL_ENTRY_OUT_BY))
            if (abs(incoming - result['volume']) > 1e-8
                    or any(int(d.entry) not in (self.api.DEAL_ENTRY_IN, self.api.DEAL_ENTRY_OUT, self.api.DEAL_ENTRY_OUT_BY) for d in history)):
                raise ValueError('持仓发生非本策略增仓或反转，停止自动处理')
            remaining = incoming - outgoing
            position = positions.get(pid)
            if remaining < -1e-8 or (position is None and remaining > 1e-8):
                raise ValueError('历史与当前持仓不一致，等待完整记录')
            if position is not None:
                if (position.symbol != order['symbol'] or int(position.magic) != MAGIC_DEFAULT
                        or int(position.type) != (0 if order['side'] == 'buy' else 1)
                        or abs(float(position.volume) - remaining) > 1e-8):
                    raise ValueError('当前持仓与策略归属不一致')
                owned[pid] = {'symbol': position.symbol, 'ticket': int(position.ticket),
                              'identifier': pid, 'side': order['side'], 'volume': remaining}
        batch['positions'] = list(owned.values())
        if not owned:
            batch.update(state='closed', error='')
        elif batch.get('complete') and not batch.get('closing') and all(
                abs(p['volume'] - next(o['result']['volume'] for o in batch['orders']
                                      if o['kind'] == 'market' and o['symbol'] == p['symbol'])) < 1e-8
                for p in owned.values()) and len(owned) == len(batch['legs']):
            batch.update(state='open', error='')
        else:
            batch.update(state='blocked', error='部分成交、外部减仓或未完成平仓；仅允许核验后平掉本策略剩余仓位')
        self.ledger.save(batch)

    def _recover_order(self, order):
        """Positive evidence only: exact unique comment + magic + terminal order.

        Broker comment rewriting, missing history, or ambiguous matches leaves
        the request blocked. This method never places/cancels an order.
        """
        start = dt.datetime.fromtimestamp(order['started'] - 60, dt.timezone.utc)
        end = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=1)
        history = self.api.history_orders_get(start, end)
        if history is None:
            return None
        expected_type = self.api.ORDER_TYPE_BUY if order['side'] == 'buy' else self.api.ORDER_TYPE_SELL
        if order['kind'] == 'close':
            expected_type = self.api.ORDER_TYPE_SELL if expected_type == self.api.ORDER_TYPE_BUY else self.api.ORDER_TYPE_BUY
        matches = [o for o in history if o.comment == order['comment'] and int(o.magic) == MAGIC_DEFAULT
                   and o.symbol == order['symbol'] and int(o.type) == expected_type
                   and abs(float(o.volume_initial) - order['volume']) < 1e-8]
        if len(matches) != 1:
            return None
        item = matches[0]
        if item.state not in (self.api.ORDER_STATE_FILLED, self.api.ORDER_STATE_CANCELED,
                              self.api.ORDER_STATE_REJECTED, self.api.ORDER_STATE_EXPIRED):
            return None
        active = self.api.orders_get(ticket=int(item.ticket))
        deals = self.api.history_deals_get(start, end)
        if active is None or active or deals is None:
            return None
        fills = [d for d in deals if int(d.order) == int(item.ticket)]
        volume = sum(float(d.volume) for d in fills)
        if (not math.isfinite(volume) or abs(volume - (item.volume_initial - item.volume_current)) > 1e-8
                or any(d.symbol != order['symbol'] or int(d.magic) != MAGIC_DEFAULT
                       or int(d.type) != expected_type for d in fills)):
            return None
        if order['kind'] == 'close' and any(int(d.position_id) != order['identifier'] for d in fills):
            return None
        return {'retcode': (self.api.TRADE_RETCODE_DONE if abs(volume - order['volume']) < 1e-8 else
                            self.api.TRADE_RETCODE_DONE_PARTIAL) if volume > 0 else 10006,
                'order': int(item.ticket), 'deal': int(fills[0].ticket) if fills else 0,
                'volume': volume, 'price': float(fills[0].price) if fills else 0}

    def _rejected_codes(self):
        # Deliberately exclude generic ERROR, TIMEOUT, CONNECTION and PLACED.
        return {10004, 10006, 10013, 10014, 10015, 10016, 10017, 10018,
                10019, 10020, 10021, 10022, 10024, 10026, 10027, 10030,
                10032, 10033, 10034, 10035, 10036, 10038, 10040, 10042,
                10043, 10044, 10045, 10046}

    @_exclusive
    def snapshot(self) -> dict:
        with self.worker._mt5_lock:
            self._account()
            positions = self._positions()
            batches = self.ledger.batches(active_only=True)
            for batch in batches:
                if batch['state'] == 'closed':
                    continue
                try:
                    self._reconcile(batch, positions)
                except Exception as exc:
                    batch.update(state='blocked', error=str(exc), positions=[])
                    self.ledger.save(batch)
            active = [b for b in batches if b['state'] != 'closed']
            return {'ready': True, 'blocked': any(b['state'] != 'open' for b in active),
                    'batches': active, 'ts': time.time()}

    @_exclusive
    def execute(self, message: dict, guard=None) -> dict:
        request_id = str(message.get('request_id', ''))
        if not request_id or len(request_id) > 80:
            raise ValueError('缺少有效request_id')
        if not self.ledger.claim(message):
            return {'duplicate': True, 'request_id': request_id, 'state': self.snapshot()}
        with self.worker._mt5_lock:
            self._account()
            action = message.get('operation')
            strategy = str(message.get('strategy_id', ''))
            direction = message.get('direction')
            if not strategy or direction not in ('buy', 'sell'):
                raise ValueError('缺少策略归属或方向')
            state = self.snapshot()
            if action == 'open':
                if state['blocked']:
                    raise ValueError('账户有未核清的策略批次，禁止新增风险')
                self._open(message, state, guard)
            elif action == 'close':
                if guard is not None:
                    guard(state)
                self._close(message, state)
            else:
                raise ValueError('无效受管操作')
            return {'request_id': request_id, 'state': self.snapshot()}

    def _preflight(self, message, state):
        account = self._account()
        if not getattr(account, 'trade_allowed', False) or not getattr(account, 'trade_expert', False):
            raise ValueError('账户未允许自动交易')
        if (not all(math.isfinite(float(getattr(account, field))) for field in ('equity', 'margin', 'margin_free'))
                or account.equity <= 0 or account.margin < 0 or account.margin_free < 0):
            raise ValueError('账户资金数据无效')
        limit = int(message.get('max_layers', 0))
        if not 1 <= limit <= 100:
            raise ValueError('无效策略层数限制')
        count = sum(b['strategy'] == message['strategy_id'] and b['direction'] == message['direction']
                    for b in state['batches'])
        if count >= limit:
            raise ValueError('已达到本策略方向的真实持仓层数上限')
        legs = message.get('legs', [])
        if not 2 <= len(legs) <= 5 or len({x['symbol'] for x in legs}) != len(legs):
            raise ValueError('交易篮子必须含2至5个不同品种')
        cap = self._number(message.get('max_account_lots', 0), '账户总手数上限')
        margin_ratio = self._number(message.get('max_margin_ratio', 0), '保证金使用率上限')
        if margin_ratio > 1:
            raise ValueError('保证金使用率上限不得大于100%')
        positions = self._positions()
        pending = self.api.orders_get()
        if pending is None:
            raise ValueError('挂单查询失败，无法确认账户新增风险预算')
        if pending:
            tickets = ', '.join(str(order.ticket) for order in pending)
            raise ValueError(f'账户存在挂单（ticket: {tickets}），按保守规则阻止新增仓位')
        total = sum(float(p.volume) for p in positions.values())
        margin = 0.0
        for leg in legs:
            symbol, side = leg['symbol'], leg['side']
            volume = self._number(leg['volume'], '手数')
            info, tick = self.api.symbol_info(symbol), self.worker._tick(symbol)
            if (side not in ('buy', 'sell') or info is None or tick is None
                    or info.volume_step <= 0 or not info.volume_min <= volume <= info.volume_max
                    or abs(volume / info.volume_step - round(volume / info.volume_step)) > 1e-7):
                raise ValueError(f'{symbol} 交易规格或行情无效')
            if not (getattr(info, 'filling_mode', 0) & 2):
                raise ValueError(f'{symbol} 不支持策略需要的IOC成交方式')
            order_type = self.api.ORDER_TYPE_BUY if side == 'buy' else self.api.ORDER_TYPE_SELL
            check = self.api.order_check({
                'action': self.api.TRADE_ACTION_DEAL, 'symbol': symbol, 'volume': volume,
                'type': order_type, 'price': tick['ask' if side == 'buy' else 'bid'],
                'type_filling': self.api.ORDER_FILLING_IOC, 'type_time': self.api.ORDER_TIME_GTC,
                'magic': MAGIC_DEFAULT, 'deviation': 20})
            if check is None or check.retcode != 0:
                raise ValueError(f'{symbol} broker下单预检查未通过')
            needed = self.api.order_calc_margin(order_type, symbol, volume, tick['ask' if side == 'buy' else 'bid'])
            if needed is None or not math.isfinite(needed) or needed < 0:
                raise ValueError(f'{symbol} 保证金无法估算')
            margin += needed
            total += volume
        self._check_currency_residual(legs)
        if (total > cap + 1e-8 or account.equity <= 0 or margin > account.margin_free
                or (account.margin + margin) / account.equity > margin_ratio):
            raise ValueError('交易篮子超过账户总手数或保证金预算')

    def _check_currency_residual(self, legs):
        """Validate rounded FX lots against current executable-side cash flows.

        Check each currency in its own units; no conversion can cancel unrelated
        currencies. The 5% tolerance follows the existing weight model.
        """
        flows, largest = {}, {}
        for leg in legs:
            info = self.api.symbol_info(leg['symbol'])
            tick = self.worker._tick(leg['symbol'])
            if (info is None or tick is None or getattr(info, 'trade_calc_mode', None) not in (0, 5)
                    or not info.currency_base or not info.currency_profit or info.currency_base == info.currency_profit):
                raise ValueError('受管中性策略只支持有明确基础/报价币种的FOREX合约')
            contract = self._number(info.trade_contract_size, '合约大小')
            units = contract * self._number(leg['volume'], '手数')
            sign = 1 if leg['side'] == 'buy' else -1
            price = self._number(tick['ask' if sign > 0 else 'bid'], '报价')
            for currency, value in ((info.currency_base, sign * units),
                                    (info.currency_profit, -sign * units * price)):
                flows[currency] = flows.get(currency, 0.) + value
                largest[currency] = max(largest.get(currency, 0.), abs(value))
        if any(not math.isfinite(value) or abs(value) > largest[cur] * .05 + 1e-8 for cur, value in flows.items()):
            raise ValueError('步长取整后的逐币种残余敞口超过5%，请调整每层手数或组合')

    def _send(self, batch, request):
        order = {**request, '_managed': True, 'result': None, 'started': time.time()}
        order['comment'] = 'cfd-' + uuid.uuid4().hex[:24]
        batch['orders'].append(order)
        self.ledger.save(batch)  # Must succeed BEFORE order_send can be reached.
        builder = self.worker._build_market if request['kind'] == 'market' else self.worker._build_close
        try:
            result = builder(order)
        except OrderNotSent as exc:
            order['result'] = {'not_sent': True, 'reason': str(exc)}
            self.ledger.save(batch)
            raise ValueError(str(exc)) from exc
        if result is not None:
            order['result'] = {key: getattr(result, key, 0) for key in ('retcode', 'order', 'deal', 'volume', 'price')}
            self.ledger.save(batch)  # Failure leaves unknown intent on disk.
        if (result is None or result.retcode not in (self.api.TRADE_RETCODE_DONE, self.api.TRADE_RETCODE_DONE_PARTIAL)
                or not math.isfinite(float(result.volume)) or not 0 < result.volume <= request['volume'] + 1e-8):
            raise ValueError('委托未完整确认，已停止后续发单；请核对账本与终端')
        return float(result.volume)

    def _open(self, message, state, guard=None):
        self._preflight(message, state)
        if self.worker._stop_evt.is_set():
            raise ValueError('会话正在停止')
        if guard is not None:
            guard(state)  # Revalue after risk/API checks, before the first intent.
        legs = sorted(message['legs'], key=lambda x: float(x['volume']), reverse=True)
        batch = {'id': message['request_id'], 'strategy': message['strategy_id'],
                 'direction': message['direction'], 'legs': legs, 'orders': [],
                 'state': 'pending', 'complete': False, 'closing': False, 'created': time.time()}
        self.ledger.save(batch)
        ratio = 1.0
        for index, leg in enumerate(legs):
            volume = float(leg['volume']) * ratio
            info = self.api.symbol_info(leg['symbol'])
            if info is None or info.volume_step <= 0:
                raise ValueError('对冲腿交易规格不可用')
            volume = math.floor((volume + 1e-10) / info.volume_step) * info.volume_step
            if volume < info.volume_min or volume > info.volume_max:
                raise ValueError('部分成交后的对冲手数不符合交易规格')
            filled = self._send(batch, {'kind': 'market', 'symbol': leg['symbol'], 'side': leg['side'], 'volume': volume})
            if index == 0:
                ratio = filled / float(leg['volume'])
                planned = [{**leg, 'volume': filled}]
                for passive in legs[1:]:
                    spec = self.api.symbol_info(passive['symbol'])
                    if spec is None or spec.volume_step <= 0:
                        raise ValueError('部分成交后交易规格不可用')
                    amount = math.floor((float(passive['volume']) * ratio + 1e-10) / spec.volume_step) * spec.volume_step
                    planned.append({**passive, 'volume': amount})
                self._check_currency_residual(planned)
            elif abs(filled - volume) > 1e-8:
                raise ValueError('对冲腿部分成交，禁止继续增加风险')
        batch['complete'] = True
        self.ledger.save(batch)

    def _close(self, message, state):
        matches = [b for b in state['batches'] if b['strategy'] == message['strategy_id'] and b['direction'] == message['direction']]
        selected = message.get('batch_ids')
        if selected is not None:
            if not isinstance(selected, list) or not selected or len(set(selected)) != len(selected):
                raise ValueError('无效平仓批次选择')
            if not set(selected).issubset({b['id'] for b in matches}):
                raise ValueError('平仓批次已变化或不属于当前策略')
            matches = [b for b in matches if b['id'] in selected]
        if not matches:
            return
        # Prevalidate the entire close set before the first broker call. An
        # unknown opening/closing intent cannot be resolved by sending again.
        if any(not b.get('positions') for b in matches):
            raise ValueError('存在无法核验归属的批次，拒绝自动平仓')
        for batch in matches:
            batch['closing'] = True
            self.ledger.save(batch)
            for position in batch['positions']:
                self._account()
                current = self._positions().get(position['identifier'])
                if current is None or current.ticket != position['ticket'] or abs(current.volume - position['volume']) > 1e-8:
                    raise ValueError('平仓前持仓发生变化，请重新核验')
                filled = self._send(batch, {'kind': 'close', **position})
                if abs(filled - position['volume']) > 1e-8:
                    raise ValueError('平仓仅部分成交，监测停止；核验后可平剩余归属仓位')
