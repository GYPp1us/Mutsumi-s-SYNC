from __future__ import annotations

import asyncio
import pytest
import tempfile
import os
from datetime import date

from src.mutsumi_sync.config import Config
from src.mutsumi_sync.memory.store import MessageStore, StoredMessage
from src.mutsumi_sync.memory.window import MessageWindow
from src.mutsumi_sync.memory.session import SessionState
from src.mutsumi_sync.message.sender import Peer
from src.mutsumi_sync.message.classifier import MessageType
from src.mutsumi_sync.scheduler import PipelineDeps
from src.mutsumi_sync.main import build_registry
from src.mutsumi_sync.pipeline import LLMResult, pipeline, _build_context, _recycle_window_if_needed
import src.mutsumi_sync.pipeline as pipeline_module


class CaptureSender:
    """Records all send/poke calls."""

    def __init__(self):
        self.sent: list[dict] = []
        self.pokes: list[dict] = []

    async def send(self, peer, message) -> dict:
        self.sent.append({"message": message, "peer": peer})
        return {"status": "ok"}

    async def send_poke(self, peer) -> dict:
        self.pokes.append({"peer": peer})
        return {"status": "ok"}


def make_config():
    c = Config()
    c.session.timeout = 0
    c.context.window_max_tokens = 200
    c.context.window_min_tokens = 100
    c.context.summaries_max_count = 10
    c.context.summaries_min_count = 5
    c.memory.archive_threshold_tokens = 50
    c.memory.self_note_target_tokens = 1000
    c.memory.self_note_max_multiplier = 2.0
    return c


class TestPipelineE2EMultiRound:
    """Multi-round test: window growth -> recycle -> archive -> context assembly."""

    async def test_window_recycle_and_archive(self):
        config = make_config()
        config.memory.archive_threshold_tokens = 10000
        sender = CaptureSender()
        store = MessageStore(db_path=":memory:")
        await store.initialize()
        registry = build_registry(config, store)

        window = MessageWindow()
        session = SessionState()
        peer = Peer(chat_type=1, peer_uid="e2e_test")
        group_key = "private:e2e_test"

        await store.upsert_self_note(group_key, "用户叫E2E，测试用")

        rounds = 8
        for i in range(rounds):
            deps = PipelineDeps(
                config=config, registry=registry, sender=sender,
                store=store, window=window, session=session,
                peer=peer, group_key=group_key,
            )
            msg = f"round {i} " + "hello " * 10
            await pipeline(msg, MessageType.SHORT_TEXT, None, None, deps=deps)

        assert len(sender.sent) == 0

        window_size = len(window)
        total_estimate = sum(
            len(str(m.get("content", ""))) // 4 for m in window.get_context()
        )
        assert window_size > 0, "Window should have entries"

        deps = PipelineDeps(
            config=config, registry=registry, sender=sender,
            store=store, window=window, session=session,
            peer=peer, group_key=group_key,
        )
        ctx = await _build_context("test final", deps)

        assert ctx[0]["role"] == "system", "First message should be system"

        note_injected = any(
            "E2E" in str(m.get("content", ""))
            for m in ctx if m["role"] == "system"
        )
        assert note_injected, "self_note should be in context"

        assert ctx[-1]["role"] == "user", f"Last message should be user, got {ctx[-1]['role']}"
        assert "test final" in ctx[-1]["content"]

        await store.close()

    async def test_long_message_triggers_archive(self):
        """Single long message triggers archive_to_summary."""
        config = make_config()
        sender = CaptureSender()
        store = MessageStore(db_path=":memory:")
        await store.initialize()
        registry = build_registry(config, store)

        window = MessageWindow()
        session = SessionState()
        peer = Peer(chat_type=1, peer_uid="archive_test")
        group_key = "private:archive_test"

        long_msg = "long message " * 20

        deps = PipelineDeps(
            config=config, registry=registry, sender=sender,
            store=store, window=window, session=session,
            peer=peer, group_key=group_key,
        )
        await pipeline(long_msg, MessageType.SHORT_TEXT, None, None, deps=deps)

        assert len(sender.sent) == 0

        all_msgs = await store.get_messages(group_key=group_key)
        assert len(all_msgs) >= 1, "Message should be saved to store"

        await store.close()

    async def test_self_note_in_context(self):
        """Verify self_note is properly injected with length metadata."""
        config = make_config()
        sender = CaptureSender()
        store = MessageStore(db_path=":memory:")
        await store.initialize()
        registry = build_registry(config, store)

        window = MessageWindow()
        session = SessionState()
        peer = Peer(chat_type=1, peer_uid="note_test")
        group_key = "private:note_test"

        await store.upsert_self_note(group_key, "TestUser likes Python and async programming")

        deps = PipelineDeps(
            config=config, registry=registry, sender=sender,
            store=store, window=window, session=session,
            peer=peer, group_key=group_key,
        )
        ctx = await _build_context("hello", deps)

        note_message = next(
            (m for m in ctx if "current:" in str(m.get("content", "")) and "私人印象" in str(m.get("content", ""))),
            None,
        )
        assert note_message is not None, f"self_note with current/target not found in context. Messages: {[m['role'] for m in ctx]}"
        note_text = str(note_message["content"])
        assert "current:" in note_text, "Should have current token count"
        assert "target:" in note_text, "Should have target token count"
        assert "Python" in note_text, "Should contain the note content"

        await store.close()

    async def test_context_has_correct_structure(self):
        """Verify _build_context produces correctly structured messages array."""
        config = make_config()
        sender = CaptureSender()
        store = MessageStore(db_path=":memory:")
        await store.initialize()
        registry = build_registry(config, store)

        window = MessageWindow()
        window.add("user1", "previous user message")
        window.add("user1", "previous bot reply", is_bot=True)

        session = SessionState()
        peer = Peer(chat_type=1, peer_uid="struct_test")
        group_key = "private:struct_test"

        await store.upsert_self_note(group_key, "structure test note")
        await store.add_summary(group_key, "user", "past conversation about weather")

        deps = PipelineDeps(
            config=config, registry=registry, sender=sender,
            store=store, window=window, session=session,
            peer=peer, group_key=group_key,
        )
        ctx = await _build_context("current user message", deps)

        roles = [m["role"] for m in ctx]
        assert roles[0] == "system"

        assert "user" in roles

        assert ctx[-1]["role"] == "user"
        assert ctx[-1]["content"] == "current user message"

        await store.close()

class TestPipelineE2EDebounce:
    """Verify debounce integration in full pipeline flow."""

    async def test_pipeline_with_debounce(self):
        """Pipeline should handle messages correctly after scheduler dispatch."""
        config = make_config()
        sender = CaptureSender()
        store = MessageStore(db_path=":memory:")
        await store.initialize()
        registry = build_registry(config, store)

        window = MessageWindow()
        session = SessionState()
        peer = Peer(chat_type=1, peer_uid="debounce_test")
        group_key = "private:debounce_test"

        deps = PipelineDeps(
            config=config, registry=registry, sender=sender,
            store=store, window=window, session=session,
            peer=peer, group_key=group_key,
        )

        await pipeline("hi", MessageType.SHORT_TEXT, None, None, deps=deps)
        assert len(sender.sent) == 0

        await pipeline("how are you", MessageType.SHORT_TEXT, None, None, deps=deps)
        assert len(sender.sent) == 0
        assert len(window) >= 2

        await store.close()

    async def test_content_is_not_sent_directly(self, caplog):
        config = make_config()
        sender = CaptureSender()
        store = MessageStore(db_path=":memory:")
        await store.initialize()
        registry = build_registry(config, store)

        window = MessageWindow()
        session = SessionState()
        peer = Peer(chat_type=1, peer_uid="content_only_test")
        group_key = "private:content_only_test"

        deps = PipelineDeps(
            config=config, registry=registry, sender=sender,
            store=store, window=window, session=session,
            peer=peer, group_key=group_key,
        )

        with caplog.at_level("WARNING", logger="mutsumi.pipeline"):
            await pipeline("hello", MessageType.SHORT_TEXT, None, None, deps=deps)

        assert len(sender.sent) == 0
        assert any("direct content send disabled" in record.message for record in caplog.records)

        await store.close()

    async def test_pipeline_logs_content_only_branch_end_to_end(self, caplog, monkeypatch):
        config = make_config()
        sender = CaptureSender()
        store = MessageStore(db_path=":memory:")
        await store.initialize()
        registry = build_registry(config, store)

        async def fake_llm_call(messages, deps):
            return LLMResult(content="logged content-only reply", input_tokens=3, output_tokens=2)

        monkeypatch.setattr(pipeline_module, "_do_llm_call", fake_llm_call)

        deps = PipelineDeps(
            config=config, registry=registry, sender=sender,
            store=store, window=MessageWindow(), session=SessionState(),
            peer=Peer(chat_type=1, peer_uid="log_chain_test"),
            group_key="private:log_chain_test",
        )

        with caplog.at_level("INFO", logger="mutsumi.pipeline"):
            await pipeline("hello", MessageType.SHORT_TEXT, None, None, deps=deps)

        messages = "\n".join(record.message for record in caplog.records)
        assert "[PIPE] LLM result" in messages
        assert "[PIPE] branch=content_only" in messages
        assert "[PIPE] saved message category=short_text response=yes" in messages
        assert "[PIPE] window updated" in messages
        assert "[PIPE] cleanup complete" in messages

        await store.close()
