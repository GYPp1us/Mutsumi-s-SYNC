from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from .memory.window import MessageWindow
from .memory.session import SessionState

if TYPE_CHECKING:
    from .config import Config
    from .memory.store import MessageStore
    from .memory.store import ScheduledTaskRecord
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
    token_counter: dict | None = None
    report_state: Callable[[str], None] | None = None
    report_llm_health: Callable[[bool], None] | None = None
    source: str = "user"
    silent: bool = False
    remember_input: bool = True


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
        self._pipeline_states: dict[str, str] = {}
        self._last_active_key: str | None = None
        self.llm_healthy: bool = True
        self.token_usage: dict = {"input": 0, "output": 0, "cache_hit": 0, "cache_miss": 0}
        self.on_state_change: Callable[[], None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._scheduled_tasks: dict[int, asyncio.Task[None]] = {}

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

    def _notify(self) -> None:
        if self.on_state_change:
            self.on_state_change()

    def _make_report_state(self, key: str) -> Callable[[str], None]:
        def report(state: str) -> None:
            self._pipeline_states[key] = state
            self._last_active_key = key
            self._notify()
        return report

    def _make_report_llm_health(self) -> Callable[[bool], None]:
        def report(healthy: bool) -> None:
            self.llm_healthy = healthy
            self._notify()
        return report

    def set_pipeline_state(self, key: str, state: str) -> None:
        self._pipeline_states[key] = state
        self._notify()

    def clear_pipeline_state(self, key: str) -> None:
        self._pipeline_states.pop(key, None)
        self._notify()

    def _ensure_user_state(self, key: str) -> None:
        if key not in self._windows:
            self._windows[key] = MessageWindow()
        if key not in self._sessions:
            self._sessions[key] = SessionState()

    @staticmethod
    def _peer_from_key(key: str) -> Peer:
        from .message.sender import Peer
        parts = key.split(":")
        if len(parts) >= 2 and parts[0] == "group":
            return Peer(chat_type=2, peer_uid=parts[1])
        if len(parts) >= 2 and parts[0] == "private":
            return Peer(chat_type=1, peer_uid=parts[1])
        return Peer(chat_type=1, peer_uid="heartbeat")

    def _select_heartbeat_key(self) -> str:
        if self.config.heartbeat.aggressive_provider_cache_retention:
            if self._last_active_key and self._last_active_key in self._windows:
                return self._last_active_key
            if self._windows:
                return next(iter(self._windows.keys()))
        return "private:heartbeat"

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
            token_counter=self.token_usage,
            report_state=self._make_report_state(key),
            report_llm_health=self._make_report_llm_health(),
        )

        logger.info("[SCHED] dispatching merged key=%s type=%s msgs=%d", key, final_type.value, len(events))

        async def _run():
            try:
                await pipeline(
                    message=merged_message,
                    msg_type=final_type,
                    image_file=final_image_file,
                    image_url=final_image_url,
                    deps=deps,
                )
            except asyncio.CancelledError:
                self.set_pipeline_state(key, "CANCELLED")
                raise
            except Exception:
                logger.exception("[SCHED] unhandled error in pipeline for %s", key)
            finally:
                self.clear_pipeline_state(key)

        task = asyncio.create_task(_run())
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
            token_counter=self.token_usage,
            report_state=self._make_report_state(key),
            report_llm_health=self._make_report_llm_health(),
        )

        logger.info("[SCHED] dispatching direct key=%s type=%s", key, classified.msg_type.value)

        async def _run():
            try:
                await pipeline(
                    message=classified.content or event.raw_message,
                    msg_type=classified.msg_type,
                    image_file=classified.image_file,
                    image_url=classified.image_url,
                    deps=deps,
                )
            except asyncio.CancelledError:
                self.set_pipeline_state(key, "CANCELLED")
                raise
            except Exception:
                logger.exception("[SCHED] unhandled error in pipeline for %s", key)
            finally:
                self.clear_pipeline_state(key)

        task = asyncio.create_task(_run())
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
        self._notify()

    def active_keys(self) -> list[str]:
        return [k for k, t in self._tasks.items() if not t.done()]

    def status(self) -> dict:
        return {
            "active_tasks": len([t for t in self._tasks.values() if not t.done()]),
            "total_windows": len(self._windows),
            "total_sessions": len(self._sessions),
            "task_keys": list(self._tasks.keys()),
            "config_dirty": self.config.dirty,
            "registry_version": self.registry.version,
            "token_usage": dict(self.token_usage),
            "pipeline_states": dict(self._pipeline_states),
            "last_active_key": self._last_active_key,
            "llm_healthy": self.llm_healthy,
        }

    async def startup(self) -> None:
        logger.info("[STARTUP] restoring windows from database")
        group_keys = await self.store.get_message_group_keys()

        for gk in group_keys:
            boundary = await self.store.get_newest_summary(gk)
            after_id = boundary["last_message_id"] if boundary else 0

            uncovered = await self.store.get_messages_after(gk, after_id, limit=200)
            if not uncovered:
                continue

            window = MessageWindow()
            for msg in uncovered:
                try:
                    parsed = json.loads(msg["content"])
                except (json.JSONDecodeError, TypeError):
                    window.add(user_id=gk, message=msg["content"][:500], created_at=msg.get("created_at"))
                    continue

                if isinstance(parsed, dict):
                    user_text = parsed.get("user", "")
                    bot_text = parsed.get("bot", "")
                    if user_text:
                        window.add(user_id=gk, message=str(user_text), created_at=msg.get("created_at"))
                    if bot_text:
                        window.add(user_id=gk, message=str(bot_text), is_bot=True, created_at=msg.get("created_at"))
                else:
                    window.add(user_id=gk, message=str(parsed)[:500], created_at=msg.get("created_at"))

            self._windows[gk] = window
            self._ensure_user_state(gk)
            logger.info("[STARTUP] restored window %s: %d items (after id %d)", gk, len(window), after_id)

        if self.config.heartbeat.enabled and self._heartbeat_task is None:
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        pending_scheduled_tasks = await self.store.get_pending_scheduled_tasks()
        for record in pending_scheduled_tasks:
            self._start_scheduled_task(record)
        if pending_scheduled_tasks:
            logger.info("[SCHEDULE] restored %d pending tasks", len(pending_scheduled_tasks))

    async def _heartbeat_loop(self) -> None:
        interval = max(60, int(self.config.heartbeat.interval_seconds))
        logger.info("[HEARTBEAT] enabled interval=%ss aggressive_cache=%s",
                    interval, self.config.heartbeat.aggressive_provider_cache_retention)
        try:
            while True:
                await asyncio.sleep(interval)
                await self.run_heartbeat_once()
        except asyncio.CancelledError:
            logger.info("[HEARTBEAT] stopped")
            raise

    async def run_heartbeat_once(self) -> None:
        from .message.classifier import MessageType
        from .pipeline import pipeline

        key = self._select_heartbeat_key()
        self._ensure_user_state(key)
        peer = self._peer_from_key(key)
        deps = PipelineDeps(
            config=self.config,
            registry=self.registry,
            sender=self.sender,
            store=self.store,
            window=self._windows[key],
            session=self._sessions[key],
            peer=peer,
            group_key=key,
            token_counter=self.token_usage,
            report_state=self._make_report_state(key),
            report_llm_health=self._make_report_llm_health(),
            source="heartbeat",
            silent=True,
            remember_input=False,
        )
        logger.info("[HEARTBEAT] triggering pipeline key=%s", key)
        await pipeline(
            message="[HEARTBEAT] Run a real health check. Do not send a visible reply; call no_reply if available.",
            msg_type=MessageType.SHORT_TEXT,
            image_file=None,
            image_url=None,
            deps=deps,
        )

    async def schedule_once(self, *, scheduled_at: float, prompt: str, group_key: str, peer: Peer) -> int:
        task_id = await self.store.add_scheduled_task(
            group_key=group_key,
            peer_chat_type=peer.chat_type,
            peer_uid=peer.peer_uid,
            prompt=prompt,
            scheduled_at=scheduled_at,
        )
        from .memory.store import ScheduledTaskRecord

        record = ScheduledTaskRecord(
            id=task_id,
            group_key=group_key,
            peer_chat_type=peer.chat_type,
            peer_uid=peer.peer_uid,
            prompt=prompt,
            scheduled_at=scheduled_at,
            status="pending",
            created_at=time.time(),
        )
        self._start_scheduled_task(record)
        logger.info("[SCHEDULE] registered task_id=%s key=%s trigger_at=%s", task_id, group_key, scheduled_at)
        return task_id

    def _start_scheduled_task(self, record: ScheduledTaskRecord) -> None:
        existing = self._scheduled_tasks.pop(record.id, None)
        if existing and not existing.done():
            existing.cancel()
        self._scheduled_tasks[record.id] = asyncio.create_task(self._scheduled_sleep_and_fire(record))

    async def _scheduled_sleep_and_fire(self, record: ScheduledTaskRecord) -> None:
        try:
            delay = max(0.0, record.scheduled_at - time.time())
            if delay:
                await asyncio.sleep(delay)
            await self._fire_scheduled_task(record)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[SCHEDULE] task_id=%s failed", record.id)
            await self.store.mark_scheduled_task_status(record.id, "error")
        finally:
            self._scheduled_tasks.pop(record.id, None)

    async def _fire_scheduled_task(self, record: ScheduledTaskRecord) -> None:
        from .message.classifier import MessageType
        from .message.sender import Peer
        from .pipeline import pipeline

        logger.info("[SCHEDULE] firing task_id=%s key=%s", record.id, record.group_key)
        await self.store.mark_scheduled_task_status(record.id, "running")

        await self.cancel_user(record.group_key)
        self._ensure_user_state(record.group_key)
        peer = Peer(chat_type=record.peer_chat_type, peer_uid=record.peer_uid)

        deps = PipelineDeps(
            config=self.config,
            registry=self.registry,
            sender=self.sender,
            store=self.store,
            window=self._windows[record.group_key],
            session=self._sessions[record.group_key],
            peer=peer,
            group_key=record.group_key,
            token_counter=self.token_usage,
            report_state=self._make_report_state(record.group_key),
            report_llm_health=self._make_report_llm_health(),
            source="schedule",
            silent=False,
            remember_input=True,
        )
        await pipeline(
            message=f"[SCHEDULED:{record.id}] {record.prompt}",
            msg_type=MessageType.SHORT_TEXT,
            image_file=None,
            image_url=None,
            deps=deps,
        )
        await self.store.mark_scheduled_task_status(record.id, "done")

    async def shutdown(self) -> None:
        logger.info("[SHUTDOWN] stopping scheduler with %d windows", len(self._windows))

        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            self._heartbeat_task = None

        for task in list(self._scheduled_tasks.values()):
            task.cancel()
        for task in list(self._scheduled_tasks.values()):
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._scheduled_tasks.clear()

        for key in list(self._keys()):
            self._cleanup_debounce(key)
            await self.cancel_user(key)

        await self.store.close()
        logger.info("[SHUTDOWN] complete")

    def _keys(self) -> list[str]:
        return list(self._windows.keys())
