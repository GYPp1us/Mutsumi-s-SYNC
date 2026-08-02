from __future__ import annotations

import asyncio
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pytest

from src.mutsumi_sync.config import Config
from src.mutsumi_sync.memory.store import MessageStore, ScheduledTaskRecord
from src.mutsumi_sync.memory.store import EventRecord, EventType
from src.mutsumi_sync.message.sender import Peer
from src.mutsumi_sync.scheduler import PipelineScheduler
from src.mutsumi_sync.tools.scheduler import next_cron_time, parse_cron_expression
from src.mutsumi_sync.tools.registry import ToolRegistry
import src.mutsumi_sync.pipeline as pipeline_module


class Sender:
    async def send(self, peer, message):
        return {"status": "ok"}

    async def send_poke(self, peer):
        return {"status": "ok"}


def test_cron_parser_and_next_time_use_utc_plus_8() -> None:
    spec = parse_cron_expression("0 9 * * 1-5")
    assert 0 in spec.minutes
    assert 9 in spec.hours
    assert 1 in spec.days_of_week
    assert 5 in spec.days_of_week

    after = datetime(2026, 8, 3, 9, 1, tzinfo=timezone(timedelta(hours=8)))
    assert next_cron_time("0 9 * * 1-5", after).isoformat() == "2026-08-04T09:00:00+08:00"


def test_cron_parser_maps_seven_to_sunday() -> None:
    spec = parse_cron_expression("0 0 * * 7")
    assert spec.days_of_week == frozenset({0})


def test_cron_parser_treats_stepped_wildcards_as_wildcards() -> None:
    spec = parse_cron_expression("*/5 * 1 * */2")
    assert spec.day_of_month_wildcard is False
    assert spec.day_of_week_wildcard is True


def test_cron_uses_or_when_both_day_fields_are_restricted() -> None:
    after = datetime(2026, 8, 3, 8, 0, tzinfo=timezone(timedelta(hours=8)))
    assert next_cron_time("0 9 1 * 1", after).isoformat() == "2026-08-03T09:00:00+08:00"


async def test_interval_task_persists_lifecycle_and_next_run(tmp_path) -> None:
    config = Config()
    config.heartbeat.enabled = False
    store = MessageStore(str(tmp_path / "schedule.db"), str(tmp_path / "media"))
    await store.initialize()
    scheduler = PipelineScheduler(config, ToolRegistry(), Sender(), store)
    try:
        task_id = await scheduler.schedule_task(
            scheduled_at=time.time() + 3600,
            prompt="periodic work",
            group_key="private:alice",
            peer=Peer(chat_type=1, peer_uid="alice"),
            interval_seconds=900,
        )
        record = await store.get_scheduled_task(task_id)
        assert record is not None
        assert record.schedule_kind == "interval"
        assert record.interval_seconds == 900

        assert "paused" in await scheduler.update_scheduled_task_lifecycle(task_id, "pause")
        assert "resumed" in await scheduler.update_scheduled_task_lifecycle(task_id, "resume")
        assert "cancelled" in await scheduler.update_scheduled_task_lifecycle(task_id, "cancel")
        assert (await store.get_scheduled_task(task_id)).status == "cancelled"
    finally:
        await scheduler.shutdown()


async def test_scheduled_context_is_persisted_and_only_injected_for_the_trigger(
    monkeypatch,
    tmp_path,
) -> None:
    config = Config()
    config.heartbeat.enabled = False
    store = MessageStore(str(tmp_path / "context.db"), str(tmp_path / "media"))
    await store.initialize()
    scheduler = PipelineScheduler(config, ToolRegistry(), Sender(), store)
    observed: dict[str, object] = {}

    async def fake_pipeline(*, message, deps, **_kwargs):
        observed["message"] = message
        observed["remember_input"] = deps.remember_input
        return "no_reply"

    monkeypatch.setattr(pipeline_module, "pipeline", fake_pipeline)
    try:
        task_id = await store.add_scheduled_task(
            group_key="private:alice",
            peer_chat_type=1,
            peer_uid="alice",
            prompt="run the research task",
            context="Read skills/research/BOT.md and use Chinese.",
            scheduled_at=time.time(),
        )
        record = await store.get_scheduled_task(task_id)
        assert record is not None
        assert record.context == "Read skills/research/BOT.md and use Chinese."

        await scheduler._fire_scheduled_task(record)

        assert observed["message"] == (
            "[Scheduled task 1] run the research task\n"
            "[Additional task context]\n"
            "Read skills/research/BOT.md and use Chinese."
        )
        assert observed["remember_input"] is False
        assert (await store.get_scheduled_task(task_id)).status == "done"
    finally:
        await scheduler.shutdown()


async def test_cron_task_persists_and_reschedules_after_fire(monkeypatch, tmp_path) -> None:
    config = Config()
    config.heartbeat.enabled = False
    store = MessageStore(str(tmp_path / "cron.db"), str(tmp_path / "media"))
    await store.initialize()
    scheduler = PipelineScheduler(config, ToolRegistry(), Sender(), store)
    fired = asyncio.Event()

    async def fake_pipeline(*, deps, **_kwargs):
        fired.set()

    monkeypatch.setattr(pipeline_module, "pipeline", fake_pipeline)
    try:
        task_id = await scheduler.schedule_task(
            scheduled_at=time.time(),
            prompt="cron work",
            group_key="private:alice",
            peer=Peer(chat_type=1, peer_uid="alice"),
            cron_expression="* * * * *",
        )
        await asyncio.wait_for(fired.wait(), timeout=1)
        record = await store.get_scheduled_task(task_id)
        assert record is not None
        assert record.schedule_kind == "cron"
        assert record.cron_expression == "* * * * *"
        assert record.status == "pending"
        assert record.last_run_at is not None
        assert record.scheduled_at > time.time()
    finally:
        await scheduler.shutdown()


async def test_scheduler_rejects_interval_and_cron_together(tmp_path) -> None:
    config = Config()
    config.heartbeat.enabled = False
    store = MessageStore(str(tmp_path / "cron-invalid.db"), str(tmp_path / "media"))
    await store.initialize()
    scheduler = PipelineScheduler(config, ToolRegistry(), Sender(), store)
    try:
        with pytest.raises(ValueError, match="cannot be combined"):
            await scheduler.schedule_task(
                scheduled_at=time.time() + 60,
                prompt="invalid",
                group_key="private:alice",
                peer=Peer(chat_type=1, peer_uid="alice"),
                interval_seconds=60,
                cron_expression="* * * * *",
            )
    finally:
        await scheduler.shutdown()


async def test_initialize_migrates_legacy_scheduled_task_columns(tmp_path) -> None:
    db_path = tmp_path / "legacy-schedule.db"
    connection = sqlite3.connect(db_path)
    connection.execute(
        "CREATE TABLE scheduled_tasks ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, group_key TEXT NOT NULL, "
        "peer_chat_type INTEGER NOT NULL, peer_uid TEXT NOT NULL, prompt TEXT NOT NULL, "
        "scheduled_at REAL NOT NULL, status TEXT NOT NULL DEFAULT 'pending', "
        "created_at REAL NOT NULL DEFAULT 0)"
    )
    connection.execute(
        "INSERT INTO scheduled_tasks "
        "(group_key, peer_chat_type, peer_uid, prompt, scheduled_at) "
        "VALUES ('private:legacy', 1, 'legacy', 'migrate me', 123.0)"
    )
    connection.commit()
    connection.close()

    store = MessageStore(str(db_path), str(tmp_path / "media"))
    await store.initialize()
    try:
        record = await store.get_scheduled_task(1)
        assert record is not None
        assert record.schedule_kind == "once"
        assert record.interval_seconds is None
        assert record.last_run_at is None
    finally:
        await store.close()


async def test_startup_reopens_running_tasks_and_restores_timers(tmp_path) -> None:
    config = Config()
    config.heartbeat.enabled = False
    store = MessageStore(str(tmp_path / "restore.db"), str(tmp_path / "media"))
    await store.initialize()
    task_id = await store.add_scheduled_task(
        group_key="private:alice",
        peer_chat_type=1,
        peer_uid="alice",
        prompt="restore me",
        scheduled_at=time.time() + 3600,
    )
    running_id = await store.add_scheduled_task(
        group_key="private:bob",
        peer_chat_type=1,
        peer_uid="bob",
        prompt="reopen me",
        scheduled_at=time.time() + 3600,
    )
    assert await store.claim_scheduled_task(running_id) is True
    scheduler = PipelineScheduler(config, ToolRegistry(), Sender(), store)
    try:
        await scheduler.startup()

        assert set(scheduler._scheduled_tasks) == {task_id, running_id}
        assert (await store.get_scheduled_task(running_id)).status == "pending"
    finally:
        await scheduler.shutdown()


async def test_scheduled_terminal_transition_cannot_resurrect_cancelled_task(tmp_path) -> None:
    store = MessageStore(str(tmp_path / "atomic.db"), str(tmp_path / "media"))
    await store.initialize()
    try:
        task_id = await store.add_scheduled_task(
            group_key="private:alice",
            peer_chat_type=1,
            peer_uid="alice",
            prompt="atomic task",
            scheduled_at=time.time(),
        )
        assert await store.claim_scheduled_task(task_id) is True
        await store.mark_scheduled_task_status(task_id, "cancelled")

        assert await store.complete_running_task(task_id) is False
        assert await store.fail_running_task(task_id) is False
        assert await store.reschedule_running_task(
            task_id,
            scheduled_at=time.time() + 60,
            last_run_at=time.time(),
        ) is False
        assert await store.defer_scheduled_task(
            task_id,
            scheduled_at=time.time() + 60,
        ) is False
        assert (await store.get_scheduled_task(task_id)).status == "cancelled"
    finally:
        await store.close()


async def test_scheduled_task_lifecycle_is_bound_to_conversation(tmp_path) -> None:
    config = Config()
    config.heartbeat.enabled = False
    store = MessageStore(str(tmp_path / "schedule.db"), str(tmp_path / "media"))
    await store.initialize()
    scheduler = PipelineScheduler(config, ToolRegistry(), Sender(), store)
    try:
        task_id = await scheduler.schedule_task(
            scheduled_at=time.time() + 3600,
            prompt="private work",
            group_key="private:alice",
            peer=Peer(chat_type=1, peer_uid="alice"),
        )

        denied = await scheduler.update_scheduled_task_lifecycle(
            task_id,
            "cancel",
            group_key="private:bob",
        )
        assert "not bound" in denied
        assert (await store.get_scheduled_task(task_id)).status == "pending"
        assert len(await scheduler.list_scheduled_tasks(group_key="private:bob")) == 0
        assert len(await scheduler.list_scheduled_tasks(group_key="private:alice")) == 1
    finally:
        await scheduler.shutdown()


async def test_scheduled_pipeline_owns_the_conversation_slot(monkeypatch, tmp_path) -> None:
    config = Config()
    config.heartbeat.enabled = False
    store = MessageStore(str(tmp_path / "schedule.db"), str(tmp_path / "media"))
    await store.initialize()
    scheduler = PipelineScheduler(config, ToolRegistry(), Sender(), store)
    observed: list[bool] = []
    started = asyncio.Event()

    async def fake_pipeline(*, deps, **_kwargs):
        observed.append(scheduler._tasks.get(deps.conversation_id) is asyncio.current_task())
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(pipeline_module, "pipeline", fake_pipeline)
    task_id = await store.add_scheduled_task(
        group_key="private:alice",
        peer_chat_type=1,
        peer_uid="alice",
        prompt="scheduled work",
        scheduled_at=time.time(),
    )
    record = ScheduledTaskRecord(
        id=task_id,
        group_key="private:alice",
        peer_chat_type=1,
        peer_uid="alice",
        prompt="scheduled work",
        scheduled_at=time.time(),
        status="pending",
        created_at=time.time(),
    )
    try:
        task = asyncio.create_task(scheduler._fire_scheduled_task(record))
        await asyncio.wait_for(started.wait(), timeout=1)
        assert observed == [True]
        await scheduler.cancel_user("private:alice")
        await asyncio.gather(task, return_exceptions=True)
        assert "private:alice" not in scheduler._tasks
    finally:
        await scheduler.shutdown()


async def test_due_schedule_does_not_overwrite_active_user_pipeline(monkeypatch, tmp_path) -> None:
    config = Config()
    config.heartbeat.enabled = False
    store = MessageStore(str(tmp_path / "slot-race.db"), str(tmp_path / "media"))
    await store.initialize()
    scheduler = PipelineScheduler(config, ToolRegistry(), Sender(), store)
    key = "private:alice"
    scheduler._ensure_user_state(key)
    user_started = asyncio.Event()
    release_user = asyncio.Event()
    scheduled_started = asyncio.Event()

    async def user_pipeline() -> None:
        user_started.set()
        await release_user.wait()

    async def fake_pipeline(*, deps, **_kwargs):
        scheduled_started.set()

    monkeypatch.setattr(pipeline_module, "pipeline", fake_pipeline)
    user_task = asyncio.create_task(user_pipeline())
    scheduler._tasks[key] = user_task
    task_id = await store.add_scheduled_task(
        group_key=key,
        peer_chat_type=1,
        peer_uid="alice",
        prompt="wait for user",
        scheduled_at=time.time(),
    )
    record = ScheduledTaskRecord(
        id=task_id,
        group_key=key,
        peer_chat_type=1,
        peer_uid="alice",
        prompt="wait for user",
        scheduled_at=time.time(),
        status="pending",
        created_at=time.time(),
    )
    try:
        await asyncio.wait_for(user_started.wait(), timeout=1)
        scheduled_task = asyncio.create_task(scheduler._fire_scheduled_task(record))
        await asyncio.sleep(0.05)
        assert scheduler._tasks[key] is user_task
        assert not scheduled_started.is_set()

        release_user.set()
        await asyncio.wait_for(scheduled_task, timeout=1)
        assert scheduled_started.is_set()
    finally:
        release_user.set()
        await asyncio.gather(user_task, return_exceptions=True)
        await scheduler.shutdown()


async def test_due_schedule_rechecks_pause_after_waiting_for_user(monkeypatch, tmp_path) -> None:
    config = Config()
    config.heartbeat.enabled = False
    store = MessageStore(str(tmp_path / "schedule-pause-race.db"), str(tmp_path / "media"))
    await store.initialize()
    scheduler = PipelineScheduler(config, ToolRegistry(), Sender(), store)
    key = "private:alice"
    scheduler._ensure_user_state(key)
    user_started = asyncio.Event()
    release_user = asyncio.Event()
    pipeline_called = False

    async def user_pipeline() -> None:
        user_started.set()
        await release_user.wait()

    async def fake_pipeline(**_kwargs):
        nonlocal pipeline_called
        pipeline_called = True

    monkeypatch.setattr(pipeline_module, "pipeline", fake_pipeline)
    user_task = asyncio.create_task(user_pipeline())
    scheduler._tasks[key] = user_task
    task_id = await store.add_scheduled_task(
        group_key=key,
        peer_chat_type=1,
        peer_uid="alice",
        prompt="must not run after pause",
        scheduled_at=time.time(),
    )
    record = ScheduledTaskRecord(
        id=task_id,
        group_key=key,
        peer_chat_type=1,
        peer_uid="alice",
        prompt="must not run after pause",
        scheduled_at=time.time(),
        status="pending",
        created_at=time.time(),
    )
    try:
        await asyncio.wait_for(user_started.wait(), timeout=1)
        scheduled_task = asyncio.create_task(scheduler._fire_scheduled_task(record))
        await asyncio.sleep(0.05)
        await store.mark_scheduled_task_status(task_id, "paused")
        release_user.set()

        assert await asyncio.wait_for(scheduled_task, timeout=1) is False
        assert pipeline_called is False
        saved = await store.get_scheduled_task(task_id)
        assert saved is not None and saved.status == "paused"
    finally:
        release_user.set()
        await asyncio.gather(user_task, return_exceptions=True)
        await scheduler.shutdown()


async def test_interval_task_fires_and_persists_next_run(monkeypatch, tmp_path) -> None:
    config = Config()
    config.heartbeat.enabled = False
    store = MessageStore(str(tmp_path / "schedule.db"), str(tmp_path / "media"))
    await store.initialize()
    scheduler = PipelineScheduler(config, ToolRegistry(), Sender(), store)
    fired = asyncio.Event()
    calls: list[str] = []

    async def fake_pipeline(*, deps, **_kwargs):
        calls.append(deps.pipeline_id)
        fired.set()

    monkeypatch.setattr(pipeline_module, "pipeline", fake_pipeline)
    try:
        task_id = await scheduler.schedule_task(
            scheduled_at=time.time(),
            prompt="periodic work",
            group_key="private:alice",
            peer=Peer(chat_type=1, peer_uid="alice"),
            interval_seconds=1,
        )
        await asyncio.wait_for(fired.wait(), timeout=1)
        record = await store.get_scheduled_task(task_id)
        assert record is not None
        assert calls
        assert record.status == "pending"
        assert record.last_run_at is not None
        assert record.scheduled_at > time.time()

        assert "cancelled" in await scheduler.update_scheduled_task_lifecycle(task_id, "cancel")
        await asyncio.sleep(0)
        assert (await store.get_scheduled_task(task_id)).status == "cancelled"
    finally:
        await scheduler.shutdown()


async def test_one_shot_task_records_pipeline_error_instead_of_done(monkeypatch, tmp_path) -> None:
    config = Config()
    config.heartbeat.enabled = False
    store = MessageStore(str(tmp_path / "schedule-error.db"), str(tmp_path / "media"))
    await store.initialize()
    scheduler = PipelineScheduler(config, ToolRegistry(), Sender(), store)

    async def failed_pipeline(*, deps, **_kwargs):
        return "error"

    monkeypatch.setattr(pipeline_module, "pipeline", failed_pipeline)
    try:
        task_id = await store.add_scheduled_task(
            group_key="private:alice",
            peer_chat_type=1,
            peer_uid="alice",
            prompt="fail once",
            scheduled_at=time.time(),
        )
        record = ScheduledTaskRecord(
            id=task_id,
            group_key="private:alice",
            peer_chat_type=1,
            peer_uid="alice",
            prompt="fail once",
            scheduled_at=time.time(),
            status="pending",
            created_at=time.time(),
        )

        assert await scheduler._fire_scheduled_task(record) is False
        saved = await store.get_scheduled_task(task_id)
        assert saved is not None and saved.status == "error"
    finally:
        await scheduler.shutdown()


async def test_one_shot_cancelled_pipeline_is_deferred(monkeypatch, tmp_path) -> None:
    config = Config()
    config.heartbeat.enabled = False
    store = MessageStore(str(tmp_path / "schedule-cancel.db"), str(tmp_path / "media"))
    await store.initialize()
    scheduler = PipelineScheduler(config, ToolRegistry(), Sender(), store)

    async def cancelled_pipeline(*, deps, **_kwargs):
        return "cancelled"

    monkeypatch.setattr(pipeline_module, "pipeline", cancelled_pipeline)
    try:
        task_id = await store.add_scheduled_task(
            group_key="private:alice",
            peer_chat_type=1,
            peer_uid="alice",
            prompt="retry after interruption",
            scheduled_at=time.time(),
        )
        record = ScheduledTaskRecord(
            id=task_id,
            group_key="private:alice",
            peer_chat_type=1,
            peer_uid="alice",
            prompt="retry after interruption",
            scheduled_at=time.time(),
            status="pending",
            created_at=time.time(),
        )

        assert await scheduler._fire_scheduled_task(record) is False
        saved = await store.get_scheduled_task(task_id)
        assert saved is not None
        assert saved.status == "pending"
        assert saved.scheduled_at > time.time()
    finally:
        await scheduler.shutdown()


async def test_cancelled_task_report_retries_as_a_fresh_pipeline(monkeypatch, tmp_path) -> None:
    config = Config()
    config.heartbeat.enabled = False
    config.opencode.enabled = True
    store = MessageStore(str(tmp_path / "report.db"), str(tmp_path / "media"))
    await store.initialize()
    scheduler = PipelineScheduler(config, ToolRegistry(), Sender(), store)
    run_id = "oc-retry"
    event_id = "task-report:oc-retry"
    try:
        await store.create_delegated_task(
            run_id=run_id,
            conversation_id="private:alice",
            prompt="do work",
            workspace=str(tmp_path),
            report_token="token",
        )
        await store.accept_delegated_report(run_id, "token", "finished")
        await store.append_event(EventRecord(
            event_id=event_id,
            conversation_id="private:alice",
            actor_id="service:opencode",
            actor_kind="service",
            event_type=EventType.INBOUND.value,
            content="finished",
            pipeline_id="task-report:oc-retry",
            status="cancelled",
        ))
        await store.update_delegated_task(run_id, report_event_id=event_id)

        async def fake_pipeline(*, deps, **_kwargs):
            await store.update_event_status(event_id, "finalized")

        monkeypatch.setattr(pipeline_module, "pipeline", fake_pipeline)
        await scheduler._run_task_report("private:alice", run_id, event_id)

        record = await store.get_delegated_task(run_id)
        event = await store.get_event(event_id)
        assert record is not None and record.status == "delivered"
        assert event is not None and event.status == "finalized"
        assert event.pipeline_id.startswith("task-report:oc-retry:")
    finally:
        await scheduler.shutdown()


async def test_report_pipeline_that_does_not_finalize_is_not_delivered(monkeypatch, tmp_path) -> None:
    config = Config()
    config.heartbeat.enabled = False
    config.opencode.enabled = True
    store = MessageStore(str(tmp_path / "report-cancel.db"), str(tmp_path / "media"))
    await store.initialize()
    scheduler = PipelineScheduler(config, ToolRegistry(), Sender(), store)
    run_id = "oc-report-cancel"
    event_id = "task-report:oc-report-cancel"
    try:
        await store.create_delegated_task(
            run_id=run_id,
            conversation_id="private:alice",
            prompt="do work",
            workspace=str(tmp_path),
            report_token="token",
        )
        await store.accept_delegated_report(run_id, "token", "finished")
        await store.append_event(EventRecord(
            event_id=event_id,
            conversation_id="private:alice",
            actor_id="service:opencode",
            actor_kind="service",
            event_type=EventType.INBOUND.value,
            content="finished",
            pipeline_id="task-report:oc-report-cancel",
            status="received",
        ))
        await store.update_delegated_task(run_id, report_event_id=event_id)

        async def cancelled_pipeline(*, deps, **_kwargs):
            return "cancelled"

        monkeypatch.setattr(pipeline_module, "pipeline", cancelled_pipeline)
        with pytest.raises(RuntimeError, match="did not finalize"):
            await scheduler._run_task_report("private:alice", run_id, event_id)

        record = await store.get_delegated_task(run_id)
        event = await store.get_event(event_id)
        assert record is not None and record.status == "reported"
        assert event is not None and event.status == "received"
    finally:
        await scheduler.shutdown()


async def test_report_rebuilds_missing_event_from_durable_report(tmp_path) -> None:
    config = Config()
    config.heartbeat.enabled = False
    config.opencode.enabled = True
    store = MessageStore(str(tmp_path / "missing-report-event.db"), str(tmp_path / "media"))
    await store.initialize()
    scheduler = PipelineScheduler(config, ToolRegistry(), Sender(), store)
    try:
        await store.create_delegated_task(
            run_id="oc-missing-event",
            conversation_id="private:alice",
            prompt="do work",
            workspace=str(tmp_path),
            report_token="token",
        )
        assert await store.accept_delegated_report(
            "oc-missing-event", "token", "durable report"
        ) is True
        await store.update_delegated_task(
            "oc-missing-event",
            report_event_id="task-report:oc-missing-event",
        )

        await scheduler._accept_opencode_report("oc-missing-event", "transient duplicate")

        event = await store.get_event("task-report:oc-missing-event")
        assert event is not None
        assert event.content == "durable report"
    finally:
        await scheduler.shutdown()


async def test_duplicate_report_callbacks_create_one_wakeup(monkeypatch, tmp_path) -> None:
    config = Config()
    config.heartbeat.enabled = False
    config.opencode.enabled = True
    store = MessageStore(str(tmp_path / "report-once.db"), str(tmp_path / "media"))
    await store.initialize()
    scheduler = PipelineScheduler(config, ToolRegistry(), Sender(), store)
    started = asyncio.Event()
    release = asyncio.Event()
    calls: list[tuple[str, str]] = []

    async def fake_report_pipeline(key: str, run_id: str, event_id: str) -> None:
        calls.append((run_id, event_id))
        started.set()
        await release.wait()

    monkeypatch.setattr(scheduler, "_run_task_report", fake_report_pipeline)
    try:
        await store.create_delegated_task(
            run_id="oc-once",
            conversation_id="private:alice",
            prompt="do work",
            workspace=str(tmp_path),
            report_token="token",
        )

        await scheduler._accept_opencode_report("oc-once", "finished")
        await asyncio.wait_for(started.wait(), timeout=1)
        await scheduler._accept_opencode_report("oc-once", "duplicate")

        assert calls == [("oc-once", "task-report:oc-once")]
        release.set()
        await scheduler._task_report_queues["private:alice"].join()
    finally:
        release.set()
        await scheduler.shutdown()


async def test_startup_restores_reported_task_delivery(monkeypatch, tmp_path) -> None:
    config = Config()
    config.heartbeat.enabled = False
    config.opencode.enabled = True
    store = MessageStore(str(tmp_path / "report-restore.db"), str(tmp_path / "media"))
    await store.initialize()
    run_id = "oc-restore"
    event_id = f"task-report:{run_id}"
    try:
        await store.create_delegated_task(
            run_id=run_id,
            conversation_id="private:alice",
            prompt="do work",
            workspace=str(tmp_path),
            report_token="token",
        )
        assert await store.accept_delegated_report(run_id, "token", "restored report")
        await store.append_event(EventRecord(
            event_id=event_id,
            conversation_id="private:alice",
            actor_id="service:opencode",
            actor_kind="service",
            event_type=EventType.INBOUND.value,
            content="restored report",
            pipeline_id=event_id,
            status="received",
        ))
        await store.update_delegated_task(run_id, report_event_id=event_id)

        async def fake_pipeline(*, deps, **_kwargs):
            await deps.store.update_event_status(event_id, "finalized")
            return "responded"

        monkeypatch.setattr(pipeline_module, "pipeline", fake_pipeline)
        scheduler = PipelineScheduler(config, ToolRegistry(), Sender(), store)
        try:
            await scheduler.startup()
            await scheduler._task_report_queues["private:alice"].join()

            record = await store.get_delegated_task(run_id)
            event = await store.get_event(event_id)
            assert record is not None and record.status == "delivered"
            assert event is not None and event.status == "finalized"
        finally:
            await scheduler.shutdown()
    finally:
        if store._conn is not None:
            await store.close()
