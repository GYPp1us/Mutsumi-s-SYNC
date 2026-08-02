from __future__ import annotations

import pytest

from src.mutsumi_sync.memory.projection import build_life_stream
from src.mutsumi_sync.memory.store import EpisodeRecord, EventRecord, EventType, MessageStore
from src.mutsumi_sync.memory.episodes import summarize_pending_episode
from src.mutsumi_sync.config import Config
import src.mutsumi_sync.memory.episodes as episodes_module


async def test_episode_replaces_covered_raw_events_in_live_projection(tmp_path) -> None:
    store = MessageStore(str(tmp_path / "context.db"), str(tmp_path / "media"))
    await store.initialize()
    try:
        first = await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="qq:user:alice",
            actor_kind="human",
            event_type=EventType.INBOUND.value,
            content="old question",
            pipeline_id="turn-1",
        ))
        last = await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="bot:self",
            actor_kind="bot",
            event_type=EventType.OUTBOUND.value,
            content="old answer",
            pipeline_id="turn-1",
        ))
        await store.add_episode(EpisodeRecord(
            conversation_id="private:alice",
            first_sequence=first.sequence or 0,
            last_sequence=last.sequence or 0,
            narrative="Alice and Mutsumi completed an old exchange.",
        ))

        stream = await build_life_stream(store, max_tokens=1000)

        assert len(stream) == 1
        assert stream[0]["role"] == "user"
        assert "completed an old exchange" in stream[0]["content"]
        assert "old question" not in stream[0]["content"]
    finally:
        await store.close()


async def test_private_context_is_filtered_by_audience_but_global_facts_remain(tmp_path) -> None:
    store = MessageStore(str(tmp_path / "audience.db"), str(tmp_path / "media"))
    await store.initialize()
    try:
        await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="qq:user:alice",
            actor_kind="human",
            event_type=EventType.INBOUND.value,
            content="Alice 的私密事实：她下个月要去东京。",
            pipeline_id="alice-turn",
        ))
        await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="bot:self",
            actor_kind="bot",
            event_type=EventType.OUTBOUND.value,
            content="我会记住这件事。",
            pipeline_id="alice-turn",
        ))
        await store.append_event(EventRecord(
            conversation_id="platform:global",
            actor_id="platform",
            actor_kind="platform",
            event_type=EventType.OUTBOUND.value,
            content="全局服务状态正常。",
            visibility="global",
        ))

        bob_stream = await build_life_stream(
            store,
            audience_conversation_id="private:bob",
        )
        bob_rendered = "\n".join(str(item.get("content", "")) for item in bob_stream)
        assert "东京" not in bob_rendered
        assert "全局服务状态正常" in bob_rendered

        alice_stream = await build_life_stream(
            store,
            audience_conversation_id="private:alice",
        )
        alice_rendered = "\n".join(str(item.get("content", "")) for item in alice_stream)
        assert "东京" in alice_rendered
    finally:
        await store.close()


async def test_identical_episode_coverage_is_idempotent(tmp_path) -> None:
    store = MessageStore(str(tmp_path / "context.db"), str(tmp_path / "media"))
    await store.initialize()
    try:
        inbound = await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="qq:user:alice",
            actor_kind="human",
            event_type=EventType.INBOUND.value,
            content="same range",
            pipeline_id="turn-1",
        ))
        outbound = await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="bot:self",
            actor_kind="bot",
            event_type=EventType.OUTBOUND.value,
            content="same result",
            pipeline_id="turn-1",
        ))
        first_id = await store.add_episode(EpisodeRecord(
            conversation_id="private:alice",
            first_sequence=inbound.sequence or 0,
            last_sequence=outbound.sequence or 0,
            narrative="first narrative",
        ))
        second_id = await store.add_episode(EpisodeRecord(
            conversation_id="private:alice",
            first_sequence=inbound.sequence or 0,
            last_sequence=outbound.sequence or 0,
            narrative="different duplicate narrative",
        ))

        assert second_id == first_id
        assert len(await store.get_episodes("private:alice")) == 1
    finally:
        await store.close()


async def test_incomplete_tool_round_is_not_replayed(tmp_path) -> None:
    store = MessageStore(str(tmp_path / "context.db"), str(tmp_path / "media"))
    await store.initialize()
    try:
        await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="bot:self",
            actor_kind="bot",
            event_type=EventType.TOOL_CALL.value,
            turn_id="turn-1:step:1",
            payload={"tool": "lookup", "call_id": "call-1", "arguments": {}},
        ))

        assert await build_life_stream(store) == []
    finally:
        await store.close()


async def test_ephemeral_file_skill_round_is_not_replayed(tmp_path) -> None:
    store = MessageStore(str(tmp_path / "ephemeral-skill.db"), str(tmp_path / "media"))
    await store.initialize()
    try:
        pipeline_id = "skill-turn"
        turn_id = "skill-turn:step:1"
        await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="qq:user:alice",
            actor_kind="human",
            event_type=EventType.INBOUND.value,
            content="use the paper skill",
            pipeline_id=pipeline_id,
            status="finalized",
        ))
        await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="bot:self",
            actor_kind="bot",
            event_type=EventType.TOOL_CALL.value,
            turn_id=turn_id,
            pipeline_id=pipeline_id,
            status="finalized",
            payload={
                "tool": "file_edit",
                "call_id": "call-read",
                "arguments": {"operation": "read", "path": "paper/SKILL.md"},
                "context_policy": "ephemeral",
            },
        ))
        await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="bot:self",
            actor_kind="bot",
            event_type=EventType.TOOL_RESULT.value,
            turn_id=turn_id,
            pipeline_id=pipeline_id,
            status="finalized",
            content="[file_edit result omitted from historical context]",
            payload={
                "tool": "file_edit",
                "call_id": "call-read",
                "result": "[file_edit result omitted from historical context]",
                "context_policy": "ephemeral",
            },
        ))
        await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="bot:self",
            actor_kind="bot",
            event_type=EventType.OUTBOUND.value,
            content="I will use the skill now.",
            pipeline_id=pipeline_id,
            status="finalized",
        ))

        stream = await build_life_stream(store, max_tokens=1000)
        rendered = "\n".join(str(item.get("content", "")) for item in stream)

        assert [item["role"] for item in stream] == ["user", "assistant"]
        assert "file_edit" not in rendered
        assert "SKILL.md" not in rendered
    finally:
        await store.close()


async def test_incomplete_pipeline_does_not_leak_its_tool_round(tmp_path) -> None:
    store = MessageStore(str(tmp_path / "context.db"), str(tmp_path / "media"))
    await store.initialize()
    try:
        await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="qq:user:alice",
            actor_kind="human",
            event_type=EventType.INBOUND.value,
            content="still running",
            pipeline_id="turn-1",
            status="received",
        ))
        await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="bot:self",
            actor_kind="bot",
            event_type=EventType.TOOL_CALL.value,
            turn_id="turn-1:step:1",
            pipeline_id="turn-1",
            payload={"tool": "lookup", "call_id": "call-1", "arguments": {}},
        ))
        await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="bot:self",
            actor_kind="bot",
            event_type=EventType.TOOL_RESULT.value,
            turn_id="turn-1:step:1",
            pipeline_id="turn-1",
            payload={"tool": "lookup", "call_id": "call-1", "result": "result"},
        ))

        assert await build_life_stream(store) == []
    finally:
        await store.close()


async def test_tool_only_current_pipeline_is_not_replayed_without_input(tmp_path) -> None:
    store = MessageStore(str(tmp_path / "tool-only-current.db"), str(tmp_path / "media"))
    await store.initialize()
    try:
        await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="bot:self",
            actor_kind="bot",
            event_type=EventType.TOOL_CALL.value,
            turn_id="current:step:1",
            pipeline_id="heartbeat:private:alice:1",
            payload={"tool": "sticker_search", "call_id": "call-1", "arguments": {}},
        ))
        await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="bot:self",
            actor_kind="bot",
            event_type=EventType.TOOL_RESULT.value,
            turn_id="current:step:1",
            pipeline_id="heartbeat:private:alice:1",
            payload={"tool": "sticker_search", "call_id": "call-1", "result": "[]"},
        ))

        assert await build_life_stream(store) == []
    finally:
        await store.close()


async def test_silent_pipeline_is_audit_only_and_does_not_enter_history(tmp_path) -> None:
    store = MessageStore(str(tmp_path / "silent.db"), str(tmp_path / "media"))
    await store.initialize()
    try:
        await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="qq:user:alice",
            actor_kind="human",
            event_type=EventType.INBOUND.value,
            content="silent input",
            pipeline_id="silent-turn",
            status="no_reply",
        ))
        await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="bot:self",
            actor_kind="bot",
            event_type=EventType.TOOL_CALL.value,
            turn_id="silent-turn:step:1",
            pipeline_id="silent-turn",
            payload={"tool": "memory_save", "call_id": "call-1", "arguments": {}},
        ))
        await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="bot:self",
            actor_kind="bot",
            event_type=EventType.TOOL_RESULT.value,
            turn_id="silent-turn:step:1",
            pipeline_id="silent-turn",
            payload={"tool": "memory_save", "call_id": "call-1", "result": "staged"},
        ))

        assert await build_life_stream(store) == []
    finally:
        await store.close()


async def test_bounded_query_loads_the_whole_pipeline_group(tmp_path) -> None:
    store = MessageStore(str(tmp_path / "context.db"), str(tmp_path / "media"))
    await store.initialize()
    try:
        await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="qq:user:alice",
            actor_kind="human",
            event_type=EventType.INBOUND.value,
            content="question",
            pipeline_id="turn-1",
        ))
        await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="bot:self",
            actor_kind="bot",
            event_type=EventType.TOOL_CALL.value,
            turn_id="turn-1:step:1",
            pipeline_id="turn-1",
            payload={"tool": "lookup", "call_id": "call-1", "arguments": {}},
        ))
        await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="bot:self",
            actor_kind="bot",
            event_type=EventType.TOOL_RESULT.value,
            turn_id="turn-1:step:1",
            pipeline_id="turn-1",
            payload={"tool": "lookup", "call_id": "call-1", "result": "result"},
        ))
        await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="bot:self",
            actor_kind="bot",
            event_type=EventType.OUTBOUND.value,
            content="answer",
            pipeline_id="turn-1",
        ))

        stream = await build_life_stream(store, limit=1)

        assert [item["role"] for item in stream] == ["user", "assistant", "tool", "assistant"]
    finally:
        await store.close()


async def test_bounded_query_loads_a_legacy_tool_round_by_turn_id(tmp_path) -> None:
    store = MessageStore(str(tmp_path / "context.db"), str(tmp_path / "media"))
    await store.initialize()
    try:
        await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="qq:user:alice",
            actor_kind="human",
            event_type=EventType.INBOUND.value,
            content="question",
        ))
        await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="bot:self",
            actor_kind="bot",
            event_type=EventType.TOOL_CALL.value,
            turn_id="legacy-turn",
            payload={"tool": "lookup", "call_id": "call-1", "arguments": {}},
        ))
        await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="bot:self",
            actor_kind="bot",
            event_type=EventType.TOOL_RESULT.value,
            turn_id="legacy-turn",
            payload={"tool": "lookup", "call_id": "call-1", "result": "result"},
        ))
        await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="bot:self",
            actor_kind="bot",
            event_type=EventType.OUTBOUND.value,
            content="answer",
        ))

        stream = await build_life_stream(store, limit=1)

        assert [item["role"] for item in stream] == ["user", "assistant", "tool", "assistant"]
    finally:
        await store.close()


async def test_budget_replaces_oversized_native_pipeline_group_atomically(tmp_path) -> None:
    store = MessageStore(str(tmp_path / "context.db"), str(tmp_path / "media"))
    await store.initialize()
    try:
        await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="qq:user:alice",
            actor_kind="human",
            event_type=EventType.INBOUND.value,
            content="question",
            pipeline_id="turn-1",
        ))
        await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="bot:self",
            actor_kind="bot",
            event_type=EventType.TOOL_CALL.value,
            turn_id="turn-1:step:1",
            pipeline_id="turn-1",
            payload={"tool": "lookup", "call_id": "call-1", "arguments": {}},
        ))
        await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="bot:self",
            actor_kind="bot",
            event_type=EventType.TOOL_RESULT.value,
            turn_id="turn-1:step:1",
            pipeline_id="turn-1",
            payload={"tool": "lookup", "call_id": "call-1", "result": "result"},
        ))
        await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="bot:self",
            actor_kind="bot",
            event_type=EventType.OUTBOUND.value,
            content="final answer",
            pipeline_id="turn-1",
        ))

        stream = await build_life_stream(store, max_tokens=1)
        roles = [item["role"] for item in stream]
        assert roles == ["user"]
        assert "historical_context_omitted" in stream[0]["content"]
        assert "turn-1" in stream[0]["content"]
    finally:
        await store.close()


async def test_budget_keeps_newest_contiguous_suffix(tmp_path) -> None:
    store = MessageStore(str(tmp_path / "context.db"), str(tmp_path / "media"))
    await store.initialize()
    try:
        for pipeline_id, user_text, answer in (
            ("old", "oldest context", "oldest answer"),
            ("middle", "m" * 1200, "middle answer"),
            ("new", "newest context", "newest answer"),
        ):
            await store.append_event(EventRecord(
                conversation_id="private:alice",
                actor_id="qq:user:alice",
                actor_kind="human",
                event_type=EventType.INBOUND.value,
                content=user_text,
                pipeline_id=pipeline_id,
            ))
            await store.append_event(EventRecord(
                conversation_id="private:alice",
                actor_id="bot:self",
                actor_kind="bot",
                event_type=EventType.OUTBOUND.value,
                content=answer,
                pipeline_id=pipeline_id,
            ))

        stream = await build_life_stream(store, max_tokens=20)
        rendered = "\n".join(str(item.get("content", "")) for item in stream)
        assert "newest context" in rendered
        assert "oldest context" not in rendered
        assert "middle answer" not in rendered
    finally:
        await store.close()


async def test_budget_orders_interleaved_pipeline_groups_by_latest_event(tmp_path) -> None:
    store = MessageStore(str(tmp_path / "interleaved-budget.db"), str(tmp_path / "media"))
    await store.initialize()
    try:
        for event in (
            EventRecord(
                conversation_id="private:alice",
                actor_id="qq:user:alice",
                actor_kind="human",
                event_type=EventType.INBOUND.value,
                content="Alice latest question",
                pipeline_id="pipeline-a",
            ),
            EventRecord(
                conversation_id="private:bob",
                actor_id="qq:user:bob",
                actor_kind="human",
                event_type=EventType.INBOUND.value,
                content="Bob older question",
                pipeline_id="pipeline-b",
            ),
            EventRecord(
                conversation_id="private:bob",
                actor_id="bot:self",
                actor_kind="bot",
                event_type=EventType.OUTBOUND.value,
                content="Bob older answer",
                pipeline_id="pipeline-b",
            ),
            EventRecord(
                conversation_id="private:alice",
                actor_id="bot:self",
                actor_kind="bot",
                event_type=EventType.OUTBOUND.value,
                content="Alice latest answer",
                pipeline_id="pipeline-a",
            ),
        ):
            await store.append_event(event)

        stream = await build_life_stream(store, max_tokens=50)
        rendered = "\n".join(str(item.get("content", "")) for item in stream)

        assert "Alice latest question" in rendered
        assert "Alice latest answer" in rendered
        assert "Bob older question" not in rendered
    finally:
        await store.close()


async def test_projection_marks_history_older_than_bounded_page(tmp_path) -> None:
    store = MessageStore(str(tmp_path / "bounded-page.db"), str(tmp_path / "media"))
    await store.initialize()
    try:
        for index in range(3):
            pipeline_id = f"pipeline-{index}"
            await store.append_event(EventRecord(
                conversation_id="private:alice",
                actor_id="qq:user:alice",
                actor_kind="human",
                event_type=EventType.INBOUND.value,
                content=f"question {index}",
                pipeline_id=pipeline_id,
            ))
            await store.append_event(EventRecord(
                conversation_id="private:alice",
                actor_id="bot:self",
                actor_kind="bot",
                event_type=EventType.OUTBOUND.value,
                content=f"answer {index}",
                pipeline_id=pipeline_id,
            ))

        stream = await build_life_stream(store, limit=2, max_tokens=1000)
        rendered = "\n".join(str(item.get("content", "")) for item in stream)

        assert "historical_context_omitted" in rendered
        assert "before_sequence" in rendered
    finally:
        await store.close()


async def test_projection_uses_one_ledger_high_water_mark(tmp_path) -> None:
    store = MessageStore(str(tmp_path / "snapshot.db"), str(tmp_path / "media"))
    await store.initialize()
    try:
        first = await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="qq:user:alice",
            actor_kind="human",
            event_type=EventType.INBOUND.value,
            content="first question",
            pipeline_id="turn-first",
        ))
        await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="bot:self",
            actor_kind="bot",
            event_type=EventType.OUTBOUND.value,
            content="first answer",
            pipeline_id="turn-first",
        ))
        snapshot = await store.get_event_high_water_sequence()
        await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="qq:user:alice",
            actor_kind="human",
            event_type=EventType.INBOUND.value,
            content="future question",
            pipeline_id="turn-future",
        ))
        await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="bot:self",
            actor_kind="bot",
            event_type=EventType.OUTBOUND.value,
            content="future answer",
            pipeline_id="turn-future",
        ))

        stream = await build_life_stream(store, snapshot_sequence=snapshot)
        rendered = "\n".join(str(item.get("content", "")) for item in stream)
        assert "first question" in rendered
        assert "future question" not in rendered
        assert first.sequence is not None
    finally:
        await store.close()


async def test_episode_that_cuts_a_tool_round_cannot_hide_raw_events(tmp_path) -> None:
    store = MessageStore(str(tmp_path / "cut-episode.db"), str(tmp_path / "media"))
    await store.initialize()
    try:
        inbound = await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="qq:user:alice",
            actor_kind="human",
            event_type=EventType.INBOUND.value,
            content="question",
            pipeline_id="turn-cut",
        ))
        tool_call = await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="bot:self",
            actor_kind="bot",
            event_type=EventType.TOOL_CALL.value,
            turn_id="turn-cut:step:1",
            pipeline_id="turn-cut",
            payload={"tool": "lookup", "call_id": "call-1", "arguments": {}},
        ))
        await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="bot:self",
            actor_kind="bot",
            event_type=EventType.TOOL_RESULT.value,
            turn_id="turn-cut:step:1",
            pipeline_id="turn-cut",
            payload={"tool": "lookup", "call_id": "call-1", "result": "result"},
        ))
        outbound = await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="bot:self",
            actor_kind="bot",
            event_type=EventType.OUTBOUND.value,
            content="answer",
            pipeline_id="turn-cut",
        ))
        await store.add_episode(EpisodeRecord(
            conversation_id="private:alice",
            first_sequence=inbound.sequence or 0,
            last_sequence=tool_call.sequence or 0,
            narrative="invalid partial episode",
        ))

        stream = await build_life_stream(store)
        rendered = "\n".join(str(item.get("content", "")) for item in stream)
        assert "invalid partial episode" not in rendered
        assert "question" in rendered
        assert "answer" in rendered
        assert outbound.sequence is not None
    finally:
        await store.close()


async def test_invalid_episode_cannot_advance_archive_frontier(tmp_path) -> None:
    store = MessageStore(str(tmp_path / "frontier.db"), str(tmp_path / "media"))
    await store.initialize()
    try:
        inbound = await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="qq:user:alice",
            actor_kind="human",
            event_type=EventType.INBOUND.value,
            content="question whose tool round is unfinished",
            pipeline_id="turn-frontier",
        ))
        tool_call = await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="bot:self",
            actor_kind="bot",
            event_type=EventType.TOOL_CALL.value,
            turn_id="turn-frontier:step:1",
            pipeline_id="turn-frontier",
            payload={"tool": "lookup", "call_id": "call-1", "arguments": {}},
        ))
        await store.add_episode(EpisodeRecord(
            conversation_id="private:alice",
            first_sequence=inbound.sequence or 0,
            last_sequence=tool_call.sequence or 0,
            narrative="invalid frontier episode",
        ))

        assert await store.get_latest_episode_sequence("private:alice") == 0
    finally:
        await store.close()


async def test_episode_summary_splits_large_source_without_truncation(monkeypatch, tmp_path) -> None:
    store = MessageStore(str(tmp_path / "context.db"), str(tmp_path / "media"))
    await store.initialize()
    calls: list[str] = []

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"choices": [{"message": {"content": "faithful summary"}}]}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def post(self, url, **kwargs):
            calls.append(kwargs["json"]["messages"][1]["content"])
            return Response()

    monkeypatch.setattr(episodes_module.httpx, "AsyncClient", lambda **_: Client())
    config = Config()
    config.summarizer.api_key = "test-key"
    config.summarizer.max_input_tokens = 1
    try:
        await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="qq:user:alice",
            actor_kind="human",
            event_type=EventType.INBOUND.value,
            content="x" * 3000,
            pipeline_id="turn-large",
        ))
        await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="bot:self",
            actor_kind="bot",
            event_type=EventType.OUTBOUND.value,
            content="answer",
            pipeline_id="turn-large",
        ))

        assert await summarize_pending_episode(store, config, "private:alice") is not None
        assert len(calls) > 1
    finally:
        await store.close()


async def test_terminal_cancelled_pipeline_does_not_block_later_episode(monkeypatch, tmp_path) -> None:
    store = MessageStore(str(tmp_path / "context.db"), str(tmp_path / "media"))
    await store.initialize()

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"choices": [{"message": {"content": "A later completed exchange."}}]}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def post(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr(episodes_module.httpx, "AsyncClient", lambda **_: Client())
    config = Config()
    config.summarizer.api_key = "test-key"
    try:
        await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="qq:user:alice",
            actor_kind="human",
            event_type=EventType.INBOUND.value,
            content="cancelled input",
            pipeline_id="turn-cancelled",
            status="cancelled",
        ))
        later_inbound = await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="qq:user:alice",
            actor_kind="human",
            event_type=EventType.INBOUND.value,
            content="later question",
            pipeline_id="turn-complete",
        ))
        later_outbound = await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="bot:self",
            actor_kind="bot",
            event_type=EventType.OUTBOUND.value,
            content="later answer",
            pipeline_id="turn-complete",
        ))

        episode_id = await summarize_pending_episode(store, config, "private:alice")

        assert episode_id is not None
        episode = (await store.get_episodes("private:alice"))[0]
        assert episode.first_sequence == later_inbound.sequence
        assert episode.last_sequence == later_outbound.sequence
    finally:
        await store.close()


async def test_finalized_legacy_inbound_fragment_does_not_block_later_episode(monkeypatch, tmp_path) -> None:
    store = MessageStore(str(tmp_path / "legacy-gap.db"), str(tmp_path / "media"))
    await store.initialize()

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"choices": [{"message": {"content": "later exchange summary"}}]}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def post(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr(episodes_module.httpx, "AsyncClient", lambda **_: Client())
    config = Config()
    config.summarizer.api_key = "test-key"
    try:
        await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="qq:user:alice",
            actor_kind="human",
            event_type=EventType.INBOUND.value,
            content="legacy input without a reply",
        ))
        later_inbound = await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="qq:user:alice",
            actor_kind="human",
            event_type=EventType.INBOUND.value,
            content="later question",
            pipeline_id="turn-later",
        ))
        later_outbound = await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="bot:self",
            actor_kind="bot",
            event_type=EventType.OUTBOUND.value,
            content="later answer",
            pipeline_id="turn-later",
        ))

        episode_id = await summarize_pending_episode(store, config, "private:alice")

        assert episode_id is not None
        episode = (await store.get_episodes("private:alice"))[0]
        assert episode.first_sequence == later_inbound.sequence
        assert episode.last_sequence == later_outbound.sequence
    finally:
        await store.close()


async def test_cancelled_pipeline_with_finished_tool_does_not_block_later_episode(monkeypatch, tmp_path) -> None:
    store = MessageStore(str(tmp_path / "context.db"), str(tmp_path / "media"))
    await store.initialize()

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"choices": [{"message": {"content": "later summary"}}]}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def post(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr(episodes_module.httpx, "AsyncClient", lambda **_: Client())
    config = Config()
    config.summarizer.api_key = "test-key"
    try:
        await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="qq:user:alice",
            actor_kind="human",
            event_type=EventType.INBOUND.value,
            content="cancelled input",
            pipeline_id="turn-cancelled",
            status="cancelled",
        ))
        await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="bot:self",
            actor_kind="bot",
            event_type=EventType.TOOL_CALL.value,
            content="",
            pipeline_id="turn-cancelled",
            turn_id="turn-cancelled:step:1",
            payload={"tool": "lookup", "call_id": "call-1", "arguments": {}},
        ))
        await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="bot:self",
            actor_kind="bot",
            event_type=EventType.TOOL_RESULT.value,
            content="result",
            pipeline_id="turn-cancelled",
            turn_id="turn-cancelled:step:1",
            payload={"tool": "lookup", "call_id": "call-1", "result": "result"},
        ))
        later_inbound = await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="qq:user:alice",
            actor_kind="human",
            event_type=EventType.INBOUND.value,
            content="later question",
            pipeline_id="turn-later",
        ))
        later_outbound = await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="bot:self",
            actor_kind="bot",
            event_type=EventType.OUTBOUND.value,
            content="later answer",
            pipeline_id="turn-later",
        ))

        episode_id = await summarize_pending_episode(store, config, "private:alice")

        assert episode_id is not None
        episode = (await store.get_episodes("private:alice"))[0]
        assert episode.first_sequence == later_inbound.sequence
        assert episode.last_sequence == later_outbound.sequence
    finally:
        await store.close()


async def test_verified_outbound_survives_cancelled_pipeline_projection(tmp_path) -> None:
    store = MessageStore(str(tmp_path / "context.db"), str(tmp_path / "media"))
    await store.initialize()
    try:
        await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="qq:user:alice",
            actor_kind="human",
            event_type=EventType.INBOUND.value,
            content="cancelled after send",
            pipeline_id="turn-cancelled",
            status="cancelled",
        ))
        await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="bot:self",
            actor_kind="bot",
            event_type=EventType.OUTBOUND.value,
            content="verified sent output",
            pipeline_id="turn-cancelled",
            status="finalized",
        ))

        stream = await build_life_stream(store)

        assert len(stream) == 1
        assert "verified sent output" in stream[0]["content"]
    finally:
        await store.close()


async def test_overlapping_episodes_do_not_hide_uncovered_raw_events(tmp_path) -> None:
    store = MessageStore(str(tmp_path / "context.db"), str(tmp_path / "media"))
    await store.initialize()
    try:
        first = await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="qq:user:alice",
            actor_kind="human",
            event_type=EventType.INBOUND.value,
            content="first",
            pipeline_id="turn-1",
        ))
        second = await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="bot:self",
            actor_kind="bot",
            event_type=EventType.OUTBOUND.value,
            content="second",
            pipeline_id="turn-1",
        ))
        third = await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="qq:user:alice",
            actor_kind="human",
            event_type=EventType.INBOUND.value,
            content="third raw",
            pipeline_id="turn-2",
        ))
        await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="bot:self",
            actor_kind="bot",
            event_type=EventType.OUTBOUND.value,
            content="fourth",
            pipeline_id="turn-2",
        ))
        await store.add_episode(EpisodeRecord(
            conversation_id="private:alice",
            first_sequence=first.sequence or 0,
            last_sequence=second.sequence or 0,
            narrative="first episode",
        ))
        await store.add_episode(EpisodeRecord(
            conversation_id="private:alice",
            first_sequence=second.sequence or 0,
            last_sequence=third.sequence or 0,
            narrative="overlapping episode",
        ))

        stream = await build_life_stream(store)
        rendered = "\n".join(str(item["content"]) for item in stream)

        assert "first episode" in rendered
        assert "overlapping episode" not in rendered
        assert "third raw" in rendered
    finally:
        await store.close()


async def test_message_summary_cannot_advance_compaction_boundary(tmp_path) -> None:
    store = MessageStore(str(tmp_path / "context.db"), str(tmp_path / "media"))
    await store.initialize()
    try:
        await store.add_summary(
            "private:alice",
            "user",
            "descriptive summary",
            kind="message",
            covered_through_message_id=99,
        )

        summaries = await store.get_summaries("private:alice")

        assert summaries[0]["covered_through_message_id"] is None
        assert await store.get_newest_compaction_summary("private:alice") is None
    finally:
        await store.close()


async def test_episode_frontier_does_not_jump_over_an_uncovered_gap(tmp_path) -> None:
    store = MessageStore(str(tmp_path / "context.db"), str(tmp_path / "media"))
    await store.initialize()
    try:
        first = await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="qq:user:alice",
            actor_kind="human",
            event_type=EventType.INBOUND.value,
            content="covered question",
            pipeline_id="turn-covered",
        ))
        second = await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="bot:self",
            actor_kind="bot",
            event_type=EventType.OUTBOUND.value,
            content="covered answer",
            pipeline_id="turn-covered",
        ))
        await store.add_episode(EpisodeRecord(
            conversation_id="private:alice",
            first_sequence=first.sequence or 0,
            last_sequence=second.sequence or 0,
            narrative="covered exchange",
        ))

        gap = await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="qq:user:alice",
            actor_kind="human",
            event_type=EventType.INBOUND.value,
            content="uncovered question",
            pipeline_id="turn-gap",
        ))
        await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="bot:self",
            actor_kind="bot",
            event_type=EventType.OUTBOUND.value,
            content="uncovered answer",
            pipeline_id="turn-gap",
        ))
        later_first = await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="qq:user:alice",
            actor_kind="human",
            event_type=EventType.INBOUND.value,
            content="later question",
            pipeline_id="turn-later",
        ))
        later_last = await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="bot:self",
            actor_kind="bot",
            event_type=EventType.OUTBOUND.value,
            content="later answer",
            pipeline_id="turn-later",
        ))
        await store.add_episode(EpisodeRecord(
            conversation_id="private:alice",
            first_sequence=later_first.sequence or 0,
            last_sequence=later_last.sequence or 0,
            narrative="later exchange",
        ))

        assert await store.get_latest_episode_sequence("private:alice") == second.sequence

        stream = await build_life_stream(store, limit=1)
        rendered = "\n".join(str(item.get("content", "")) for item in stream)
        assert "uncovered question" in rendered
        assert "uncovered answer" in rendered
        assert "later exchange" in rendered
        assert gap.sequence is not None
    finally:
        await store.close()


async def test_coverage_only_event_does_not_block_episode_frontier(tmp_path) -> None:
    store = MessageStore(str(tmp_path / "coverage-only.db"), str(tmp_path / "media"))
    await store.initialize()
    try:
        first = await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="qq:user:alice",
            actor_kind="human",
            event_type=EventType.INBOUND.value,
            content="first",
            pipeline_id="turn-first",
        ))
        last = await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="bot:self",
            actor_kind="bot",
            event_type=EventType.OUTBOUND.value,
            content="answer",
            pipeline_id="turn-first",
        ))
        await store.add_episode(EpisodeRecord(
            conversation_id="private:alice",
            first_sequence=first.sequence or 0,
            last_sequence=last.sequence or 0,
            narrative="first episode",
        ))
        marker = await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="platform",
            actor_kind="platform",
            event_type=EventType.STATUS_UPDATE.value,
            content="operational marker",
        ))
        later_first = await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="qq:user:alice",
            actor_kind="human",
            event_type=EventType.INBOUND.value,
            content="later",
            pipeline_id="turn-later",
        ))
        later_last = await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="bot:self",
            actor_kind="bot",
            event_type=EventType.OUTBOUND.value,
            content="later answer",
            pipeline_id="turn-later",
        ))
        await store.add_episode(EpisodeRecord(
            conversation_id="private:alice",
            first_sequence=later_first.sequence or 0,
            last_sequence=later_last.sequence or 0,
            narrative="later episode",
        ))

        assert await store.get_latest_episode_sequence("private:alice") == later_last.sequence
        assert marker.sequence is not None
    finally:
        await store.close()


async def test_terminal_fragment_does_not_block_a_later_valid_episode(tmp_path) -> None:
    store = MessageStore(str(tmp_path / "terminal-gap.db"), str(tmp_path / "media"))
    await store.initialize()
    try:
        fragment = await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="qq:user:alice",
            actor_kind="human",
            event_type=EventType.INBOUND.value,
            content="orphaned finalized input",
            pipeline_id="orphaned-turn",
            status="finalized",
        ))
        first = await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="qq:user:alice",
            actor_kind="human",
            event_type=EventType.INBOUND.value,
            content="later question",
            pipeline_id="later-turn",
        ))
        last = await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="bot:self",
            actor_kind="bot",
            event_type=EventType.OUTBOUND.value,
            content="later answer",
            pipeline_id="later-turn",
        ))
        await store.add_episode(EpisodeRecord(
            conversation_id="private:alice",
            first_sequence=first.sequence or 0,
            last_sequence=last.sequence or 0,
            narrative="later episode",
        ))

        assert await store.get_latest_episode_sequence("private:alice") == last.sequence
        stream = await build_life_stream(store)
        rendered = "\n".join(str(item.get("content", "")) for item in stream)
        assert "later episode" in rendered
        assert "orphaned finalized input" not in rendered
        assert fragment.sequence is not None
    finally:
        await store.close()


async def test_invalid_overlapping_episode_cannot_hide_valid_episode(tmp_path) -> None:
    store = MessageStore(str(tmp_path / "invalid-overlap.db"), str(tmp_path / "media"))
    await store.initialize()
    try:
        bad_first = await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="qq:user:alice",
            actor_kind="human",
            event_type=EventType.INBOUND.value,
            content="bad orphan",
            pipeline_id="bad-turn",
        ))
        good_first = await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="qq:user:alice",
            actor_kind="human",
            event_type=EventType.INBOUND.value,
            content="good question",
            pipeline_id="good-turn",
        ))
        good_last = await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="bot:self",
            actor_kind="bot",
            event_type=EventType.OUTBOUND.value,
            content="good answer",
            pipeline_id="good-turn",
        ))
        assert store._conn is not None
        await store._conn.execute(
            "INSERT INTO episodes "
            "(episode_id, conversation_id, first_sequence, last_sequence, narrative, status) "
            "VALUES (?, ?, ?, ?, ?, 'ready')",
            (
                "invalid-overlap",
                "private:alice",
                bad_first.sequence,
                good_last.sequence,
                "invalid episode",
            ),
        )
        await store._conn.execute(
            "INSERT INTO episodes "
            "(episode_id, conversation_id, first_sequence, last_sequence, narrative, status) "
            "VALUES (?, ?, ?, ?, ?, 'ready')",
            (
                "valid-overlap",
                "private:alice",
                good_first.sequence,
                good_last.sequence,
                "valid episode",
            ),
        )
        await store._conn.commit()

        stream = await build_life_stream(store)
        rendered = "\n".join(str(item.get("content", "")) for item in stream)
        assert "valid episode" in rendered
        assert "invalid episode" not in rendered
    finally:
        await store.close()


async def test_orphaned_legacy_inbound_is_not_projected(tmp_path) -> None:
    store = MessageStore(str(tmp_path / "legacy-orphan.db"), str(tmp_path / "media"))
    await store.initialize()
    try:
        await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="qq:user:alice",
            actor_kind="human",
            event_type=EventType.INBOUND.value,
            content="orphan legacy input",
            status="finalized",
        ))

        assert await build_life_stream(store) == []
    finally:
        await store.close()


async def test_episode_reclaim_stops_before_a_later_episode(monkeypatch, tmp_path) -> None:
    store = MessageStore(str(tmp_path / "context.db"), str(tmp_path / "media"))
    await store.initialize()

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"choices": [{"message": {"content": "gap summary"}}]}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args) -> None:
            return None

        async def post(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr(episodes_module.httpx, "AsyncClient", lambda **_: Client())
    config = Config()
    config.summarizer.api_key = "test-key"
    try:
        first = await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="qq:user:alice",
            actor_kind="human",
            event_type=EventType.INBOUND.value,
            content="old question",
            pipeline_id="turn-old",
        ))
        second = await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="bot:self",
            actor_kind="bot",
            event_type=EventType.OUTBOUND.value,
            content="old answer",
            pipeline_id="turn-old",
        ))
        await store.add_episode(EpisodeRecord(
            conversation_id="private:alice",
            first_sequence=first.sequence or 0,
            last_sequence=second.sequence or 0,
            narrative="old episode",
        ))

        gap_first = await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="qq:user:alice",
            actor_kind="human",
            event_type=EventType.INBOUND.value,
            content="gap question",
            pipeline_id="turn-gap",
        ))
        gap_last = await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="bot:self",
            actor_kind="bot",
            event_type=EventType.OUTBOUND.value,
            content="gap answer",
            pipeline_id="turn-gap",
        ))
        later_first = await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="qq:user:alice",
            actor_kind="human",
            event_type=EventType.INBOUND.value,
            content="later question",
            pipeline_id="turn-later",
        ))
        later_last = await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="bot:self",
            actor_kind="bot",
            event_type=EventType.OUTBOUND.value,
            content="later answer",
            pipeline_id="turn-later",
        ))
        await store.add_episode(EpisodeRecord(
            conversation_id="private:alice",
            first_sequence=later_first.sequence or 0,
            last_sequence=later_last.sequence or 0,
            narrative="later episode",
        ))

        episode_id = await summarize_pending_episode(store, config, "private:alice")

        assert episode_id is not None
        episodes = await store.get_episodes("private:alice")
        assert any(
            episode.first_sequence == gap_first.sequence
            and episode.last_sequence == gap_last.sequence
            for episode in episodes
        )
    finally:
        await store.close()


async def test_startup_cleanup_closes_orphaned_user_pipeline_but_keeps_service_event(tmp_path) -> None:
    store = MessageStore(str(tmp_path / "orphaned.db"), str(tmp_path / "media"))
    await store.initialize()
    try:
        user_event = await store.append_event(EventRecord(
            conversation_id="private:alice",
            actor_id="qq:user:alice",
            actor_kind="human",
            event_type=EventType.INBOUND.value,
            content="interrupted before restart",
            pipeline_id="orphan-user",
            status="received",
        ))
        service_event = await store.append_event(EventRecord(
            conversation_id="service:monitor",
            actor_id="service:monitor",
            actor_kind="service",
            event_type=EventType.INBOUND.value,
            content="must be restored by ingress",
            pipeline_id="service-report",
            status="received",
        ))

        assert await store.cancel_orphaned_pipelines() == 1
        user_after = await store.get_event(user_event.event_id or "")
        service_after = await store.get_event(service_event.event_id or "")
        assert user_after is not None and user_after.status == "cancelled"
        assert service_after is not None and service_after.status == "received"
    finally:
        await store.close()
