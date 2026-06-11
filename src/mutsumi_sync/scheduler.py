from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Callable, TYPE_CHECKING

from .memory.window import MessageWindow
from .memory.session import SessionState

if TYPE_CHECKING:
    from .config import Config
    from .memory.store import MessageStore
    from .message.receiver import MessageEvent
    from .message.sender import MessageSender, Peer
    from .tools.registry import ToolRegistry

logger = logging.getLogger("mutsumi.scheduler")


@dataclass
class PipelineDeps:
    config: Config
    registry: ToolRegistry
    sender: MessageSender
    store: MessageStore
    window: MessageWindow
    session: SessionState
    peer: Peer
    group_key: str
    on_token: Callable[[str], None] | None = None


class PipelineScheduler:
    def __init__(
        self,
        config: Config,
        registry: ToolRegistry,
        sender: MessageSender,
        store: MessageStore,
    ):
        self.config = config
        self.registry = registry
        self.sender = sender
        self.store = store

        self._windows: dict[str, MessageWindow] = {}
        self._sessions: dict[str, SessionState] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self.on_token: Callable[[str], None] | None = None

    def _make_key(self, event: MessageEvent) -> str:
        if event.message_type == "group" and event.group_id:
            return f"group:{event.group_id}:{event.user_id}"
        return f"private:{event.user_id}"

    @staticmethod
    def _make_peer(event: MessageEvent) -> Peer:
        from .message.sender import Peer
        if event.message_type == "group" and event.group_id:
            return Peer(chat_type=2, peer_uid=str(event.group_id))
        return Peer(chat_type=1, peer_uid=str(event.user_id))

    def _ensure_user_state(self, key: str) -> None:
        if key not in self._windows:
            self._windows[key] = MessageWindow(max_size=self.config.context.window_size)
        if key not in self._sessions:
            self._sessions[key] = SessionState()

    async def dispatch(self, event: MessageEvent) -> None:
        key = self._make_key(event)
        peer = self._make_peer(event)

        await self.cancel_user(key)

        self._ensure_user_state(key)
        window = self._windows[key]
        session = self._sessions[key]

        deps = PipelineDeps(
            config=self.config,
            registry=self.registry,
            sender=self.sender,
            store=self.store,
            window=window,
            session=session,
            peer=peer,
            group_key=key,
            on_token=self.on_token,
        )

        from .pipeline import pipeline
        from .message.classifier import classify_message

        classified = classify_message(event.message, event.raw_message)

        logger.info("[SCHED] dispatching key=%s type=%s", key, classified.msg_type.value)

        task = asyncio.create_task(
            _task_wrapper(
                pipeline(
                    message=classified.content or event.raw_message,
                    msg_type=classified.msg_type,
                    image_file=classified.image_file,
                    image_url=classified.image_url,
                    deps=deps,
                ),
                key,
            )
        )
        self._tasks[key] = task

    async def cancel_user(self, key: str) -> None:
        task = self._tasks.pop(key, None)
        if task is None:
            return
        if not task.done():
            logger.info("[SCHED] cancelling task for %s", key)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    def active_keys(self) -> list[str]:
        return [k for k, t in self._tasks.items() if not t.done()]

    def status(self) -> dict:
        return {
            "active_tasks": len([t for t in self._tasks.values() if not t.done()]),
            "total_windows": len(self._windows),
            "total_sessions": len(self._sessions),
            "task_keys": list(self._tasks.keys()),
            "config_dirty": self.config.dirty,
        }


async def _task_wrapper(coro, key: str) -> None:
    try:
        await coro
    except asyncio.CancelledError:
        logger.info("[SCHED] task cancelled: %s", key)
        raise
    except Exception:
        logger.exception("[SCHED] unhandled error in pipeline for %s", key)
