"""Session-owned state; transport availability and quote freshness are separate."""
from dataclasses import dataclass, replace
from enum import StrEnum


class Phase(StrEnum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    STOPPING = "stopping"
    FAILED = "failed"


_ALLOWED = {
    Phase.DISCONNECTED: {Phase.CONNECTING},
    Phase.CONNECTING: {Phase.CONNECTED, Phase.RECONNECTING, Phase.STOPPING, Phase.FAILED},
    Phase.CONNECTED: {Phase.RECONNECTING, Phase.STOPPING, Phase.FAILED},
    Phase.RECONNECTING: {Phase.CONNECTED, Phase.STOPPING, Phase.FAILED},
    Phase.STOPPING: {Phase.DISCONNECTED, Phase.FAILED},
    Phase.FAILED: {Phase.STOPPING},
}


@dataclass(frozen=True)
class ConnectionState:
    phase: Phase = Phase.DISCONNECTED
    revision: int = 0
    message: str = "未连接"

    def transition(self, phase: Phase, message: str) -> "ConnectionState":
        if phase != self.phase and phase not in _ALLOWED[self.phase]:
            raise ValueError(f"Illegal connection transition: {self.phase} -> {phase}")
        return replace(self, phase=phase, revision=self.revision + 1, message=message)

    def payload(self) -> dict:
        return {"phase": self.phase.value, "ok": self.phase == Phase.CONNECTED,
                "revision": self.revision, "msg": self.message}
