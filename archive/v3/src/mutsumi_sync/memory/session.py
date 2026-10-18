from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class SessionState:
    last_active: float = field(default_factory=time.time)
    is_pending: bool = False

    def is_cold(self, timeout: float) -> bool:
        return (time.time() - self.last_active) > timeout

    def touch(self) -> None:
        self.last_active = time.time()

    def mark_pending(self) -> None:
        self.is_pending = True

    def clear_pending(self) -> None:
        self.is_pending = False
