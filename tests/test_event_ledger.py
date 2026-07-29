from __future__ import annotations

import pytest

from src.mutsumi_sync.memory.store import ActorRecord, EpisodeRecord, EventRecord, MessageStore, EventType
from src.mutsumi_sync.memory.projection import build_global_life_context, build_life_stream


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


@pytest.mark.asyncio
async def test_projection_replaces_covered_global_events_with_episode(tmp_path):
    store = MessageStore(str(tmp_path / "ledger.db"), str(tmp_path / "media"))
    await store.initialize()
    try:
        first = await store.append_event(EventRecord(
            conversation_id="qq:private:alice", actor_id="qq:user:alice",
            actor_kind="human", event_type=EventType.INBOUND.value,
            content="public fact one", visibility="global",
        ))
        second = await store.append_event(EventRecord(
            conversation_id="qq:private:alice", actor_id="bot:self",
            actor_kind="bot", event_type=EventType.OUTBOUND.value,
            content="public fact two", visibility="global",
        ))
        await store.add_episode(EpisodeRecord(
            conversation_id="qq:private:alice",
            first_sequence=first.sequence or 0,
            last_sequence=second.sequence or 0,
            narrative="Alice and Mutsumi established two public facts.",
        ))
        context = await build_global_life_context(store, "qq:private:bob")
        assert "<episode" in context
        assert "public fact one" not in context
        assert "public fact two" not in context
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_projection_episode_coverage_ignores_interleaved_conversations(tmp_path):
    store = MessageStore(str(tmp_path / "ledger.db"), str(tmp_path / "media"))
    await store.initialize()
    try:
        first = await store.append_event(EventRecord(
            conversation_id="private:alice", actor_id="bot:self",
            actor_kind="bot", event_type=EventType.OUTBOUND.value,
            content="global one", visibility="global",
        ))
        await store.append_event(EventRecord(
            conversation_id="private:charlie", actor_id="qq:user:charlie",
            actor_kind="human", event_type=EventType.INBOUND.value,
            content="private interleaved event", visibility="private",
        ))
        last = await store.append_event(EventRecord(
            conversation_id="private:alice", actor_id="bot:self",
            actor_kind="bot", event_type=EventType.OUTBOUND.value,
            content="global two", visibility="global",
        ))
        await store.add_episode(EpisodeRecord(
            conversation_id="private:alice",
            first_sequence=first.sequence or 0,
            last_sequence=last.sequence or 0,
            narrative="Two global events from Alice's conversation.",
        ))

        context = await build_global_life_context(store, "private:bob")

        assert "Two global events from Alice" in context
        assert "private interleaved event" not in context
        assert "global one" not in context
        assert "global two" not in context
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_projection_selects_latest_global_events_without_private_noise(tmp_path):
    store = MessageStore(str(tmp_path / "ledger.db"), str(tmp_path / "media"))
    await store.initialize()
    try:
        await store.append_event(EventRecord(
            conversation_id="private:alice", actor_id="bot:self",
            actor_kind="bot", event_type=EventType.STATE_CHANGE.value,
            content="old global state", visibility="global",
        ))
        for index in range(120):
            await store.append_event(EventRecord(
                conversation_id=f"private:noise-{index}", actor_id=f"qq:user:{index}",
                actor_kind="human", event_type=EventType.INBOUND.value,
                content=f"private noise {index}", visibility="private",
            ))
        await store.append_event(EventRecord(
            conversation_id="private:alice", actor_id="bot:self",
            actor_kind="bot", event_type=EventType.STATE_CHANGE.value,
            content="new global state", visibility="global",
        ))

        context = await build_global_life_context(store, "private:bob", limit=1)

        assert "new global state" in context
        assert "old global state" not in context
        assert "private noise" not in context
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_media_ledger_deduplicates_binary_and_keeps_description(tmp_path):
    store = MessageStore(str(tmp_path / "ledger.db"), str(tmp_path / "media"))
    await store.initialize()
    try:
        first = await store.register_media(b"same image", kind="image", ext="png")
        second = await store.register_media(b"same image", kind="image", ext="png")
        assert first.media_id == second.media_id
        assert first.sha256 == second.sha256
        assert len(await store.list_media(kind="image")) == 1
        await store.update_media_description(first.media_id, "A small blue square.", "blue square")
        saved = await store.get_media(first.media_id)
        assert saved is not None
        assert saved.short_description == "blue square"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_actor_registry_updates_global_alias_and_relationship(tmp_path):
    store = MessageStore(str(tmp_path / "ledger.db"), str(tmp_path / "media"))
    await store.initialize()
    try:
        actor = await store.ensure_actor(ActorRecord(
            actor_id="qq:user:alice",
            kind="human",
            platform="qq",
            platform_subject_id="alice",
            display_name="Alice",
        ))
        assert actor.private_alias == ""
        updated = await store.update_actor_profile(
            actor.actor_id,
            private_alias="主人",
            relationship="owner",
        )
        assert updated is not None
        assert updated.private_alias == "主人"
        assert updated.relationship == "owner"
        assert (await store.get_actor(actor.actor_id)).private_alias == "主人"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_life_stream_replays_tool_round_as_native_provider_messages(tmp_path):
    store = MessageStore(str(tmp_path / "ledger.db"), str(tmp_path / "media"))
    await store.initialize()
    try:
        await store.append_event(EventRecord(
            conversation_id="private:alice", actor_id="qq:user:alice", actor_kind="human",
            event_type=EventType.INBOUND.value, content="查一下天气", turn_id="turn-in",
        ))
        await store.append_event(EventRecord(
            conversation_id="private:alice", actor_id="bot:self", actor_kind="bot",
            event_type=EventType.TOOL_CALL.value, turn_id="pipeline:step:1",
            content='{"tool":"weather","call_id":"call-1","arguments":{"city":"上海"}}',
            payload={
                "tool": "weather", "call_id": "call-1", "arguments": {"city": "上海"},
                "assistant_content": "",
            },
        ))
        await store.append_event(EventRecord(
            conversation_id="private:alice", actor_id="bot:self", actor_kind="bot",
            event_type=EventType.TOOL_RESULT.value, turn_id="pipeline:step:1",
            content="晴天", payload={"tool": "weather", "call_id": "call-1", "result": "晴天"},
        ))
        await store.append_event(EventRecord(
            conversation_id="private:alice", actor_id="bot:self", actor_kind="bot",
            event_type=EventType.OUTBOUND.value, content="今天上海晴天。",
        ))

        stream = await build_life_stream(store)
        assert [item["role"] for item in stream] == ["user", "assistant", "tool", "assistant"]
        assert stream[1]["tool_calls"][0]["function"]["name"] == "weather"
        assert stream[1]["tool_calls"][0]["function"]["arguments"] == '{"city": "上海"}'
        assert stream[2] == {"role": "tool", "tool_call_id": "call-1", "content": "晴天"}
        assert "[assistant]" not in "\n".join(str(item) for item in stream)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_life_stream_keeps_interleaved_pipeline_tool_round_contiguous(tmp_path):
    store = MessageStore(str(tmp_path / "ledger.db"), str(tmp_path / "media"))
    await store.initialize()
    try:
        await store.append_event(EventRecord(
            conversation_id="private:alice", actor_id="bot:self", actor_kind="bot",
            event_type=EventType.TOOL_CALL.value, turn_id="pipeline-a:step:1",
            payload={"tool": "lookup", "call_id": "a-1", "arguments": {}},
        ))
        await store.append_event(EventRecord(
            conversation_id="private:bob", actor_id="qq:user:bob", actor_kind="human",
            event_type=EventType.INBOUND.value, content="另一条聊天消息",
        ))
        await store.append_event(EventRecord(
            conversation_id="private:alice", actor_id="bot:self", actor_kind="bot",
            event_type=EventType.TOOL_RESULT.value, turn_id="pipeline-a:step:1",
            payload={"tool": "lookup", "call_id": "a-1", "result": "查到结果"},
        ))

        stream = await build_life_stream(store)
        assert [item["role"] for item in stream] == ["assistant", "tool", "user"]
        assert stream[1]["content"] == "查到结果"
        assert "另一条聊天消息" in stream[2]["content"]
    finally:
        await store.close()
