from __future__ import annotations

from collections import deque


class MessageWindow:
    def __init__(self, max_size: int = 20):
        self.max_size = max_size
        self._window: deque[dict] = deque(maxlen=max_size)

    def add(self, user_id: str, message: str, is_bot: bool = False) -> None:
        role = "assistant" if is_bot else "user"
        self._window.append({"role": role, "content": message, "user_id": user_id})

    def get_context(self) -> list[dict[str, str]]:
        return [{"role": m["role"], "content": m["content"]} for m in self._window]

    def clear(self) -> None:
        self._window.clear()

    def __len__(self) -> int:
        return len(self._window)
