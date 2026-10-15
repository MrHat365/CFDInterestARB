"""Application-owned sessions; views do not create competing workers."""
import os
import threading

from .account_session import AccountSession
from .connection_state import Phase


class SessionRegistry:
    def __init__(self, engine, execution_db: str | None = None):
        self.engine = engine
        self.execution_db = execution_db
        self._sessions: dict[str, AccountSession] = {}
        self._lock = threading.RLock()

    def get(self, key: str) -> AccountSession | None:
        with self._lock:
            return self._sessions.get(key)

    def connect(self, account: dict) -> AccountSession:
        key = f'{account["login"]}@{account["server"]}'
        with self._lock:
            old = self._sessions.get(key)
            if old and not old.closed.is_set():
                return old
            terminal_path = account.get("terminal_path")
            if not terminal_path or not str(terminal_path).strip():
                raise ValueError('请先绑定终端，再连接账户')
            path = os.path.normcase(os.path.realpath(terminal_path))
            for name, session in self._sessions.items():
                other = os.path.normcase(os.path.realpath(session.account.get("terminal_path") or ""))
                if name != key and not session.closed.is_set() and path == other:
                    raise ValueError(f"终端正被 {name} 使用，请先断开该账户")
            session = AccountSession(self.engine, {**account, "execution_db": self.execution_db})
            self._sessions[key] = session
            session.start()
            return session

    def disconnect(self, key: str) -> None:
        with self._lock:
            session = self._sessions.get(key)
            if not session or session.closed.is_set() or session.state.phase == Phase.STOPPING:
                return
            # Mark stopping before returning; a rapid reconnect cannot spawn a
            # second worker while the first is completing broker operations.
            session.begin_stop()
            threading.Thread(target=session.stop, name=f"stop-{key}", daemon=True).start()

    def reclaim(self, key: str) -> None:
        session = self.get(key)
        if session is not None:
            threading.Thread(target=session.reclaim, name=f'reclaim-{key}', daemon=True).start()

    def is_current(self, event) -> bool:
        data = event.data
        if not isinstance(data, dict):
            return False
        session = self.get(data.get("session", ""))
        return session is not None and data.get("generation") == session.generation

    def close(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
        # Begin cancellation for every account before waiting for any one.
        for session in sessions:
            self.disconnect(session.name)
        for session in sessions:
            session.closed.wait(48)
