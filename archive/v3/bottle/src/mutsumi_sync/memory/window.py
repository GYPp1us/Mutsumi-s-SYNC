from typing import List
from collections import deque


class MessageWindow:
    def __init__(self, max_size: int = 20):
        self.max_size = max_size
        self._window: deque = deque(maxlen=max_size)

    def add(self, user_id: int, message: str, is_bot: bool = False):
        role = "bot" if is_bot else "user"
        self._window.append({"role": role, "content": message, "user_id": user_id})

    def get_context(self) -> List[str]:
        return [f"{m['role']}: {m['content']}" for m in self._window]

    def clear(self):
        self._window.clear()
