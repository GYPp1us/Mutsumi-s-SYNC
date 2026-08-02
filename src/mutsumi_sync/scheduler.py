from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import logging
import math
import time
import uuid
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Callable

from .memory.window import MessageWindow
from .memory.session import SessionState
from .opencode import OpenCodeRunner

if TYPE_CHECKING:
    from .config import Config
    from .memory.store import MessageStore
    from .memory.store import ScheduledTaskRecord
    from .message.receiver import MessageEvent, ServiceMessageEvent
    from .message.sender import MessageSender, Peer
    from .tools.registry import ToolRegistry

logger = logging.getLogger("mutsumi.scheduler")
SERVICE_WORKER_IDLE_SECONDS = 60.0


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
    remember_output: bool = True
    allow_visible_output: bool = True
    allow_cold_poke: bool = True
    allow_status_update: bool = True
    allow_write_tools: bool = True
    allowed_tools: set[str] | None = None
    remember_inner: bool = True
    conversation_id: str = ""
    actor_id: str = ""
    actor_name: str = ""
    pipeline_id: str = ""
    inbound_events: list[dict[str, Any]] = field(default_factory=list)
    current_event_ids: list[str] = field(default_factory=list)
    precreated_event_ids: list[str] = field(default_factory=list)
    active_work: dict[str, dict[str, str]] | None = None
    context_snapshot_sequence: int | None = None


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
        self._legacy_aliases: set[str] = set()
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._pending_events: dict[str, list[MessageEvent]] = {}
        self._debounce_timers: dict[str, asyncio.Task[None]] = {}
        self._pipeline_states: dict[str, str] = {}
        self._last_active_key: str | None = None
        self.llm_healthy: bool = True
        self.token_usage: dict = {"input": 0, "output": 0, "cache_hit": 0, "cache_miss": 0}
        self.on_state_change: Callable[[], None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._heartbeat_batches: dict[str, asyncio.Task[None]] = {}
        self._scheduled_tasks: dict[int, asyncio.Task[None]] = {}
        self._episode_timers: dict[str, asyncio.Task[None]] = {}
        self._active_work: dict[str, dict[str, str]] = {}
        self._service_queues: dict[str, asyncio.Queue[tuple[ServiceMessageEvent, str]]] = {}
        self._service_workers: dict[str, asyncio.Task[None]] = {}
        self._service_event_ids: set[str] = set()
        self._task_report_queues: dict[str, asyncio.Queue[tuple[str, str]]] = {}
        self._task_report_workers: dict[str, asyncio.Task[None]] = {}
        self._task_report_retries: dict[str, int] = {}
        self._queued_task_report_runs: set[str] = set()
        self._opencode_runner: OpenCodeRunner | None = (
            OpenCodeRunner(config, store, self._accept_opencode_report)
            if config.opencode.enabled
            else None
        )
        self._shutting_down = False

    def _make_key(self, event: MessageEvent) -> str:
        if getattr(event, "source_kind", "qq") == "service":
            return f"service:{event.user_id}"
        if event.message_type == "group" and event.group_id:
            return f"group:{event.group_id}"
        return f"private:{event.user_id}"

    @staticmethod
    def _actor_id(event: MessageEvent) -> str:
        if getattr(event, "source_kind", "qq") == "service":
            return f"service:{event.user_id}"
        return f"qq:user:{event.user_id}"

    @staticmethod
    def _actor_name(event: MessageEvent) -> str:
        sender = event.sender or {}
        return str(sender.get("card") or sender.get("nickname") or event.user_id)

    @staticmethod
    def _storage_key(event: MessageEvent) -> str:
        if getattr(event, "source_kind", "qq") == "service":
            return f"service:{event.user_id}"
        if event.message_type == "group" and event.group_id:
            # Keep legacy per-actor memory scopes while the window/event stream
            # is shared by the actual group conversation.
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

    def _alias_legacy_group_state(self, key: str, storage_key: str) -> None:
        if key == storage_key or not key.startswith("group:"):
            return
        self._windows[storage_key] = self._windows[key]
        self._sessions[storage_key] = self._sessions[key]
        self._legacy_aliases.add(storage_key)

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

    def _cancel_episode_timer(self, conversation_id: str) -> None:
        task = self._episode_timers.pop(conversation_id, None)
        if task and not task.done():
            task.cancel()

    def _schedule_episode_summary(self, conversation_id: str) -> None:
        self._cancel_episode_timer(conversation_id)
        self._episode_timers[conversation_id] = asyncio.create_task(
            self._wait_and_summarize_episode(conversation_id)
        )

    async def _wait_and_summarize_episode(self, conversation_id: str) -> None:
        try:
            from .memory.episodes import summarize_pending_episode
            await asyncio.sleep(max(1, int(self.config.context.episode_idle_seconds)))
            await summarize_pending_episode(
                self.store,
                self.config,
                conversation_id,
                max_events=max(1, int(self.config.context.episode_max_events)),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[EPISODE] idle summary failed conversation=%s", conversation_id)
        finally:
            self._episode_timers.pop(conversation_id, None)

    async def dispatch(self, event: MessageEvent) -> None:
        key = self._make_key(event)
        self._cancel_episode_timer(key)

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

    async def dispatch_service_event(self, event: ServiceMessageEvent, event_id: str) -> None:
        """Queue a persisted service event without cancelling earlier reports."""
        if self._shutting_down:
            raise RuntimeError("scheduler is shutting down")
        if event_id in self._service_event_ids:
            logger.info("[SCHED] service event already queued event_id=%s", event_id)
            return
        key = self._make_key(event)
        self._ensure_user_state(key)
        queue = self._service_queues.setdefault(key, asyncio.Queue())
        self._service_event_ids.add(event_id)
        try:
            await queue.put((event, event_id))
        except Exception:
            self._service_event_ids.discard(event_id)
            raise
        self._ensure_service_worker(key)
        logger.info("[SCHED] queued service event key=%s event_id=%s depth=%d", key, event_id, queue.qsize())

    def _ensure_service_worker(self, key: str) -> None:
        worker = self._service_workers.get(key)
        if worker is not None and not worker.done():
            return
        worker = asyncio.create_task(self._run_service_queue(key))
        self._service_workers[key] = worker
        self._tasks[key] = worker

    async def _run_service_queue(self, key: str) -> None:
        queue = self._service_queues[key]
        try:
            while True:
                try:
                    event, event_id = await asyncio.wait_for(
                        queue.get(),
                        timeout=SERVICE_WORKER_IDLE_SECONDS,
                    )
                except asyncio.TimeoutError:
                    break
                try:
                    await self._run_service_event(key, event, event_id)
                except asyncio.CancelledError:
                    record = await self.store.get_event(event_id)
                    if record is not None and record.status == "cancelled":
                        await self.store.update_event_status(event_id, "received")
                        logger.info("[SCHED] restored cancelled service event to received event_id=%s", event_id)
                    raise
                except Exception:
                    logger.exception("[SCHED] service pipeline failed key=%s event_id=%s", key, event_id)
                finally:
                    self._service_event_ids.discard(event_id)
                    queue.task_done()
                if queue.empty():
                    self._schedule_episode_summary(key)
        finally:
            if self._service_workers.get(key) is asyncio.current_task():
                self._service_workers.pop(key, None)
            if self._tasks.get(key) is asyncio.current_task():
                self._tasks.pop(key, None)
            if queue.empty():
                self._service_queues.pop(key, None)
            elif not self._shutting_down:
                self._ensure_service_worker(key)

    async def _run_service_event(self, key: str, event: ServiceMessageEvent, event_id: str) -> None:
        from .message.classifier import classify_message
        from .pipeline import pipeline

        classified = classify_message(event.message, event.raw_message)
        self._cancel_episode_timer(key)
        actor_id = self._actor_id(event)
        actor_name = self._actor_name(event)
        peer = self._make_service_peer()
        content = classified.content or event.raw_message
        deps = PipelineDeps(
            config=self.config,
            registry=self.registry,
            sender=self.sender,
            store=self.store,
            window=self._windows[key],
            session=self._sessions[key],
            peer=peer,
            group_key=key,
            active_work=self._active_work,
            token_counter=self.token_usage,
            report_state=self._make_report_state(key),
            report_llm_health=self._make_report_llm_health(),
            source="external_service",
            allow_cold_poke=False,
            conversation_id=key,
            actor_id=actor_id,
            actor_name=actor_name,
            pipeline_id=f"{key}:{time.time_ns()}",
            inbound_events=[{
                "actor_id": actor_id,
                "actor_name": actor_name,
                "content": content,
                "created_at": event.time or time.time(),
            }],
            precreated_event_ids=[event_id],
        )
        logger.info("[SCHED] dispatching service key=%s event_id=%s type=%s", key, event_id, classified.msg_type.value)
        await pipeline(
            message=content,
            msg_type=classified.msg_type,
            image_file=classified.image_file,
            image_url=classified.image_url,
            media_kind=classified.media_kind,
            deps=deps,
        )

    def _make_service_peer(self) -> Peer:
        from .message.sender import Peer
        return Peer(chat_type=1, peer_uid=str(self.config.ingress.target_user_id))

    async def delegate_task(self, *, prompt: str, conversation_id: str) -> str:
        if self._opencode_runner is None:
            raise RuntimeError("OpenCode integration is disabled")
        return await self._opencode_runner.start(
            prompt=prompt,
            conversation_id=conversation_id,
        )

    async def _accept_opencode_report(self, run_id: str, report: str) -> None:
        record = await self.store.get_delegated_task(run_id)
        if record is None:
            logger.warning("[OPENCODE] report for unknown run_id=%s", run_id)
            return
        if record.status == "delivered":
            logger.info("[OPENCODE] report already delivered run_id=%s", run_id)
            return

        # The database row is the durable report channel. A duplicate callback
        # must not replace its content with a second transient file payload.
        durable_report = str(record.report or report).strip()
        if not durable_report:
            logger.warning("[OPENCODE] empty report run_id=%s", run_id)
            return

        from .memory.store import EventRecord, EventType

        if record.report_event_id:
            # The report row is durable even if queue delivery failed after a
            # process exit. Re-enqueue it rather than treating the callback as
            # a duplicate and losing the wake-up.
            existing_event = await self.store.get_event(record.report_event_id)
            if existing_event is None:
                logger.warning(
                    "[OPENCODE] report event missing; rebuilding run_id=%s event_id=%s",
                    run_id,
                    record.report_event_id,
                )
                event = await self.store.append_event(EventRecord(
                    event_id=record.report_event_id,
                    conversation_id=record.conversation_id,
                    actor_id="service:opencode",
                    actor_kind="service",
                    actor_name="OpenCode",
                    event_type=EventType.INBOUND.value,
                    content=durable_report,
                    payload={"run_id": run_id, "source": "opencode"},
                    status="received",
                    visibility="private",
                    audience="bot:self",
                    pipeline_id=f"task-report:{run_id}",
                ))
                existing_event = event
            if existing_event.status == "finalized":
                await self.store.update_delegated_task(run_id, status="delivered")
                return
            self._enqueue_task_report_for_conversation(
                record.conversation_id,
                run_id,
                existing_event.event_id or record.report_event_id,
            )
            return

        try:
            event = await self.store.append_event(EventRecord(
                event_id=f"task-report:{run_id}",
                conversation_id=record.conversation_id,
                actor_id="service:opencode",
                actor_kind="service",
                actor_name="OpenCode",
                event_type=EventType.INBOUND.value,
                content=durable_report,
                payload={"run_id": run_id, "source": "opencode"},
                status="received",
                visibility="private",
                audience="bot:self",
                pipeline_id=f"task-report:{run_id}",
            ))
        except Exception:
            event = await self.store.get_event(f"task-report:{run_id}")
            if event is None:
                raise
        await self.store.update_delegated_task(
            run_id,
            report=durable_report,
            report_event_id=event.event_id or "",
        )
        self._enqueue_task_report_for_conversation(
            record.conversation_id,
            run_id,
            event.event_id or "",
        )

    def _ensure_task_report_worker(self, key: str) -> None:
        worker = self._task_report_workers.get(key)
        if worker is not None and not worker.done():
            return
        self._task_report_workers[key] = asyncio.create_task(
            self._run_task_report_queue(key)
        )

    def _enqueue_task_report_for_conversation(
        self,
        conversation_id: str,
        run_id: str,
        event_id: str,
    ) -> bool:
        """Queue one durable report wake-up at most once per process.

        The event row is the cross-restart idempotency key. This in-memory set
        only prevents duplicate callbacks from creating duplicate queue items
        while the current process is alive; startup reconstructs it from the
        durable delegated-task row.
        """
        run_id = str(run_id or "").strip()
        event_id = str(event_id or "").strip()
        if not run_id or not event_id:
            return False
        if run_id in self._queued_task_report_runs:
            logger.info("[OPENCODE] report already queued run_id=%s", run_id)
            return False
        queue = self._task_report_queues.setdefault(
            str(conversation_id), asyncio.Queue()
        )
        self._queued_task_report_runs.add(run_id)
        queue.put_nowait((run_id, event_id))
        self._ensure_task_report_worker(str(conversation_id))
        logger.info(
            "[OPENCODE] report queued run_id=%s conversation=%s depth=%d",
            run_id,
            conversation_id,
            queue.qsize(),
        )
        return True

    async def _run_task_report_queue(self, key: str) -> None:
        queue = self._task_report_queues[key]
        try:
            while True:
                try:
                    run_id, event_id = await asyncio.wait_for(queue.get(), timeout=60.0)
                except asyncio.TimeoutError:
                    break
                report_task: asyncio.Task[None] | None = None
                delivered = False
                try:
                    await self._wait_for_conversation_slot(key)
                    report_task = asyncio.create_task(
                        self._run_task_report(key, run_id, event_id)
                    )
                    self._tasks[key] = report_task
                    try:
                        await report_task
                        delivered = True
                    except asyncio.CancelledError:
                        if self._shutting_down:
                            raise
                        self._queued_task_report_runs.discard(run_id)
                        self._enqueue_task_report_for_conversation(key, run_id, event_id)
                        logger.info(
                            "[OPENCODE] report pipeline cancelled; requeued run_id=%s",
                            run_id,
                        )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("[OPENCODE] report pipeline failed run_id=%s", run_id)
                    retries = self._task_report_retries.get(run_id, 0) + 1
                    self._task_report_retries[run_id] = retries
                    if retries <= 3 and not self._shutting_down:
                        await asyncio.sleep(min(30.0, float(2 ** retries)))
                        self._queued_task_report_runs.discard(run_id)
                        self._enqueue_task_report_for_conversation(key, run_id, event_id)
                        logger.warning(
                            "[OPENCODE] report requeued run_id=%s retry=%d/3",
                            run_id,
                            retries,
                        )
                    else:
                        # Keep the durable event in ``received``. A later
                        # service restart can retry it; this process must not
                        # pretend that the report was delivered.
                        self._queued_task_report_runs.discard(run_id)
                finally:
                    if report_task is not None and self._tasks.get(key) is report_task:
                        self._tasks.pop(key, None)
                    if delivered:
                        self._queued_task_report_runs.discard(run_id)
                    queue.task_done()
                if queue.empty():
                    break
        finally:
            if self._task_report_workers.get(key) is asyncio.current_task():
                self._task_report_workers.pop(key, None)
            if queue.empty():
                self._task_report_queues.pop(key, None)
            elif not self._shutting_down:
                self._ensure_task_report_worker(key)

    async def _wait_for_conversation_slot(
        self,
        key: str,
        *,
        current_task: asyncio.Task[None] | None = None,
    ) -> None:
        """Wait until user input and its debounce window have priority."""
        while True:
            active = self._tasks.get(key)
            if active is not None and active is not current_task and not active.done():
                try:
                    await asyncio.shield(active)
                except asyncio.CancelledError:
                    if not active.done():
                        raise
                continue

            debounce = self._debounce_timers.get(key)
            if debounce is not None and not debounce.done():
                await asyncio.shield(debounce)
                continue
            if self._pending_events.get(key):
                await asyncio.sleep(0.05)
                continue
            return

    async def _run_task_report(self, key: str, run_id: str, event_id: str) -> None:
        from .message.sender import Peer
        from .memory.store import EventType
        from .pipeline import pipeline
        from .message.classifier import MessageType

        record = await self.store.get_delegated_task(run_id)
        event = await self.store.get_event(event_id)
        if record is None:
            raise RuntimeError(f"delegated task {run_id} no longer exists")
        if event is None:
            if not str(record.report or "").strip():
                raise RuntimeError(f"delegated task {run_id} has no durable report")
            # Rebuild the durable report event and continue this delivery
            # attempt. The run is already marked queued, so asking the queue
            # helper to enqueue it again here would suppress the only retry.
            await self._accept_opencode_report(run_id, record.report)
            record = await self.store.get_delegated_task(run_id)
            if record is None or not record.report_event_id:
                raise RuntimeError(f"delegated task {run_id} report event was not rebuilt")
            event_id = record.report_event_id
            event = await self.store.get_event(event_id)
            if event is None:
                raise RuntimeError(f"delegated task {run_id} report event is missing")
        if event.status == "finalized":
            # A process may have died after the report pipeline finalized its
            # inbound event but before the delegated-task status was updated.
            # Do not deliver the same report twice after restart.
            await self.store.update_delegated_task(run_id, status="delivered")
            self._task_report_retries.pop(run_id, None)
            return
        if event.status in {"cancelled", "error"}:
            # Report delivery is retryable. Ordinary user cancellations remain
            # terminal, but a report that was already accepted must not be
            # lost merely because a newer user input interrupted its pipeline.
            await self.store.update_event_status(event_id, "received")
        attempt_pipeline_id = f"task-report:{run_id}:{uuid.uuid4().hex}"
        if event.pipeline_id != attempt_pipeline_id:
            await self.store.update_event_pipeline_id(event_id, attempt_pipeline_id)
        report = str(record.report or event.content or "").strip()
        if not report:
            raise RuntimeError(f"delegated task {run_id} has an empty report")
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
            source="task_report",
            allow_cold_poke=False,
            conversation_id=key,
            actor_id="service:opencode",
            actor_name="OpenCode",
            pipeline_id=attempt_pipeline_id,
            inbound_events=[{
                "actor_id": "service:opencode",
                "actor_name": "OpenCode",
                "content": report,
                "created_at": event.created_at or time.time(),
            }],
            precreated_event_ids=[event_id],
        )
        pipeline_status = await pipeline(
            message=report,
            msg_type=MessageType.LONG_TEXT if len(report) >= 50 else MessageType.SHORT_TEXT,
            image_file=None,
            image_url=None,
            deps=deps,
        )
        processed_event = await self.store.get_event(event_id)
        if (
            pipeline_status in {"cancelled", "error"}
            or processed_event is None
            or processed_event.status not in {"finalized", "no_reply", "empty"}
        ):
            raise RuntimeError(
                f"report pipeline did not finalize event {event_id} "
                f"status={pipeline_status} event_status={processed_event.status if processed_event else 'missing'}"
            )
        await self.store.update_delegated_task(run_id, status="delivered")
        self._task_report_retries.pop(run_id, None)

    async def _debounce_expire(self, key: str) -> None:
        await asyncio.sleep(self.config.context.debounce_timeout)
        events = self._pending_events.pop(key, [])
        self._debounce_timers.pop(key, None)
        if not events:
            return

        from .message.classifier import classify_message, MessageType

        texts: list[str] = []
        text_events: list[tuple[MessageEvent, str]] = []
        final_type = MessageType.SHORT_TEXT
        final_image_file: str | None = None
        final_image_url: str | None = None
        for ev in events:
            c = classify_message(ev.message, ev.raw_message)
            if c.content:
                text_events.append((ev, c.content))
            if c.msg_type == MessageType.IMAGE:
                final_type = MessageType.IMAGE
                final_image_file = c.image_file or final_image_file
                final_image_url = c.image_url or final_image_url

        actor_ids = {self._actor_id(ev) for ev, _ in text_events}
        multiple_group_actors = key.startswith("group:") and len(actor_ids) > 1
        inbound_events = [
            {
                "actor_id": self._actor_id(ev),
                "actor_name": self._actor_name(ev),
                "content": content,
                "created_at": ev.time or time.time(),
            }
            for ev, content in text_events
        ]
        for ev, content in text_events:
            if multiple_group_actors:
                texts.append(f"Speaker {self._actor_name(ev)} ({self._actor_id(ev)}):\n{content}")
            else:
                texts.append(content)
        merged_message = "\n".join(texts)
        if len(merged_message) >= 50:
            final_type = MessageType.LONG_TEXT

        PEER = self._make_peer(events[0])

        await self.cancel_user(key)
        self._ensure_user_state(key)
        storage_key = self._storage_key(events[0])
        self._alias_legacy_group_state(key, storage_key)

        from .pipeline import pipeline

        actor_id = "qq:group:multiple" if multiple_group_actors else self._actor_id(events[0])
        actor_name = "Multiple group members" if multiple_group_actors else self._actor_name(events[0])
        deps = PipelineDeps(
            config=self.config,
            registry=self.registry,
            sender=self.sender,
            store=self.store,
            window=self._windows[key],
            session=self._sessions[key],
            peer=PEER,
            group_key=self._storage_key(events[0]),
            conversation_id=key,
            actor_id=actor_id,
            actor_name=actor_name,
            pipeline_id=f"{key}:{time.time_ns()}",
            inbound_events=inbound_events,
            active_work=self._active_work,
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
                    media_kind="image",
                    deps=deps,
                )
            except asyncio.CancelledError:
                self.set_pipeline_state(key, "CANCELLED")
                raise
            except Exception:
                logger.exception("[SCHED] unhandled error in pipeline for %s", key)
            finally:
                self.clear_pipeline_state(key)
                self._schedule_episode_summary(key)

        task = asyncio.create_task(_run())
        self._tasks[key] = task

    async def _dispatch_direct(self, key: str, event: MessageEvent, classified) -> None:
        PEER = self._make_peer(event)
        await self.cancel_user(key)
        self._ensure_user_state(key)
        storage_key = self._storage_key(event)
        self._alias_legacy_group_state(key, storage_key)

        from .pipeline import pipeline

        deps = PipelineDeps(
            config=self.config,
            registry=self.registry,
            sender=self.sender,
            store=self.store,
            window=self._windows[key],
            session=self._sessions[key],
            peer=PEER,
            group_key=self._storage_key(event),
            conversation_id=key,
            actor_id=self._actor_id(event),
            actor_name=self._actor_name(event),
            pipeline_id=f"{key}:{time.time_ns()}",
            active_work=self._active_work,
            token_counter=self.token_usage,
            report_state=self._make_report_state(key),
            report_llm_health=self._make_report_llm_health(),
        )

        logger.info("[SCHED] dispatching direct key=%s type=%s", key, classified.msg_type.value)

        async def _run():
            try:
                await pipeline(
                    message=(
                        classified.content
                        or ("[Image received]" if classified.msg_type.value == "image" else event.raw_message)
                    ),
                    msg_type=classified.msg_type,
                    image_file=classified.image_file,
                    image_url=classified.image_url,
                    media_kind=classified.media_kind,
                    deps=deps,
                )
            except asyncio.CancelledError:
                self.set_pipeline_state(key, "CANCELLED")
                raise
            except Exception:
                logger.exception("[SCHED] unhandled error in pipeline for %s", key)
            finally:
                self.clear_pipeline_state(key)
                self._schedule_episode_summary(key)

        task = asyncio.create_task(_run())
        self._tasks[key] = task

    async def cancel_user(self, key: str) -> None:
        self._cleanup_debounce(key)
        heartbeat_tasks = list(self._heartbeat_batches.values())
        for heartbeat_task in heartbeat_tasks:
            if not heartbeat_task.done():
                heartbeat_task.cancel()
        if heartbeat_tasks:
            await asyncio.gather(*heartbeat_tasks, return_exceptions=True)
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
        orphaned_pipelines = await self.store.cancel_orphaned_pipelines()
        if orphaned_pipelines:
            logger.warning(
                "[STARTUP] closed %d orphaned non-service pipelines as cancelled",
                orphaned_pipelines,
            )
        ledger_conversations = set(await self.store.get_event_conversation_ids())
        group_keys = await self.store.get_message_group_keys()
        restored: dict[str, list[dict]] = {}
        scopes: dict[str, list[str]] = {}
        legacy_boundaries: dict[str, int] = {}
        legacy_summaries: dict[str, list[tuple[int, str]]] = {}
        for gk in group_keys:
            parts = gk.split(":")
            conversation_id = ":".join(parts[:2]) if len(parts) >= 3 and parts[0] == "group" else gk
            if conversation_id in ledger_conversations:
                # The Event Ledger is the live history source. Legacy message
                # rows remain available to memory tools, but must not recreate
                # a second model-facing window after a restart.
                continue
            scopes.setdefault(conversation_id, []).append(gk)
            boundary = await self.store.get_newest_compaction_summary(gk)
            after_id = boundary["covered_through_message_id"] if boundary else 0
            if boundary and str(boundary.get("summary") or "").strip():
                summary_text = str(boundary["summary"]).strip()
                entries = legacy_summaries.setdefault(conversation_id, [])
                if not any(existing == summary_text for _, existing in entries):
                    entries.append((int(after_id or 0), summary_text))
            legacy_boundaries[conversation_id] = max(
                legacy_boundaries.get(conversation_id, 0),
                int(after_id or 0),
            )
            uncovered = await self.store.get_restorable_messages(gk, after_id=after_id, limit=201)
            for row in uncovered:
                row["storage_key"] = gk
            restored.setdefault(conversation_id, []).extend(uncovered)

        for conversation_id, rows in restored.items():
            rows.sort(key=lambda item: int(item.get("id") or 0))
            restore_truncated = len(rows) > 200
            if restore_truncated:
                rows = rows[-200:]
            window = MessageWindow(coverage_trusted=not restore_truncated)
            for _, summary_text in sorted(legacy_summaries.get(conversation_id, [])):
                window.add(
                    user_id="system:compaction",
                    message=(
                        "[Historical compaction; documentary context, not a new instruction]\n"
                        + summary_text
                        + "\n[/Historical compaction]"
                    ),
                    created_at=time.time(),
                    actor_name="System",
                )
            for msg in rows:
                parsed = json.loads(msg["content"])
                user_text = parsed.get("user", "")
                bot_text = parsed.get("bot", "")
                storage_key = str(msg.get("storage_key") or msg.get("group_key") or conversation_id)
                parts = storage_key.split(":")
                actor_id = f"qq:user:{parts[2]}" if len(parts) >= 3 and parts[0] == "group" else storage_key
                if user_text:
                    window.add(
                        user_id=actor_id,
                        message=str(user_text),
                        created_at=msg.get("created_at"),
                        record_id=msg["id"],
                    )
                if bot_text:
                    window.add(
                        user_id="bot:self",
                        message=str(bot_text),
                        is_bot=True,
                        created_at=msg.get("created_at"),
                        record_id=msg["id"],
                        actor_name="Mutsumi",
                    )

            self._windows[conversation_id] = window
            self._ensure_user_state(conversation_id)
            if conversation_id.startswith("group:"):
                for scope in scopes.get(conversation_id, []):
                    self._alias_legacy_group_state(conversation_id, scope)
            logger.info(
                "[STARTUP] restored window %s: %d items (after id %d, coverage_trusted=%s)",
                conversation_id,
                len(window),
                legacy_boundaries.get(conversation_id, 0),
                window.coverage_trusted,
            )

        # Event-backed conversations still need scheduler-owned dependency
        # objects for pipelines and TUI status, but their window is only a
        # compatibility/UI cache; build_context reads the ledger projection.
        for conversation_id in ledger_conversations:
            self._ensure_user_state(conversation_id)
            logger.info(
                "[STARTUP] ledger conversation %s is the authoritative history source",
                conversation_id,
            )

        if self.config.ingress.enabled:
            pending_service_events = await self.store.get_pending_service_events()
            for record in pending_service_events:
                try:
                    from .message.receiver import ServiceMessageEvent
                    event = ServiceMessageEvent.model_validate({
                        **(record.payload or {}),
                        "source_kind": "service",
                    })
                    await self.dispatch_service_event(event, str(record.event_id))
                except Exception:
                    logger.exception("[STARTUP] failed to restore service event=%s", record.event_id)
                    if record.event_id:
                        await self.store.update_event_status(record.event_id, "error")
            if pending_service_events:
                logger.info("[STARTUP] restored %d pending service events", len(pending_service_events))

        if self.config.heartbeat.enabled and self._heartbeat_task is None:
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        pending_scheduled_tasks = await self.store.get_pending_scheduled_tasks()
        for record in pending_scheduled_tasks:
            if record.status == "running":
                # A previous process may have died after claiming the task.
                # Reopen it as pending before creating exactly one local timer.
                await self.store.update_scheduled_task(record.id, status="pending")
                record = replace(record, status="pending")
            self._start_scheduled_task(record)
        if pending_scheduled_tasks:
            logger.info("[SCHEDULE] restored %d pending tasks", len(pending_scheduled_tasks))

        delegated_tasks = await self.store.get_recoverable_delegated_tasks()
        for record in delegated_tasks:
            if record.status != "reported" or not record.report:
                if record.status in {"pending", "running"}:
                    await self.store.update_delegated_task(
                        record.run_id,
                        status="unreported",
                        finished_at=time.time(),
                    )
                logger.warning(
                    "[OPENCODE] run=%s was not reported before restart; status=%s",
                    record.run_id,
                    record.status,
                )
                continue
            if record.report_event_id:
                self._enqueue_task_report_for_conversation(
                    record.conversation_id,
                    record.run_id,
                    record.report_event_id,
                )
            else:
                await self._accept_opencode_report(record.run_id, record.report)

    async def _heartbeat_loop(self) -> None:
        private_interval = max(60, int(self.config.heartbeat.private_interval_seconds))
        group_interval = max(60, int(self.config.heartbeat.group_interval_seconds))
        next_private = time.monotonic() + private_interval
        next_group = time.monotonic() + group_interval
        logger.info(
            "[HEARTBEAT] enabled private_interval=%ss group_interval=%ss active_window=%ss",
            private_interval,
            group_interval,
            self.config.heartbeat.active_window_seconds,
        )
        try:
            while True:
                delay = max(0.1, min(next_private, next_group) - time.monotonic())
                await asyncio.sleep(delay)
                now = time.monotonic()
                if now >= next_private:
                    await self._start_heartbeat_batch("private")
                    next_private = time.monotonic() + private_interval
                if now >= next_group:
                    await self._start_heartbeat_batch("group")
                    next_group = time.monotonic() + group_interval
        except asyncio.CancelledError:
            logger.info("[HEARTBEAT] stopped")
            raise

    async def run_heartbeat_once(self, *, scope: str = "private") -> None:
        from .message.classifier import MessageType
        from .pipeline import pipeline
        if any(task and not task.done() for task in self._tasks.values()):
            logger.info("[HEARTBEAT] skipped scope=%s because a user pipeline is active", scope)
            return
        since = time.time() - max(60, int(self.config.heartbeat.active_window_seconds))
        candidates = await self.store.get_recent_active_conversations(since=since, chat_type=scope)
        logger.info("[HEARTBEAT] scope=%s candidates=%d", scope, len(candidates))
        for candidate in candidates:
            key = str(candidate["conversation_id"])
            if any(task and not task.done() for task in self._tasks.values()):
                logger.info("[HEARTBEAT] yielding scope=%s at key=%s", scope, key)
                break
            await self._run_heartbeat_for_conversation(key, candidate, pipeline, MessageType)

    async def _start_heartbeat_batch(self, scope: str) -> None:
        task = asyncio.create_task(self.run_heartbeat_once(scope=scope))
        self._heartbeat_batches[scope] = task
        try:
            await task
        except asyncio.CancelledError:
            logger.info("[HEARTBEAT] batch cancelled scope=%s", scope)
        finally:
            if self._heartbeat_batches.get(scope) is task:
                self._heartbeat_batches.pop(scope, None)

    async def _run_heartbeat_for_conversation(
        self,
        key: str,
        candidate: dict,
        pipeline,
        message_type,
    ) -> None:
        self._ensure_user_state(key)
        peer = self._peer_from_key(key)
        storage_key = key
        if key.startswith("group:") and str(candidate.get("actor_id", "")).startswith("qq:user:"):
            storage_key = f"{key}:{str(candidate['actor_id']).rsplit(':', 1)[-1]}"
        waiting_for_reply = await self.store.has_unanswered_proactive(key)
        allowed_tools = {"sticker_search", "media_search", "send"}
        deps = PipelineDeps(
            config=self.config,
            registry=self.registry,
            sender=self.sender,
            store=self.store,
            window=self._windows[key],
            session=self._sessions[key],
            peer=peer,
            group_key=storage_key,
            conversation_id=key,
            actor_id=str(candidate.get("actor_id") or ""),
            actor_name=str(candidate.get("actor_name") or ""),
            pipeline_id=f"heartbeat:{key}:{time.time_ns()}",
            active_work=self._active_work,
            token_counter=self.token_usage,
            report_state=self._make_report_state(key),
            report_llm_health=self._make_report_llm_health(),
            source="heartbeat",
            silent=False,
            remember_input=False,
            remember_output=True,
            allow_visible_output=not waiting_for_reply,
            allow_cold_poke=False,
            allow_status_update=False,
            allow_write_tools=False,
            allowed_tools=allowed_tools,
        )
        prompt = self.config.prompts.system.heartbeat
        logger.info("[HEARTBEAT] triggering scope=%s key=%s actor=%s", key.split(":", 1)[0], key, deps.actor_id)
        await pipeline(
            message=prompt,
            msg_type=message_type.SHORT_TEXT,
            image_file=None,
            image_url=None,
            deps=deps,
        )

    async def schedule_task(
        self,
        *,
        scheduled_at: float,
        prompt: str,
        context: str = "",
        group_key: str,
        peer: Peer,
        interval_seconds: float | None = None,
        cron_expression: str = "",
    ) -> int:
        prompt = str(prompt or "").strip()
        if not prompt:
            raise ValueError("prompt is required")
        context = str(context or "").strip()
        if not math.isfinite(float(scheduled_at)):
            raise ValueError("scheduled_at must be finite")
        cron_expression = str(cron_expression or "").strip()
        if interval_seconds is not None and cron_expression:
            raise ValueError("cron_expression and interval_seconds cannot be combined")
        if interval_seconds is not None and interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        if interval_seconds is not None and not math.isfinite(float(interval_seconds)):
            raise ValueError("interval_seconds must be finite")
        if cron_expression:
            from .tools.scheduler import parse_cron_expression

            parse_cron_expression(cron_expression)
        schedule_kind = (
            "cron"
            if cron_expression
            else ("interval" if interval_seconds is not None else "once")
        )
        task_id = await self.store.add_scheduled_task(
            group_key=group_key,
            peer_chat_type=peer.chat_type,
            peer_uid=peer.peer_uid,
            prompt=prompt,
            context=context,
            scheduled_at=scheduled_at,
            schedule_kind=schedule_kind,
            interval_seconds=interval_seconds,
            cron_expression=cron_expression,
        )
        from .memory.store import ScheduledTaskRecord

        record = ScheduledTaskRecord(
            id=task_id,
            group_key=group_key,
            peer_chat_type=peer.chat_type,
            peer_uid=peer.peer_uid,
            prompt=prompt,
            context=context,
            scheduled_at=scheduled_at,
            status="pending",
            created_at=time.time(),
            schedule_kind=schedule_kind,
            interval_seconds=interval_seconds,
            cron_expression=cron_expression,
        )
        self._start_scheduled_task(record)
        logger.info(
            "[SCHEDULE] registered task_id=%s key=%s kind=%s trigger_at=%s interval=%s cron=%s",
            task_id,
            group_key,
            schedule_kind,
            scheduled_at,
            interval_seconds,
            cron_expression or "-",
        )
        return task_id

    @staticmethod
    def _next_recurring_time(record: ScheduledTaskRecord) -> float | None:
        """Return the next wall-clock trigger for a recurring task."""
        now = time.time()
        if record.schedule_kind == "interval" and record.interval_seconds:
            return now + max(1.0, float(record.interval_seconds))
        if record.schedule_kind == "cron" and record.cron_expression:
            from .tools.scheduler import next_cron_time

            utc_plus_8 = timezone(timedelta(hours=8))
            next_at = next_cron_time(
                record.cron_expression,
                datetime.fromtimestamp(now, utc_plus_8),
            )
            return next_at.timestamp()
        return None

    async def schedule_once(
        self,
        *,
        scheduled_at: float,
        prompt: str,
        group_key: str,
        peer: Peer,
        context: str = "",
    ) -> int:
        """Backward-compatible wrapper for the original one-shot API."""
        return await self.schedule_task(
            scheduled_at=scheduled_at,
            prompt=prompt,
            context=context,
            group_key=group_key,
            peer=peer,
        )

    async def list_scheduled_tasks(
        self,
        *,
        group_key: str | None = None,
    ) -> list[ScheduledTaskRecord]:
        return await self.store.list_scheduled_tasks(group_key=group_key)

    async def update_scheduled_task_lifecycle(
        self,
        task_id: int,
        operation: str,
        *,
        group_key: str | None = None,
    ) -> str:
        record = await self.store.get_scheduled_task(task_id)
        if record is None:
            return f"[Error: scheduled task {task_id} was not found]"
        if group_key and record.group_key != group_key:
            return f"[Error: scheduled task {task_id} is not bound to this conversation]"
        operation = operation.lower()
        if operation == "pause":
            if record.status in {"done", "cancelled"}:
                return f"[Error: task {task_id} is already {record.status}]"
            await self.store.mark_scheduled_task_status(task_id, "paused")
            task = self._scheduled_tasks.pop(task_id, None)
            if task is not None and not task.done():
                task.cancel()
            return f"[OK] scheduled task {task_id} paused"
        if operation == "resume":
            if record.status != "paused":
                return f"[Error: task {task_id} is not paused]"
            await self.store.mark_scheduled_task_status(task_id, "pending")
            resumed = replace(record, status="pending")
            self._start_scheduled_task(resumed)
            return f"[OK] scheduled task {task_id} resumed"
        if operation == "cancel":
            if record.status == "cancelled":
                return f"[OK] scheduled task {task_id} already cancelled"
            if record.status == "done":
                return f"[Error: task {task_id} is already done]"
            await self.store.mark_scheduled_task_status(task_id, "cancelled")
            task = self._scheduled_tasks.pop(task_id, None)
            if task is not None and not task.done():
                task.cancel()
            return f"[OK] scheduled task {task_id} cancelled"
        return f"[Error: unsupported scheduled task operation: {operation}]"

    def _start_scheduled_task(self, record: ScheduledTaskRecord) -> None:
        existing = self._scheduled_tasks.pop(record.id, None)
        if existing and not existing.done():
            existing.cancel()
        self._scheduled_tasks[record.id] = asyncio.create_task(self._scheduled_sleep_and_fire(record))

    async def _scheduled_sleep_and_fire(self, record: ScheduledTaskRecord) -> None:
        current_task = asyncio.current_task()
        requeue: ScheduledTaskRecord | None = None
        try:
            current = record
            while True:
                delay = max(0.0, current.scheduled_at - time.time())
                if delay:
                    await asyncio.sleep(delay)
                fired = await self._fire_scheduled_task(current)
                if not fired:
                    # A pipeline adapter may convert cancellation into a
                    # normal terminal result. Preserve a deferred task just
                    # as the CancelledError path does.
                    latest = await self.store.get_scheduled_task(current.id)
                    if (
                        latest is not None
                        and latest.status == "pending"
                        and not self._shutting_down
                    ):
                        requeue = latest
                    break
                next_at = self._next_recurring_time(current)
                if next_at is None:
                    break
                # One missed interval produces one run, then the next fire is
                # scheduled from now. Cron likewise skips missed wall-clock
                # slots instead of replaying a burst after restart.
                last_run_at = time.time()
                transitioned = await self.store.reschedule_running_task(
                    current.id,
                    scheduled_at=next_at,
                    last_run_at=last_run_at,
                )
                if not transitioned:
                    logger.info(
                        "[SCHEDULE] task_id=%s was changed during execution; stopping timer",
                        current.id,
                    )
                    break
                current = replace(
                    current,
                    status="pending",
                    scheduled_at=next_at,
                    last_run_at=last_run_at,
                )
        except asyncio.CancelledError:
            # User input may preempt a due scheduled pipeline. Preserve the
            # task for a later attempt; explicit pause/cancel and shutdown are
            # distinguished by the database state and scheduler flag.
            if not self._shutting_down and self._scheduled_tasks.get(record.id) is current_task:
                latest = await self.store.get_scheduled_task(record.id)
                if latest is not None and latest.status in {"pending", "running"}:
                    next_at = time.time() + 1.0
                    transitioned = await self.store.defer_scheduled_task(
                        record.id,
                        scheduled_at=next_at,
                    )
                    if transitioned:
                        requeue = replace(latest, status="pending", scheduled_at=next_at)
            raise
        except Exception:
            logger.exception("[SCHEDULE] task_id=%s failed", record.id)
            next_at = self._next_recurring_time(record)
            if next_at is not None:
                await self.store.reschedule_running_task(
                    record.id,
                    scheduled_at=next_at,
                    last_run_at=time.time(),
                )
            else:
                await self.store.fail_running_task(record.id)
        finally:
            if self._scheduled_tasks.get(record.id) is current_task:
                self._scheduled_tasks.pop(record.id, None)
            if requeue is not None and not self._shutting_down:
                self._start_scheduled_task(requeue)

    async def _fire_scheduled_task(self, record: ScheduledTaskRecord) -> bool:
        from .message.classifier import MessageType
        from .message.sender import Peer
        from .pipeline import pipeline

        current_task = asyncio.current_task()
        if not await self.store.claim_scheduled_task(record.id):
            logger.info("[SCHEDULE] task_id=%s was no longer pending", record.id)
            return False
        logger.info("[SCHEDULE] firing task_id=%s key=%s", record.id, record.group_key)

        # Do not put the scheduled task into the conversation slot while it is
        # waiting. The slot may contain a real user pipeline; overwriting it
        # here would make _wait_for_conversation_slot observe itself and allow
        # two pipelines to mutate one working window concurrently.
        await self._wait_for_conversation_slot(
            record.group_key,
            current_task=current_task,
        )

        # The task can be paused or cancelled while it waits behind a user
        # pipeline. Recheck the durable claim before entering the slot; the
        # in-memory timer cancellation alone is not an atomic lifecycle gate.
        latest = await self.store.get_scheduled_task(record.id)
        if latest is None or latest.status != "running":
            logger.info(
                "[SCHEDULE] task_id=%s stopped while waiting status=%s",
                record.id,
                latest.status if latest is not None else "missing",
            )
            return False

        # The event loop is single-threaded, so this assignment is atomic with
        # respect to dispatches until the next await. A new user input after
        # this point will cancel and defer the scheduled run through the normal
        # conversation-slot path.
        if current_task is not None:
            active = self._tasks.get(record.group_key)
            if active is not None and active is not current_task and not active.done():
                await self._wait_for_conversation_slot(
                    record.group_key,
                    current_task=current_task,
                )
            self._tasks[record.group_key] = current_task

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
            remember_input=False,
            allow_cold_poke=False,
            conversation_id=record.group_key,
            actor_id=f"scheduler:task:{record.id}",
            actor_name="Scheduler",
            pipeline_id=f"schedule:{record.id}:{time.time_ns()}",
        )
        scheduled_message = f"[Scheduled task {record.id}] {record.prompt}"
        if record.context:
            scheduled_message += f"\n[Additional task context]\n{record.context}"
        try:
            pipeline_status = await pipeline(
                message=scheduled_message,
                msg_type=MessageType.SHORT_TEXT,
                image_file=None,
                image_url=None,
                deps=deps,
            )
        finally:
            if current_task is not None and self._tasks.get(record.group_key) is current_task:
                self._tasks.pop(record.group_key, None)
        if pipeline_status in {"error", "cancelled"}:
            logger.warning(
                "[SCHEDULE] task_id=%s pipeline_status=%s",
                record.id,
                pipeline_status,
            )
            if pipeline_status == "cancelled":
                deferred = await self.store.defer_scheduled_task(
                    record.id,
                    scheduled_at=time.time() + 1.0,
                )
                return not deferred
            if record.schedule_kind not in {"interval", "cron"}:
                await self.store.fail_running_task(record.id)
                return False
        if record.schedule_kind not in {"interval", "cron"}:
            return await self.store.complete_running_task(record.id)
        return True

    async def shutdown(self) -> None:
        logger.info("[SHUTDOWN] stopping scheduler with %d windows", len(self._windows))
        self._shutting_down = True

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

        for task in list(self._task_report_workers.values()):
            task.cancel()
        for task in list(self._task_report_workers.values()):
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._task_report_workers.clear()
        self._task_report_queues.clear()
        self._queued_task_report_runs.clear()

        if self._opencode_runner is not None:
            await self._opencode_runner.shutdown()

        for task in list(self._episode_timers.values()):
            task.cancel()
        for task in list(self._episode_timers.values()):
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._episode_timers.clear()

        for key in list(self._keys()):
            self._cleanup_debounce(key)
            await self.cancel_user(key)

        self._service_queues.clear()
        self._service_workers.clear()
        self._service_event_ids.clear()

        await self.store.close()
        logger.info("[SHUTDOWN] complete")

    def _keys(self) -> list[str]:
        return [key for key in self._windows if key not in self._legacy_aliases]
