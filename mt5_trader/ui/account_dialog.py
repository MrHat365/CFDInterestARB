"""添加/编辑账户对话框：仅凭据 + 绑定终端（订阅品种由控制台按需订阅）。"""

from __future__ import annotations

from PySide6 import QtCore, QtWidgets


class AccountDialog(QtWidgets.QDialog):
    def __init__(
        self,
        parent: QtWidgets.QWidget | None = None,
        initial: dict | None = None,
        terminals: list[dict] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("账户信息")
        self.setMinimumWidth(480)

        self._login = QtWidgets.QSpinBox()
        self._login.setRange(1, 999_999_999)

        self._password = QtWidgets.QLineEdit()
        self._password.setEchoMode(QtWidgets.QLineEdit.Password)

        self._server = QtWidgets.QLineEdit()
        self._server.setPlaceholderText("如 MetaQuotes-Demo 或经纪商服务器名")

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
        self._terminal.addItem("(自动/默认终端)", None)
        btn_browse = QtWidgets.QPushButton("浏览…")
        btn_browse.clicked.connect(self._browse_terminal)

        terminal_row = QtWidgets.QHBoxLayout()
        terminal_row.addWidget(self._terminal, stretch=1)
        terminal_row.addWidget(btn_browse)

        form = QtWidgets.QFormLayout()
        form.addRow("账户", self._login)
        form.addRow("密码", self._password)
        form.addRow("服务器", self._server)
        form.addRow("绑定终端", terminal_row)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self._validate)
        buttons.rejected.connect(self.reject)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

        if initial:
            self._login.setValue(int(initial.get("login", 1)))
            self._password.setText(initial.get("password", ""))
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
        if not self._server.text().strip():
            QtWidgets.QMessageBox.warning(self, "提示", "请输入服务器名称")
            return
        if not self._password.text():
            QtWidgets.QMessageBox.warning(self, "提示", "请输入密码")
            return
        self.accept()

    def credentials(self) -> dict:
        text = self._terminal.currentText()
        auto = "(自动/默认终端)" in text
        terminal_path = None if auto else text
        return {
            "login": int(self._login.value()),
            "password": self._password.text(),
            "server": self._server.text().strip(),
            "symbols": [],
            "terminal_id": self._terminal.currentData(),
            "terminal_path": terminal_path,
        }
