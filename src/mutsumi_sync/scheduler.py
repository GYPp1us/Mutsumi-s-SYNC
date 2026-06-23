from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

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
        self._pending_events: dict[str, list[MessageEvent]] = {}
        self._debounce_timers: dict[str, asyncio.Task[None]] = {}

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
            self._windows[key] = MessageWindow()
        if key not in self._sessions:
            self._sessions[key] = SessionState()

    def _cancel_debounce_timer(self, key: str) -> None:
        timer = self._debounce_timers.pop(key, None)
        if timer and not timer.done():
            timer.cancel()

    def _cleanup_debounce(self, key: str) -> None:
        self._cancel_debounce_timer(key)
        self._pending_events.pop(key, None)

    async def dispatch(self, event: MessageEvent) -> None:
        key = self._make_key(event)

        from .message.classifier import classify_message
        classified = classify_message(event.message, event.raw_message)
        if classified.msg_type.value in ("image", "media"):
            self._cleanup_debounce(key)
            await self._dispatch_direct(key, event, classified)
            return

        if key not in self._pending_events:
            self._pending_events[key] = []
        self._pending_events[key].append(event)

        self._cancel_debounce_timer(key)
        self._debounce_timers[key] = asyncio.create_task(self._debounce_expire(key))

    async def _debounce_expire(self, key: str) -> None:
        await asyncio.sleep(self.config.context.debounce_timeout)
        events = self._pending_events.pop(key, [])
        self._debounce_timers.pop(key, None)
        if not events:
            return

        from .message.classifier import classify_message, MessageType

        texts: list[str] = []
        final_type = MessageType.SHORT_TEXT
        final_image_file: str | None = None
        final_image_url: str | None = None
        for ev in events:
            c = classify_message(ev.message, ev.raw_message)
            if c.content:
                texts.append(c.content)
            if c.msg_type == MessageType.IMAGE:
                final_type = MessageType.IMAGE
                final_image_file = c.image_file or final_image_file
                final_image_url = c.image_url or final_image_url

        merged_message = "\n".join(texts)
        if len(merged_message) >= 50:
            final_type = MessageType.LONG_TEXT

        PEER = self._make_peer(events[0])

        await self.cancel_user(key)
        self._ensure_user_state(key)

        from .pipeline import pipeline

        deps = PipelineDeps(
            config=self.config,
            registry=self.registry,
            sender=self.sender,
            store=self.store,
            window=self._windows[key],
            session=self._sessions[key],
            peer=PEER,
            group_key=key,
        )

        logger.info("[SCHED] dispatching merged key=%s type=%s msgs=%d", key, final_type.value, len(events))

        task = asyncio.create_task(
            _task_wrapper(
                pipeline(
                    message=merged_message,
                    msg_type=final_type,
                    image_file=final_image_file,
                    image_url=final_image_url,
                    deps=deps,
                ),
                key,
            )
        )
        self._tasks[key] = task

    async def _dispatch_direct(self, key: str, event: MessageEvent, classified) -> None:
        PEER = self._make_peer(event)
        await self.cancel_user(key)
        self._ensure_user_state(key)

        from .pipeline import pipeline

        deps = PipelineDeps(
            config=self.config,
            registry=self.registry,
            sender=self.sender,
            store=self.store,
            window=self._windows[key],
            session=self._sessions[key],
            peer=PEER,
            group_key=key,
        )

        logger.info("[SCHED] dispatching direct key=%s type=%s", key, classified.msg_type.value)

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
        self._cleanup_debounce(key)
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
