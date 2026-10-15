"""添加/编辑账户对话框：仅凭据 + 绑定终端（订阅品种由控制台按需订阅）。"""

from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets


class AccountDialog(QtWidgets.QDialog):
    def __init__(
        self,
        parent: QtWidgets.QWidget | None = None,
        initial: dict | None = None,
        terminals: list[dict] | None = None,
    ) -> None:
        super().__init__(parent)
        self._initial_symbols = list((initial or {}).get("symbols", []))
        self.setWindowTitle("账户信息")
        self.setMinimumWidth(480)

        self._login = QtWidgets.QLineEdit()
        self._login.setValidator(QtGui.QRegularExpressionValidator(QtCore.QRegularExpression("[1-9][0-9]{0,18}")))

        self._legacy_password = (initial or {}).get("password", "")
        self._clear_password = QtWidgets.QCheckBox("删除此账户已保存的旧密码（attach无需密码）")
        self._clear_password.setVisible(bool(self._legacy_password))

        self._server = QtWidgets.QLineEdit()
        self._server.setPlaceholderText("如 MetaQuotes-Demo 或经纪商服务器名")
        self._broker_timezone = QtWidgets.QLineEdit((initial or {}).get('broker_timezone', 'UTC'))
        self._broker_timezone.setToolTip('MT5默认UTC；仅在核实API时间偏移后设置UTC+03:00或IANA时区（含历史DST规则）')
        self._quote_age = QtWidgets.QSpinBox()
        self._quote_age.setRange(5, 600)
        self._quote_age.setValue(int((initial or {}).get('quote_max_age', 60)))
        self._quote_age.setSuffix(' 秒')

        # ---------------------------------------------------------------
        # 终端绑定：仅列出真实存在的终端（完整绝对路径），失效项直接过滤
        # ---------------------------------------------------------------
        self._terminal = QtWidgets.QComboBox()
        from pathlib import Path as _Path
        for t in terminals or []:
            path = t.get("path") or ""
            if not path or not _Path(path).exists():
                continue  # 幽灵/失效终端不再出现在绑定下拉框
            item_text = path
            self._terminal.addItem(item_text, t["id"])
            self._terminal.setItemData(
                self._terminal.count() - 1, path, QtCore.Qt.ToolTipRole
            )
        self._terminal.addItem("(未绑定，请先选择终端)", None)
        btn_browse = QtWidgets.QPushButton("浏览…")
        btn_browse.clicked.connect(self._browse_terminal)

        terminal_row = QtWidgets.QHBoxLayout()
        terminal_row.addWidget(self._terminal, stretch=1)
        terminal_row.addWidget(btn_browse)

        form = QtWidgets.QFormLayout()
        form.addRow("账户", self._login)
        form.addRow("", QtWidgets.QLabel("在MT5终端中登录；本工具不收集新密码"))
        form.addRow("", self._clear_password)
        form.addRow("服务器", self._server)
        form.addRow("绑定终端", terminal_row)
        form.addRow('报价时间基准', self._broker_timezone)
        form.addRow('市场报价最大年龄', self._quote_age)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self._validate)
        buttons.rejected.connect(self.reject)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

        if initial:
            self._login.setText(str(initial.get("login", 1)))
            self._login.setReadOnly(True)
            self._server.setReadOnly(True)
            self._server.setText(initial.get("server", ""))
            path = initial.get("terminal_path") or ""
            for i in range(self._terminal.count() - 1):
                if self._terminal.itemText(i) == path:
                    self._terminal.setCurrentIndex(i)
                    break
            else:
                self._terminal.setCurrentIndex(self._terminal.count() - 1)

    def _browse_terminal(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "选择 terminal64.exe", "", "MetaTrader 5 (terminal64.exe)"
        )
        if not path:
            return
        existing_idx = None
        for i in range(self._terminal.count()):
            if self._terminal.itemText(i) == path:
                existing_idx = i
                break
        if existing_idx is None:
            self._terminal.insertItem(0, path, None)
            existing_idx = 0
        self._terminal.setCurrentIndex(existing_idx)

    def _validate(self) -> None:
        from ..broker_clock import BrokerClock
        try:
            BrokerClock(self._broker_timezone.text())
        except ValueError as exc:
            QtWidgets.QMessageBox.warning(self, '时间基准', str(exc))
            return
        if not self._server.text().strip():
            QtWidgets.QMessageBox.warning(self, "提示", "请输入服务器名称")
            return
        if not self._login.hasAcceptableInput() or int(self._login.text()) > 2**63 - 1:
            QtWidgets.QMessageBox.warning(self, "提示", "请输入有效的正整数登录号（不超过64位有符号整数）")
            return
        if self._terminal.currentText().startswith("(未绑定"):
            QtWidgets.QMessageBox.warning(self, "提示", "attach 模式需要绑定终端，请先选择终端")
            return
        self.accept()

    def credentials(self) -> dict:
        text = self._terminal.currentText()
        auto = text.startswith("(未绑定")
        terminal_path = None if auto else text
        return {
            "login": int(self._login.text()),
            "password": "" if self._clear_password.isChecked() else self._legacy_password,
            "server": self._server.text().strip(),
            "symbols": list(self._initial_symbols),
            "terminal_id": self._terminal.currentData(),
            "terminal_path": terminal_path,
            'broker_timezone': self._broker_timezone.text().strip() or 'UTC',
            'quote_max_age': self._quote_age.value(),
        }
