from __future__ import annotations

import asyncio

import pytest

from src.mutsumi_sync.config import Config
from src.mutsumi_sync.memory.store import EventRecord, EventType, MessageStore
from src.mutsumi_sync.message.receiver import ServiceMessageEvent
from src.mutsumi_sync.message.sender import Peer
from src.mutsumi_sync.scheduler import PipelineScheduler
from src.mutsumi_sync.tools.registry import ToolRegistry
import src.mutsumi_sync.pipeline as pipeline_module
import src.mutsumi_sync.scheduler as scheduler_module


class FakeSender:
    async def send(self, peer: Peer, message: str | list[dict]) -> dict:
        return {"status": "ok"}

    async def send_poke(self, peer: Peer) -> dict:
        return {"status": "ok"}


def _event(message_id: str, text: str) -> ServiceMessageEvent:
    return ServiceMessageEvent(
        post_type="message",
        message_type="private",
        user_id="calendar",
        message_id=message_id,
        message=[{"type": "text", "data": {"text": text}}],
        raw_message=text,
        sender={"nickname": "日程服务"},
    )


@pytest.mark.asyncio
async def test_service_events_are_fifo_and_reply_to_configured_owner(monkeypatch):
    config = Config()
    config.ingress.enabled = True
    config.ingress.target_user_id = "3535616589"
    config.context.episode_idle_seconds = 999999
    store = MessageStore(db_path=":memory:")
    await store.initialize()
    scheduler = PipelineScheduler(config, ToolRegistry(), FakeSender(), store)
    processed: list[tuple[str, str, str, str]] = []

    async def fake_pipeline(message, msg_type, image_file, image_url, *, media_kind="image", deps):
        processed.append((message, deps.source, deps.actor_id, deps.peer.peer_uid))
        await deps.store.update_event_status(deps.precreated_event_ids[0], "finalized")

    monkeypatch.setattr(pipeline_module, "pipeline", fake_pipeline)
    first = _event("calendar-1", "第一条")
    second = _event("calendar-2", "第二条")
    for event, event_id in ((first, "event-1"), (second, "event-2")):
        await store.append_event(EventRecord(
            event_id=event_id,
            conversation_id="service:calendar",
            actor_id="service:calendar",
            actor_kind="service",
            actor_name="日程服务",
            event_type=EventType.INBOUND.value,
            content=event.raw_message,
            payload=event.model_dump(),
            status="received",
        ))

    try:
        await scheduler.dispatch_service_event(first, "event-1")
        await scheduler._service_queues["service:calendar"].join()
        first_worker = scheduler._service_workers["service:calendar"]
        assert not first_worker.done()

        await scheduler.dispatch_service_event(second, "event-2")
        await scheduler._service_queues["service:calendar"].join()
        assert scheduler._service_workers["service:calendar"] is first_worker
        assert processed == [
            ("第一条", "external_service", "service:calendar", "3535616589"),
            ("第二条", "external_service", "service:calendar", "3535616589"),
        ]
        assert all(
            event.status == "finalized"
            for event in await store.get_events(conversation_id="service:calendar", finalized_only=False)
        )
    finally:
        scheduler._cancel_episode_timer("service:calendar")
        await scheduler.shutdown()


@pytest.mark.asyncio
async def test_cancelled_service_worker_restores_inflight_event_for_restart(monkeypatch):
    config = Config()
    config.ingress.enabled = True
    config.ingress.target_user_id = "3535616589"
    config.context.episode_idle_seconds = 999999
    store = MessageStore(db_path=":memory:")
    await store.initialize()
    scheduler = PipelineScheduler(config, ToolRegistry(), FakeSender(), store)
    event = _event("calendar-cancel", "处理中")
    await store.append_event(EventRecord(
        event_id="event-cancel",
        conversation_id="service:calendar",
        actor_id="service:calendar",
        actor_kind="service",
        actor_name="日程服务",
        event_type=EventType.INBOUND.value,
        content=event.raw_message,
        payload=event.model_dump(),
        status="received",
    ))
    started = asyncio.Event()

    async def fake_pipeline(message, msg_type, image_file, image_url, *, media_kind="image", deps):
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            await deps.store.update_event_status(deps.precreated_event_ids[0], "cancelled")
            raise

    monkeypatch.setattr(pipeline_module, "pipeline", fake_pipeline)
    try:
        await scheduler.dispatch_service_event(event, "event-cancel")
        await started.wait()
        worker = scheduler._service_workers["service:calendar"]
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker
        restored = await store.get_event("event-cancel")
        assert restored is not None
        assert restored.status == "received"
    finally:
        scheduler._cancel_episode_timer("service:calendar")
        await scheduler.shutdown()


@pytest.mark.asyncio
async def test_idle_service_worker_retires_without_leaking_task(monkeypatch):
    config = Config()
    config.ingress.target_user_id = "3535616589"
    config.context.episode_idle_seconds = 999999
    store = MessageStore(db_path=":memory:")
    await store.initialize()
    scheduler = PipelineScheduler(config, ToolRegistry(), FakeSender(), store)
    event = _event("calendar-idle", "一次性消息")
    await store.append_event(EventRecord(
        event_id="event-idle",
        conversation_id="service:calendar",
        actor_id="service:calendar",
        actor_kind="service",
        event_type=EventType.INBOUND.value,
        content=event.raw_message,
        payload=event.model_dump(),
        status="received",
    ))

    async def fake_pipeline(message, msg_type, image_file, image_url, *, media_kind="image", deps):
        await deps.store.update_event_status(deps.precreated_event_ids[0], "finalized")

    monkeypatch.setattr(pipeline_module, "pipeline", fake_pipeline)
    monkeypatch.setattr(scheduler_module, "SERVICE_WORKER_IDLE_SECONDS", 0.01)
    try:
        await scheduler.dispatch_service_event(event, "event-idle")
        worker = scheduler._service_workers["service:calendar"]
        await scheduler._service_queues["service:calendar"].join()
        await worker
        assert "service:calendar" not in scheduler._service_workers
        assert "service:calendar" not in scheduler._service_queues
        assert "service:calendar" not in scheduler._tasks
    finally:
        scheduler._cancel_episode_timer("service:calendar")
        await scheduler.shutdown()


@pytest.mark.asyncio
async def test_startup_restores_received_service_events(monkeypatch):
    config = Config()
    config.heartbeat.enabled = False
    config.ingress.enabled = True
    config.ingress.target_user_id = "3535616589"
    config.context.episode_idle_seconds = 999999
    store = MessageStore(db_path=":memory:")
    await store.initialize()
    await store.append_event(EventRecord(
        event_id="pending-service-event",
        conversation_id="service:calendar",
        actor_id="service:calendar",
        actor_kind="service",
        actor_name="日程服务",
        event_type=EventType.INBOUND.value,
        content="重启前未处理",
        payload=_event("calendar-restart", "重启前未处理").model_dump(),
        status="received",
    ))
    scheduler = PipelineScheduler(config, ToolRegistry(), FakeSender(), store)
    processed: list[str] = []

    async def fake_pipeline(message, msg_type, image_file, image_url, *, media_kind="image", deps):
        processed.append(message)
        await deps.store.update_event_status(deps.precreated_event_ids[0], "finalized")

    monkeypatch.setattr(pipeline_module, "pipeline", fake_pipeline)
    try:
        await scheduler.startup()
        await scheduler._service_queues["service:calendar"].join()
        assert processed == ["重启前未处理"]
        restored = await store.get_event("pending-service-event")
        assert restored is not None
        assert restored.status == "finalized"
    finally:
        scheduler._cancel_episode_timer("service:calendar")
        await scheduler.shutdown()
