"""Manual configuration and independent switches for account background tasks."""
from __future__ import annotations

import time
import uuid

from PySide6 import QtCore, QtWidgets

from ..automated_trading import validate_task


class TaskEditor(QtWidgets.QDialog):
    def __init__(self, seed, parent=None):
        super().__init__(parent)
        self.setWindowTitle('配对自动交易条件（保存后仍暂停）')
        self.resize(680, 760)
        self.seed = seed
        root = QtWidgets.QVBoxLayout(self)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        content = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(content)
        scroll.setWidget(content)
        root.addWidget(scroll)
        self.fields = {}
        self.name = QtWidgets.QLineEdit(seed.get('name', '配对任务'))
        form.addRow('任务名称', self.name)
        self.legs = QtWidgets.QTableWidget(len(seed['legs']), 3)
        self.legs.setHorizontalHeaderLabels(['品种', '固定方向', '每层实际手数'])
        self.legs.setMinimumHeight(160)
        self.legs.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.Stretch)
        for row, leg in enumerate(seed['legs']):
            for col, value in enumerate((leg['symbol'], '买' if leg['side'] == 'buy' else '卖', leg['volume'])):
                item = QtWidgets.QTableWidgetItem(str(value))
                if col < 2:
                    item.setFlags(item.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
                self.legs.setItem(row, col, item)
        form.addRow('交易腿（方向不会随信号翻转）', self.legs)

        def spin(key, label, default, minimum=0., maximum=1e9, decimals=4):
            widget = QtWidgets.QDoubleSpinBox()
            widget.setRange(minimum, maximum)
            widget.setDecimals(decimals)
            widget.setValue(float(seed.get(key, default)))
            self.fields[key] = widget
            form.addRow(label, widget)

        def comparison(key, label, default):
            widget = QtWidgets.QComboBox()
            widget.addItem('大于等于 ≥', 'gte')
            widget.addItem('小于等于 ≤', 'lte')
            widget.setCurrentIndex(widget.findData(seed.get(key, default)))
            self.fields[key] = widget
            form.addRow(label, widget)

        spin('target_layers', '最大目标持仓（层）', 1, 1, 100, 0)
        comparison('entry_op', '入场价差条件', 'gte')
        spin('entry_spread', '入场价差门槛（目标腿报价单位）', 0, -1e9, decimals=8)
        spin('improvement_step', '每加一层要求价差再改善', 0, decimals=8)
        spin('interval_s', '最短分批间隔（秒）', 5, .2, 86400, 1)
        spin('entry_timeout_s', '入场等待上限（秒，0=不限）', 3600)
        spin('max_roundtrip_cost', '每层往返成本上限（账户币种）', 0)
        spin('fee_per_lot_side', '每手每侧佣金及其他费用预留', 0)
        spin('slippage_per_lot_side', '每手每侧滑点预留（账户币种）', 0)
        spin('min_carry', '每层最低普通单次净息（账户币种）', 0)
        spin('profit_target', '每层出场净利润门槛（账户币种）', 1)
        self.exit_spread = QtWidgets.QCheckBox('盈利退出还须满足价差条件')
        self.exit_spread.setChecked(seed.get('exit_spread_enabled', False))
        form.addRow(self.exit_spread)
        comparison('exit_op', '出场价差条件', 'lte')
        spin('exit_spread', '出场价差门槛', 0, -1e9, decimals=8)
        spin('max_loss', '每层亏损退出限额（0=禁用）', 0)
        spin('max_hold_s', '每层最长持有秒数（0=禁用）', 0)
        spin('max_account_lots', '全账户总手数上限（含其他持仓）', 0)
        spin('max_margin_ratio', '全账户保证金使用率上限（0～1）', .3, 0, 1)
        self.confirmed = QtWidgets.QCheckBox('已核实费用及标准 position/deal swap 记账口径')
        self.confirmed.setChecked(seed.get('costs_confirmed', False))
        form.addRow(self.confirmed)
        note = QtWidgets.QLabel(
            '价差 = 目标卖腿 Bid − 其余腿合成价；方向固定，门槛由你设置。\n'
            '所有金额均为账户币种。普通单次净息不包含三倍息/节假日日历。\n'
            '净利润门槛逐层判断；时间/亏损退出允许亏损，只在自动出场启用时生效。\n'
            '费用填0表示已核实为0。独立账户级融资扣款/重开仓记息尚不支持。')
        note.setWordWrap(True)
        form.addRow(note)
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Save | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        self.task: dict | None = None

    def _accept(self):
        try:
            raw = {'id': self.seed.get('id') or uuid.uuid4().hex, 'name': self.name.text(),
                   'legs': [{**leg, 'volume': float(self.legs.item(i, 2).text())}
                            for i, leg in enumerate(self.seed['legs'])],
                   'costs_confirmed': self.confirmed.isChecked(),
                   'exit_spread_enabled': self.exit_spread.isChecked()}
            for key, widget in self.fields.items():
                raw[key] = widget.currentData() if isinstance(widget, QtWidgets.QComboBox) else widget.value()
            self.task = validate_task(raw)
        except (ValueError, TypeError, KeyError) as exc:
            QtWidgets.QMessageBox.warning(self, '条件不完整', str(exc))
            return
        self.accept()


class AutomationPanel(QtWidgets.QWidget):
    def __init__(self, send, seed, parent=None):
        super().__init__(parent)
        self._send, self._seed = send, seed
        self._tasks = {}
        self._available = False
        self._pending: str | None = None
        self._sent_at = 0.
        self._last_state_ts = 0.
        root = QtWidgets.QVBoxLayout(self)
        note = QtWidgets.QLabel(
            '后台实盘任务 · 入场与出场独立启停 · 保存不启动\n'
            '本页独立于图表的纸面模式。关闭本窗口不停止任务；断开账户或退出程序会停止。\n'
            '重启后须人工重新启用。修改条件前先暂停两个开关；有仓位时保留交易腿和每层手数。')
        note.setWordWrap(True)
        root.addWidget(note)
        bar = QtWidgets.QHBoxLayout()
        self._buttons = []
        for label, callback in (
            ('从当前组合新建', self._new), ('编辑选中任务', self._edit),
            ('启停入场', lambda: self._control('entry_enabled')),
            ('启停出场', lambda: self._control('exit_enabled')),
            ('立即平选中任务', self._close_now), ('删除', self._delete),
            ('全部暂停', lambda: self._request({'operation': 'pause_all'})),
            ('刷新', self.refresh),
        ):
            button = QtWidgets.QPushButton(label)
            button.clicked.connect(callback)
            bar.addWidget(button)
            self._buttons.append(button)
        root.addLayout(bar)
        self.table = QtWidgets.QTableWidget(0, 10)
        self.table.setHorizontalHeaderLabels(['任务', '固定买卖腿', '入场', '出场', '持仓/目标层',
            '价差', '每层成本估计', '普通单次净息', '持仓净盈亏估计', '状态'])
        self.table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QtWidgets.QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        root.addWidget(self.table)
        self.status = QtWidgets.QLabel('等待账户连接')
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._timeout)
        self._timer.start()
        self.set_available(False)

    def set_available(self, available):
        was = self._available
        self._available = available
        for button in self._buttons:
            button.setEnabled(available and self._pending is None)
        if not available:
            self._pending = None
            self.status.setText('账户未连接；后台执行不可用，表中开关可能是旧快照')
        elif not was:
            self.refresh()

    def _selected(self):
        row = self.table.currentRow()
        item = self.table.item(row, 0) if row >= 0 else None
        return self._tasks.get(item.data(QtCore.Qt.ItemDataRole.UserRole)) if item else None

    def _request(self, payload):
        if not self._available or self._pending:
            return
        request_id = uuid.uuid4().hex
        if self._send({**payload, 'request_id': request_id}):
            self._pending, self._sent_at = request_id, time.monotonic()
            self.status.setText('等待后台确认；开关以回执为准')
            self.set_available(True)
        else:
            self.status.setText('请求未发送：会话未就绪或发送队列已满')

    def refresh(self):
        self._request({'operation': 'snapshot'})

    def update_state(self, payload):
        if payload.get('request_id') == self._pending:
            self._pending = None
            self.set_available(self._available)
        stamp = payload.get('ts', 0.)
        if stamp and stamp < self._last_state_ts:
            return
        self._last_state_ts = max(stamp, self._last_state_ts)
        selected = self._selected()
        selected_id = selected['id'] if selected else None
        if 'tasks' in payload:
            self._tasks = {t['id']: t for t in payload['tasks']}
            self.table.setRowCount(len(self._tasks))
            for row, task in enumerate(self._tasks.values()):
                runtime = task.get('runtime', {})
                def fmt(key):
                    val = runtime.get(key)
                    return '-' if val is None else f'{val:.6f}'
                values = [task['name'], ' + '.join(f"{x['side']} {x['symbol']} {x['volume']}手" for x in task['legs']),
                          '启用' if task['entry_enabled'] else '暂停', '启用' if task['exit_enabled'] else '暂停',
                          f"{runtime.get('layers', '-')}/{task['target_layers']}", fmt('spread'),
                          fmt('roundtrip_cost'), fmt('carry'), fmt('net_liquidation'), runtime.get('status', '-')]
                for col, val in enumerate(values):
                    text = str(val)
                    item = self.table.item(row, col)
                    if item is None:
                        item = QtWidgets.QTableWidgetItem()
                        self.table.setItem(row, col, item)
                    if item.text() != text:
                        item.setText(text)
                        item.setToolTip(text)
                    if col == 0:
                        item.setData(QtCore.Qt.ItemDataRole.UserRole, task['id'])
                if task['id'] == selected_id:
                    self.table.selectRow(row)
        if payload.get('error'):
            self.status.setText('后台提示：' + payload['error'])
        elif not self._pending:
            self.status.setText('后台已同步 · 金额均为账户币种 · 出场优先，多个任务公平轮转')

    def _new(self):
        try:
            seed = self._seed()
        except ValueError as exc:
            self.status.setText(str(exc))
            return
        self._editor(seed)

    def _edit(self):
        task = self._selected()
        if task:
            self._editor(task)

    def _editor(self, seed):
        dialog = TaskEditor(seed, self)
        if dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            self._request({'operation': 'save', 'task_id': dialog.task['id'],
                           'revision': seed.get('revision'), 'task': dialog.task})

    def _control(self, field):
        task = self._selected()
        if task:
            self._request({'operation': 'control', 'task_id': task['id'], 'revision': task['revision'],
                           'field': field, 'enabled': not task[field]})

    def _close_now(self):
        task = self._selected()
        if task and QtWidgets.QMessageBox.question(self, '立即退出',
            '立即平掉该任务已核验归属的全部持仓，不要求盈利，并暂停该任务？') == QtWidgets.QMessageBox.StandardButton.Yes:
            self._request({'operation': 'close_now', 'task_id': task['id'], 'revision': task['revision']})

    def _delete(self):
        task = self._selected()
        if task:
            self._request({'operation': 'delete', 'task_id': task['id'], 'revision': task['revision']})

    def _timeout(self):
        if self._pending and time.monotonic() - self._sent_at > 30:
            self._pending = None
            self.set_available(self._available)
            self.status.setText('回执超时，执行结果未知；请刷新核对，不自动重发')
