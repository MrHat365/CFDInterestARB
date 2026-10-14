"""账户管理主视图：增删改查 + 终端绑定 + 打开控制台。"""

from __future__ import annotations

from pathlib import Path

from PySide6 import QtCore, QtGui, QtWidgets

from ..accounts import AccountsStore
from ..constants import EVENT_STATUS
from ..database import Database
from ..event_engine import EventEngine
from .account_dialog import AccountDialog
from .base import EventBridge
from .console import AccountConsole

COLUMNS = ["登录号", "服务器", "绑定终端", "数据目录", "连接状态", "操作"]


class TerminalScanThread(QtCore.QThread):
    """后台全盘扫描（注册表+进程+常见目录+全盘 BFS）。"""

    found = QtCore.Signal(dict)
    finished_scan = QtCore.Signal(int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._cancel = False

    def cancel(self) -> None:
        self._cancel = True

    def run(self) -> None:
        count = 0
        from ..terminals import iter_deep_scan, scan

        # 快速三路：注册表/进程/常见目录/APPDATA
        for item in scan(deep=False):
            if self._cancel:
                break
            self.found.emit(item)
            count += 1
        # 全盘 BFS 兜底
        for item in iter_deep_scan(should_cancel=lambda: self._cancel):
            if self._cancel:
                break
            self.found.emit(item)
            count += 1
        self.finished_scan.emit(count)


class AccountsView(QtWidgets.QWidget):
    """账户列表 + 操作列（打开控制台）；启动即展示。"""

    console_requested = QtCore.Signal(dict)

    def __init__(self, event_engine: EventEngine, store: AccountsStore,
                 db: Database, parent=None) -> None:
        super().__init__(parent)
        self._ee = event_engine
        self._store = store
        self._db = db
        self._consoles: dict[str, AccountConsole] = {}
        self._scan_thread: TerminalScanThread | None = None

        # --------------------------------------------------------------
        # 工具栏
        # --------------------------------------------------------------
        toolbar = QtWidgets.QHBoxLayout()
        btn_add = QtWidgets.QPushButton("添加账户 (Ctrl+A)")
        btn_add.clicked.connect(self._add_account)
        btn_del = QtWidgets.QPushButton("删除选中")
        btn_del.clicked.connect(self._delete_selected)
        btn_edit = QtWidgets.QPushButton("编辑选中")
        btn_edit.clicked.connect(self._edit_selected)
        btn_scan = QtWidgets.QPushButton("扫描 MT5 客户端")
        btn_scan.clicked.connect(self._scan_terminals)
        for b in (btn_add, btn_edit, btn_del, btn_scan):
            toolbar.addWidget(b)
        toolbar.addStretch()

        shortcut = QtGui.QShortcut(QtGui.QKeySequence(QtCore.Qt.Key_Return), self)
        shortcut.activated.connect(self._open_selected_console)

        # --------------------------------------------------------------
        # 账户表（列宽：用户可拖动调整；绑定终端列吸收余量；操作列保证按钮不压缩）
        # --------------------------------------------------------------
        self._table = QtWidgets.QTableWidget(0, len(COLUMNS))
        self._table.setHorizontalHeaderLabels(COLUMNS)
        self._table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self._table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
        header = self._table.horizontalHeader()
        # 先全部 Interactive（允许鼠标拖动调宽），再单独设吸收余量的列
        header.setSectionResizeMode(QtWidgets.QHeaderView.Interactive)
        header.setStretchLastSection(False)
        header.setMinimumSectionSize(60)
        for col, width in ((0, 96), (1, 150), (3, 220), (4, 100), (5, 210)):
            header.resizeSection(col, width)
        header.setSectionResizeMode(2, QtWidgets.QHeaderView.Stretch)   # 绑定终端列吸收余量
        self._table.verticalHeader().setVisible(False)
        self._table.verticalHeader().setDefaultSectionSize(32)
        self._table.cellDoubleClicked.connect(lambda *_: self._open_selected_console())
        self._table.setSelectionMode(QtWidgets.QAbstractItemView.SingleSelection)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addLayout(toolbar)
        layout.addWidget(self._table)

        self._status_bridge = EventBridge(event_engine, EVENT_STATUS)
        self._status_bridge.event_received.connect(self._on_status)

        self._reload()

    # ------------------------------------------------------------------
    def _reload(self) -> None:
        accounts = self._store.all()
        self._table.setRowCount(len(accounts))
        self._buttons: dict[str, tuple] = {}
        for row, acc in enumerate(accounts):
            login = str(acc["login"])
            key = f"{login}@{acc['server']}"
            self._table.setItem(row, 0, QtWidgets.QTableWidgetItem(login))
            self._table.setItem(row, 1, QtWidgets.QTableWidgetItem(acc["server"]))
            # 绑定终端：直接显示完整绝对路径；失效的用红色标出
            terminal_path = acc.get("terminal_path") or "(自动/默认终端)"
            exists = Path(terminal_path).exists() if acc.get("terminal_path") else True
            terminal_item = QtWidgets.QTableWidgetItem(terminal_path)
            terminal_item.setToolTip(terminal_path)
            if acc.get("terminal_path") and not exists:
                terminal_item.setForeground(QtGui.QColor("#e05661"))
                terminal_item.setText(terminal_path + "  ← 已失效（文件不存在）")
            self._table.setItem(row, 2, terminal_item)
            self._table.setItem(
                row, 3, QtWidgets.QTableWidgetItem(acc.get("terminal_data") or "-")
            )
            status_item = QtWidgets.QTableWidgetItem("未连接")
            self._table.setItem(row, 4, status_item)

            # 操作列：打开控制台 + 编辑 + 删除
            cell_widget = QtWidgets.QWidget()
            cell_layout = QtWidgets.QHBoxLayout(cell_widget)
            cell_layout.setContentsMargins(2, 0, 2, 0)
            btn_console = QtWidgets.QPushButton("控制台")
            btn_console.setToolTip("打开该账户的独立控制台")
            btn_console.clicked.connect(lambda _c=False, a=dict(acc): self._open_console(a))
            btn_edit = QtWidgets.QPushButton("编辑")
            btn_edit.clicked.connect(lambda _c=False, a=dict(acc): self._edit_account(a))
            btn_del = QtWidgets.QPushButton("删除")
            btn_del.clicked.connect(lambda _c=False, r=row: self._delete_row(r))
            cell_layout.addWidget(btn_console)
            cell_layout.addWidget(btn_edit)
            cell_layout.addWidget(btn_del)
            self._table.setCellWidget(row, 5, cell_widget)
            self._buttons[key] = (row, status_item)

    # ------------------------------------------------------------------
    def _add_account(self) -> None:
        dialog = AccountDialog(self, terminals=self._store.terminals())
        if dialog.exec() != QtWidgets.QDialog.Accepted:
            return
        creds = dialog.credentials()
        self._store.add(creds)
        self._reload()

    def _edit_selected(self) -> None:
        row = self._current_row()
        if row is None:
            return
        acc = self._store.all()[row]
        self._edit_account(acc)

    def _edit_account(self, acc: dict) -> None:
        dialog = AccountDialog(self, initial=acc, terminals=self._store.terminals())
        if dialog.exec() != QtWidgets.QDialog.Accepted:
            return
        creds = dialog.credentials()
        self._store.add(creds)
        self._reload()

    def _delete_selected(self) -> None:
        row = self._current_row()
        if row is None:
            return
        self._delete_row(row)

    def _delete_row(self, row: int) -> None:
        acc = self._store.all()[row]
        key = f"{acc['login']}@{acc['server']}"
        if QtWidgets.QMessageBox.question(
            self, "删除账户", f"删除 {key}？（控制台将一并关闭）"
        ) == QtWidgets.QMessageBox.No:
            return
        console = self._consoles.pop(key, None)
        if console:
            console.stop_session_and_close()
        self._store.remove(acc["login"], acc["server"])
        self._reload()

    def _current_row(self) -> int | None:
        idx = self._table.currentRow()
        return idx if idx >= 0 else None

    # ------------------------------------------------------------------
    # 扫描（后台线程 + 进度对话框）
    # ------------------------------------------------------------------
    def _scan_terminals(self) -> None:
        if self._scan_thread and self._scan_thread.isRunning():
            return
        progress = QtWidgets.QProgressDialog(
            "正在扫描本机 MT5 客户端（注册表 → 运行中进程 → 已知目录 → 全盘 BFS）…",
            "取消", 0, 0, self,
        )
        progress.setWindowTitle("扫描")
        progress.setMinimumDuration(0)
        progress.setWindowModality(QtCore.Qt.WindowModal)

        hits: list[str] = []

        thread = TerminalScanThread()
        self._scan_thread = thread

        def on_found(item: dict) -> None:
            rows = self._store.sync_terminals([item])
            label = item.get("path") or item.get("data_folder")
            if label and label not in hits:
                hits.append(label)
                self._dialog_label(progress, label)

        def on_finished(count: int) -> None:
            progress.close()
            # 刷新绑定下拉框的数据源（下一次添加账户对话框可见）
            QtWidgets.QMessageBox.information(
                self, "扫描完成",
                f"新增/确认 {count} 个终端，去重后本机累计 {len(self._store.terminals())} 个：\n"
                + ("\n".join(t["path"] or t["data_folder"] for t in self._store.terminals()))
            )

        def labeled(dialog: QtWidgets.QProgressDialog, text: str) -> None:
            dialog.setLabelText(text)

        progress.canceled.connect(thread.cancel)
        thread.found.connect(on_found)
        thread.finished_scan.connect(on_finished)
        self._progress_label = lambda d, t: labeled(d, t)
        thread.start()

        progress.exec()  # 模态等待（可取消）

        thread.cancel() if thread.isRunning() else None
        thread.wait(3000)

    @staticmethod
    def _dialog_label(progress, text: str) -> None:
        progress.setLabelText(f"发现:\n{text}\n…继续扫描")

    # ------------------------------------------------------------------
    def _open_selected_console(self) -> None:
        row = self._current_row()
        if row is None:
            return
        accounts = self._store.all()
        if row < len(accounts):
            self._open_console(accounts[row])

    def _open_console(self, acc: dict) -> None:
        key = f"{acc['login']}@{acc['server']}"
        if key in self._consoles:
            console = self._consoles[key]
            console.showNormal(); console.raise_(); console.activateWindow()
            return
        console = AccountConsole(
            self._ee, acc, self._store, self._db,
            on_closed=lambda k=key: self._consoles.pop(k, None),
        )
        self._consoles[key] = console
        console.show()

    def _on_status(self, event) -> None:
        key = event.data.get("session", "")
        ok = bool(event.data.get("payload", {}).get("ok"))
        mapping = self._buttons.get(key)
        if mapping:
            row, item = mapping
            item.setText("已连接" if ok else "未连接")
            item.setForeground(QtGui.QColor("#5fb878") if ok else QtGui.QColor("#e05661"))

    def close_all_consoles(self) -> None:
        """主窗口关闭 = 全部控制台一并关闭；单个异常绝不拖垮其它窗口。"""
        for console in list(self._consoles.values()):
            try:
                console.stop_session_and_close()
            except Exception:
                pass
            try:
                console.close()
            except Exception:
                pass
        self._consoles.clear()
