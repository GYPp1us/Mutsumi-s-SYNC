import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from src.mutsumi_sync.memory.store import EventRecord
from src.mutsumi_sync.prototype.storage import BackgroundJob, PrototypeStore


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[PrototypeStore]:
    instance = PrototypeStore(db_path=str(tmp_path / "prototype.db"), media_dir=str(tmp_path / "media"))
    await instance.initialize()
    try:
        yield instance
    finally:
        await instance.close()


async def _create(store: PrototypeStore, event_id: str = "event-1", conversation: str = "private:123") -> BackgroundJob:
    return await store.create_job(
        conversation_id=conversation, actor_id="qq:123", peer_chat_type=1, peer_uid="123",
        request="查一下这件事，原话不能丢。", input_event_ids=[event_id],
    )


async def test_jobs_and_original_events_survive_restart(tmp_path: Path) -> None:
    args = {"db_path": str(tmp_path / "prototype.db"), "media_dir": str(tmp_path / "media")}
    first = PrototypeStore(**args)
    await first.initialize()
    try:
        event = await first.append_event(EventRecord(
            conversation_id="private:123", actor_id="qq:123", actor_kind="human",
            event_type="inbound", content="原始问题", status="received",
        ))
        job = await _create(first, event.event_id)
        await first.update_job(job.id, updates=["补充要求"], status="completed", result="已验证的结果")
    finally:
        await first.close()
    second = PrototypeStore(**args)
    await second.initialize()
    try:
        restored = await second.get_job(job.id)
        assert restored.input_event_ids == [event.event_id]
        assert restored.updates == ["补充要求"]
        assert restored.result == "已验证的结果"
        assert restored.peer_uid == "123"
        assert not restored.delivered
        assert (await second.get_events(finalized_only=False))[0].content == "原始问题"
        assert (await second.recover_jobs())[0].id == job.id
    finally:
        await second.close()


async def test_recovery_preserves_unknown_actions_and_undelivered_results(store: PrototypeStore) -> None:
    queued = await _create(store, "queued")
    running = await _create(store, "running")
    completed = await _create(store, "completed")
    sent = await _create(store, "sent")
    cancelled = await _create(store, "cancelled")
    await store.update_job(running.id, status="running", active_action="call-1 save_reminder")
    await store.update_job(completed.id, status="completed", result="可交付结果")
    await store.update_job(sent.id, status="completed", result="已经送达", delivered=True)
    await store.update_job(cancelled.id, status="cancelled")
    recovered = {job.id: job for job in await store.recover_jobs()}
    assert set(recovered) == {queued.id, running.id, completed.id, cancelled.id}
    assert recovered[queued.id].status == "queued"
    assert recovered[running.id].status == "unknown"
    assert recovered[running.id].active_action == "call-1 save_reminder"
    assert recovered[completed.id].result == "可交付结果"
    assert (await store.get_job(sent.id)).delivered
    assert {job.id: job for job in await store.recover_jobs()} == recovered


async def test_creation_is_idempotent_by_conversation_and_original_input(store: PrototypeStore) -> None:
    jobs = await asyncio.gather(*(_create(store) for _ in range(3)))
    assert len({job.id for job in jobs}) == 1
    assert len(await store.list_jobs()) == 1
    other = await _create(store, conversation="group:456")
    assert other.id != jobs[0].id
    assert [job.id for job in await store.list_jobs("private:123")] == [jobs[0].id]
    assert len(await store.list_jobs()) == 2


async def test_empty_input_references_do_not_merge_unrelated_jobs(store: PrototypeStore) -> None:
    args = dict(conversation_id="private:123", actor_id="qq:123", peer_chat_type=1,
                peer_uid="123", request="独立工作", input_event_ids=[])
    first = await store.create_job(**args)
    second = await store.create_job(**args)
    assert first.id != second.id


async def test_updates_reject_routing_changes_and_invalid_state(store: PrototypeStore) -> None:
    job = await _create(store)
    for fields in (
        {"peer_uid": "999"}, {"conversation_id": "private:999"},
        {"actor_id": "qq:999"}, {"request": "改写原话"},
        {"status": "invented"}, {"updates": "not a list"},
        {"updates": [3]}, {"delivered": "false"}, {"result": {"data": "x"}},
        {"status = 'completed'; --": "x"},
    ):
        with pytest.raises(ValueError):
            await store.update_job(job.id, **fields)
    assert await store.get_job(job.id) == job
    with pytest.raises(KeyError):
        await store.update_job("missing", status="failed")
    assert await store.get_job("missing") is None
