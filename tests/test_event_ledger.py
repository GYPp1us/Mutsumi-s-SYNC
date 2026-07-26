from __future__ import annotations

import pytest

from src.mutsumi_sync.memory.store import EpisodeRecord, EventRecord, MessageStore, EventType


@pytest.mark.asyncio
async def test_event_ledger_preserves_global_order_and_conversation_projection(tmp_path):
    store = MessageStore(str(tmp_path / "ledger.db"), str(tmp_path / "media"))
    await store.initialize()
    try:
        first = await store.append_event(EventRecord(
            conversation_id="qq:private:alice",
            actor_id="qq:user:alice",
            actor_kind="human",
            event_type=EventType.INBOUND.value,
            content="hello",
            visibility="private",
        ))
        second = await store.append_event(EventRecord(
            conversation_id="qq:group:123",
            actor_id="qq:user:bob",
            actor_kind="human",
            event_type=EventType.INBOUND.value,
            content="morning",
            visibility="group",
        ))
        assert first.sequence is not None
        assert second.sequence == first.sequence + 1

        events = await store.get_events(conversation_id="qq:private:alice")
        assert [event.content for event in events] == ["hello"]
        assert (await store.get_events(after_sequence=first.sequence or 0))[0].event_id == second.event_id
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_episode_keeps_exact_event_coverage(tmp_path):
    store = MessageStore(str(tmp_path / "ledger.db"), str(tmp_path / "media"))
    await store.initialize()
    try:
        episode_id = await store.add_episode(EpisodeRecord(
            conversation_id="qq:private:alice",
            first_sequence=10,
            last_sequence=18,
            narrative="Alice and Mutsumi discussed the unfinished plan.",
            participants_json='["qq:user:alice", "bot:self"]',
            open_loops="finish the plan",
        ))
        episodes = await store.get_episodes("qq:private:alice")
        assert episodes[0].episode_id == episode_id
        assert episodes[0].first_sequence == 10
        assert episodes[0].last_sequence == 18
        assert episodes[0].open_loops == "finish the plan"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_event_status_can_finalize_or_cancel_without_changing_identity(tmp_path):
    store = MessageStore(str(tmp_path / "ledger.db"), str(tmp_path / "media"))
    await store.initialize()
    try:
        event = await store.append_event(EventRecord(
            conversation_id="qq:private:alice",
            actor_id="qq:user:alice",
            actor_kind="human",
            event_type=EventType.INBOUND.value,
            content="in progress",
            status="received",
        ))
        await store.update_event_status(event.event_id or "", "cancelled")
        rows = await store.get_events(conversation_id="qq:private:alice", finalized_only=False)
        assert len(rows) == 1
        assert rows[0].event_id == event.event_id
        assert rows[0].content == "in progress"
        assert rows[0].status == "cancelled"
        assert await store.get_events(conversation_id="qq:private:alice") == []
    finally:
        await store.close()
