from __future__ import annotations

import time
from collections import deque


class MessageWindow:
    def __init__(self, max_size: int = 20):
        self.max_size = max_size
        self._window: deque[dict] = deque()

    def add(self, user_id: str, message: str, is_bot: bool = False, created_at: float | None = None) -> None:
        role = "assistant" if is_bot else "user"
        self._window.append({
            "role": role,
            "content": message,
            "user_id": user_id,
            "created_at": created_at if created_at is not None else time.time(),
        })

    def replace(self, items: list[dict]) -> None:
        self._window.clear()
        self._window.extend(items)

    def get_context(self) -> list[dict]:
        return [
            {
                "role": m["role"],
                "content": m["content"],
                "created_at": m.get("created_at"),
            }
            for m in self._window
        ]

    def clear(self) -> None:
        self._window.clear()

    def __len__(self) -> int:
        return len(self._window)

    def __iter__(self):
        return iter(self._window)
