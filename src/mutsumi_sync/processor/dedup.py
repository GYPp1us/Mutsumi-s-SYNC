import asyncio
from typing import Dict, Optional
from dataclasses import dataclass


@dataclass
class PendingReply:
    user_id: int
    group_id: Optional[int]
    timer: asyncio.Task
    cancelled: bool = False


class Deduplicator:
    def __init__(self, wait_time: float = 1.0):
        self.wait_time = wait_time
        self._pending: Dict[tuple, PendingReply] = {}

    def _make_key(self, user_id: int, group_id: Optional[int]) -> tuple:
        return (user_id, group_id) if group_id else (user_id, None)

    async def should_reply(self, user_id: int, group_id: Optional[int] = None) -> bool:
        """检查是否应该回复"""
        key = self._make_key(user_id, group_id)
        
        if key in self._pending:
            pending = self._pending[key]
            if not pending.cancelled:
                pending.timer.cancel()
                pending.cancelled = True
            del self._pending[key]
            return True
        
        return True

    async def schedule_reply(self, user_id: int, group_id: Optional[int] = None):
        """安排延迟回复"""
        key = self._make_key(user_id, group_id)
        timer = asyncio.create_task(asyncio.sleep(self.wait_time))
        self._pending[key] = PendingReply(user_id, group_id, timer)
        return timer

    def cancel_all(self):
        """取消所有待处理回复"""
        for pending in self._pending.values():
            if not pending.cancelled:
                pending.timer.cancel()
                pending.cancelled = True
        self._pending.clear()
