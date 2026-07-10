from __future__ import annotations

import asyncio
import json
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
from src.mutsumi_sync.pipeline import (
    LLMResult,
    pipeline,
    _build_context,
    _compact_context_for_request,
    _estimate_request_tokens,
    _generate_and_save_summary,
)
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

        assert len(sender.sent) == rounds

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

        assert ctx[0]["role"] == "system", "First message should be a provider-native system prompt"
        assert ctx[0]["content"], "System prompt should not be empty"

        note_injected = any(
            "E2E" in str(m.get("content", ""))
            for m in ctx if m["role"] == "user"
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

        assert len(sender.sent) == 1

        all_msgs = await store.get_messages(group_key=group_key)
        assert len(all_msgs) >= 1, "Message should be saved to store"

        await store.close()

    def test_request_estimate_includes_tool_schema(self):
        messages = [{"role": "system", "content": "rules"}]
        small = _estimate_request_tokens(messages, [])
        large = _estimate_request_tokens(messages, [{
            "type": "function",
            "function": {
                "name": "large_tool",
                "description": "x" * 1000,
                "parameters": {"type": "object", "properties": {}},
            },
        }])

        assert large > small + 200

    async def test_request_compaction_archives_complete_turns_with_exact_coverage(self, monkeypatch):
        config = make_config()
        config.context.model_context_tokens = 700
        config.context.reserved_output_tokens = 100
        config.context.compression_trigger_ratio = 0.8
        config.context.compression_target_ratio = 0.45
        sender = CaptureSender()
        store = MessageStore(db_path=":memory:")
        await store.initialize()
        registry = build_registry(config, store)
        window = MessageWindow()
        record_ids = []
        for index in range(3):
            record_id = await store.save(StoredMessage(
                date="2026-07-11",
                group_key="private:budget",
                category="short_text",
                content=json.dumps({
                    "user": f"question {index}",
                    "bot": f"answer {index}",
                    "status": "responded",
                }),
            ))
            record_ids.append(record_id)
            window.add("budget", f"question {index} " + "q" * 220, record_id=record_id)
            window.add("budget", f"answer {index} " + "a" * 220, is_bot=True, record_id=record_id)

        async def fake_summary(deps, source, text, summarizer_cfg, *, kind="message", covered_through_message_id=None):
            assert kind == "compaction"
            assert "q" * 100 in text
            await deps.store.add_summary(
                deps.group_key,
                source,
                "compacted older turns",
                kind=kind,
                covered_through_message_id=covered_through_message_id,
            )
            return True

        monkeypatch.setattr(pipeline_module, "_generate_and_save_summary", fake_summary)
        deps = PipelineDeps(
            config=config, registry=registry, sender=sender,
            store=store, window=window, session=SessionState(),
            peer=Peer(chat_type=1, peer_uid="budget"),
            group_key="private:budget",
        )
        messages = await _build_context("current request", deps)

        compacted = await _compact_context_for_request("current request", deps, messages)

        assert compacted is True
        remaining_ids = {item["record_id"] for item in window}
        assert record_ids[-1] in remaining_ids
        assert all(
            sum(1 for item in window if item["record_id"] == record_id) in (0, 2)
            for record_id in record_ids
        )
        boundary = await store.get_newest_compaction_summary("private:budget")
        assert boundary is not None
        removed_ids = [record_id for record_id in record_ids if record_id not in remaining_ids]
        assert boundary["covered_through_message_id"] == max(removed_ids)
        await store.close()

    async def test_message_summary_does_not_claim_coverage_or_truncate_input(self, monkeypatch):
        config = make_config()
        config.model.api_key = "test-key"
        config.summarizer.max_input_tokens = 10000
        store = MessageStore(db_path=":memory:")
        await store.initialize()
        captured_inputs = []

        class Response:
            status_code = 200

            @staticmethod
            def json():
                return {"choices": [{"message": {"content": "complete long-message summary"}}]}

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def post(self, url, *, headers, json):
                captured_inputs.append(json["messages"][-1]["content"])
                return Response()

        monkeypatch.setattr(pipeline_module.httpx, "AsyncClient", lambda timeout: Client())
        deps = PipelineDeps(
            config=config, registry=build_registry(config, store), sender=CaptureSender(),
            store=store, window=MessageWindow(), session=SessionState(),
            peer=Peer(chat_type=1, peer_uid="summary"),
            group_key="private:summary",
        )
        source_text = "完整内容" * 1500

        saved = await _generate_and_save_summary(
            deps, "user", source_text, config.summarizer, kind="message"
        )

        assert saved is True
        assert captured_inputs == [source_text]
        summaries = await store.get_summaries("private:summary")
        assert summaries[0]["kind"] == "message"
        assert summaries[0]["covered_through_message_id"] is None
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
        assert "很久之前" in note_text, "Existing self_note lines should get a fallback timestamp"

        await store.close()

    async def test_context_has_correct_structure(self):
        """Verify _build_context produces correctly structured messages array."""
        config = make_config()
        config.prompts.persona = "Speak as a calm long-term companion."
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
        assert ctx[0]["role"] == "system"
        assert ctx[0]["content"]
        assert roles.count("system") == 1

        bootstrap = ctx[1]
        assert bootstrap["role"] == "user"
        assert "Context Packet" in bootstrap["content"]
        assert "structure test note" in bootstrap["content"]
        assert "past conversation about weather" in bootstrap["content"]
        assert "+08:00" in bootstrap["content"]
        assert bootstrap["content"].rstrip().endswith(
            "[Persona]\nSpeak as a calm long-term companion.\n[/Persona]\n[/Context Packet]"
        )
        assert "provider tool schema is authoritative" in ctx[0]["content"].lower()
        assert "用未转义的 |" not in ctx[0]["content"]

        assert ctx[-2]["role"] == "user"
        assert "Runtime Injection" in ctx[-2]["content"]
        assert ctx[-1]["role"] == "user"
        assert ctx[-1]["content"] == "current user message"

        await store.close()

    async def test_visible_content_keeps_pipe_literal(self, monkeypatch):
        config = make_config()
        sender = CaptureSender()
        store = MessageStore(db_path=":memory:")
        await store.initialize()
        registry = build_registry(config, store)

        async def fake_llm_call(messages, deps):
            return LLMResult(content="a | b | c", input_tokens=3, output_tokens=3)

        monkeypatch.setattr(pipeline_module, "_do_llm_call", fake_llm_call)
        deps = PipelineDeps(
            config=config, registry=registry, sender=sender,
            store=store, window=MessageWindow(), session=SessionState(),
            peer=Peer(chat_type=1, peer_uid="literal_pipe"),
            group_key="private:literal_pipe",
        )

        await pipeline("show a pipe", MessageType.SHORT_TEXT, None, None, deps=deps)

        assert [item["message"] for item in sender.sent] == ["a | b | c"]
        saved = await store.get_messages(group_key="private:literal_pipe")
        assert json.loads(saved[0].content)["bot"] == "a | b | c"
        await store.close()

    async def test_priority_override_is_in_runtime_injection_only(self):
        config = make_config()
        sender = CaptureSender()
        store = MessageStore(db_path=":memory:")
        await store.initialize()
        registry = build_registry(config, store)

        window = MessageWindow()
        window.add("user1", "previous user message", created_at=1780000000)
        window.add("user1", "previous bot reply", is_bot=True, created_at=1780000001)

        group_key = "private:priority_context_test"
        await store.upsert_priority_override(group_key, "Always preserve exact equations.")

        deps = PipelineDeps(
            config=config, registry=registry, sender=sender,
            store=store, window=window, session=SessionState(),
            peer=Peer(chat_type=1, peer_uid="priority_context_test"),
            group_key=group_key,
        )

        ctx = await _build_context("current user message", deps)
        user_messages = [m for m in ctx if m["role"] == "user"]

        assert len(user_messages) == 4
        joined = "\n".join(str(m["content"]) for m in user_messages)
        assert joined.count("[Priority Override]") == 1
        assert joined.count("Always preserve exact equations.") == 1
        assert "Runtime Injection" in ctx[-2]["content"]
        assert "Always preserve exact equations." in ctx[-2]["content"]
        assert "Priority Override" not in ctx[-1]["content"]
        assert ctx[-1]["content"] == "current user message"
        assert "+08:00" in ctx[2]["content"], "Window messages should include readable +8 timestamps"

        await store.close()

    async def test_cancelled_pipeline_keeps_inbound_message(self, monkeypatch):
        config = make_config()
        sender = CaptureSender()
        store = MessageStore(db_path=":memory:")
        await store.initialize()
        registry = build_registry(config, store)

        async def never_finishes(messages, deps):
            await asyncio.sleep(60)
            return LLMResult(content="late")

        monkeypatch.setattr(pipeline_module, "_do_llm_call", never_finishes)

        deps = PipelineDeps(
            config=config, registry=registry, sender=sender,
            store=store, window=MessageWindow(), session=SessionState(),
            peer=Peer(chat_type=1, peer_uid="cancel_persist_test"),
            group_key="private:cancel_persist_test",
        )

        task = asyncio.create_task(pipeline("do not lose me", MessageType.SHORT_TEXT, None, None, deps=deps))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        saved = await store.get_messages(group_key="private:cancel_persist_test")
        assert len(saved) == 1
        content = json.loads(saved[0].content)
        assert content["user"] == "do not lose me"
        assert content["status"] == "cancelled"

        await store.close()

    async def test_markdown_image_send_is_recorded_in_action_ledger(self, monkeypatch):
        config = make_config()
        sender = CaptureSender()
        store = MessageStore(db_path=":memory:")
        await store.initialize()
        registry = build_registry(config, store)

        calls = iter([
            LLMResult(
                content="",
                tool_calls=[{
                    "id": "call_markdown",
                    "name": "send",
                    "arguments": {"markdown_image": "# Report\n\n$E=mc^2$"},
                }],
            ),
            LLMResult(content=""),
        ])

        async def fake_llm_call(messages, deps):
            return next(calls)

        async def fake_send_tool(args, *, sender, peer, config=None):
            await sender.send(peer, [{"type": "image", "data": {"file": "rendered.png"}}])
            return json.dumps({
                "status": "ok",
                "data": {"message_id": 123},
                "artifacts": [{
                    "kind": "sent_image",
                    "source": "markdown_image",
                    "file": "rendered.png",
                    "markdown": args["markdown_image"],
                }],
            })

        monkeypatch.setattr(pipeline_module, "_do_llm_call", fake_llm_call)
        monkeypatch.setattr(pipeline_module, "send_tool", fake_send_tool)

        deps = PipelineDeps(
            config=config, registry=registry, sender=sender,
            store=store, window=MessageWindow(), session=SessionState(),
            peer=Peer(chat_type=1, peer_uid="artifact_test"),
            group_key="private:artifact_test",
        )

        await pipeline("render it", MessageType.SHORT_TEXT, None, None, deps=deps)

        image_records = await store.get_messages(group_key="private:artifact_test", category="image")
        assert image_records == []
        actions = await store.get_recent_actions("private:artifact_test")
        send_action = next(action for action in actions if action["tool_name"] == "send")
        assert send_action["success"] is True
        assert send_action["artifact"]["source"] == "markdown_image"
        assert send_action["artifact"]["message_id"] == 123
        assert send_action["artifact"]["markdown_sha256"]
        assert all("sent image" not in item["content"] for item in deps.window.get_context())

        await store.close()

    async def test_incoming_image_uses_vision_provider_when_enabled(self, monkeypatch):
        config = make_config()
        config.vision.enabled = True
        config.vision.api_key = "sk-test"
        config.vision.base_url = "https://vision.example/v1"
        config.vision.model = "vision-model"
        sender = CaptureSender()
        store = MessageStore(db_path=":memory:")
        await store.initialize()
        registry = build_registry(config, store)

        async def fake_describe_image(*, image_file, image_url, config):
            return "Image contains a commutative diagram."

        captured = []

        async def fake_llm_call(messages, deps):
            captured.append(messages)
            return LLMResult(content="I can help with that diagram.", input_tokens=20, output_tokens=5)

        monkeypatch.setattr(pipeline_module, "describe_image", fake_describe_image)
        monkeypatch.setattr(pipeline_module, "_do_llm_call", fake_llm_call)

        deps = PipelineDeps(
            config=config, registry=registry, sender=sender,
            store=store, window=MessageWindow(), session=SessionState(),
            peer=Peer(chat_type=1, peer_uid="image_vision_test"),
            group_key="private:image_vision_test",
        )

        await pipeline("please explain this", MessageType.IMAGE, None, "https://example.com/diagram.png", deps=deps)

        assert sender.sent[-1]["message"] == "I can help with that diagram."
        assert "please explain this" in captured[0][-1]["content"]
        assert "commutative diagram" in captured[0][-1]["content"]
        saved = await store.get_messages(group_key="private:image_vision_test", category="image")
        assert len(saved) == 1
        payload = json.loads(saved[0].content)
        assert payload["input_metadata"]["image_description"] == "Image contains a commutative diagram."
        assert payload["input_metadata"]["caption"] == "please explain this"
        assert "commutative diagram" in deps.window.get_context()[0]["content"]
        assert deps.window.get_context()[-1]["content"] == "I can help with that diagram."

        await store.close()

    async def test_image_cancelled_during_vision_is_persisted(self, monkeypatch):
        config = make_config()
        config.vision.enabled = True
        store = MessageStore(db_path=":memory:")
        await store.initialize()
        started = asyncio.Event()

        async def slow_vision(*, image_file, image_url, config):
            started.set()
            await asyncio.sleep(60)
            return "late description"

        monkeypatch.setattr(pipeline_module, "describe_image", slow_vision)
        deps = PipelineDeps(
            config=config, registry=build_registry(config, store), sender=CaptureSender(),
            store=store, window=MessageWindow(), session=SessionState(),
            peer=Peer(chat_type=1, peer_uid="image_cancel"),
            group_key="private:image_cancel",
        )
        task = asyncio.create_task(pipeline(
            "keep this caption",
            MessageType.IMAGE,
            "photo.png",
            None,
            deps=deps,
        ))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        saved = await store.get_messages(group_key="private:image_cancel", category="image")
        assert len(saved) == 1
        payload = json.loads(saved[0].content)
        assert payload["status"] == "cancelled"
        assert payload["user"] == "keep this caption"
        assert payload["input_metadata"]["image_file"] == "photo.png"
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
        assert len(sender.sent) == 1

        await pipeline("how are you", MessageType.SHORT_TEXT, None, None, deps=deps)
        assert len(sender.sent) == 2
        assert len(window) >= 2

        await store.close()

    async def test_content_is_sent_directly(self, monkeypatch):
        config = make_config()
        sender = CaptureSender()
        store = MessageStore(db_path=":memory:")
        await store.initialize()
        registry = build_registry(config, store)

        async def fake_llm_call(messages, deps):
            return LLMResult(content="hello from content", input_tokens=3, output_tokens=2)

        monkeypatch.setattr(pipeline_module, "_do_llm_call", fake_llm_call)

        window = MessageWindow()
        session = SessionState()
        peer = Peer(chat_type=1, peer_uid="content_only_test")
        group_key = "private:content_only_test"

        deps = PipelineDeps(
            config=config, registry=registry, sender=sender,
            store=store, window=window, session=session,
            peer=peer, group_key=group_key,
        )

        await pipeline("hello", MessageType.SHORT_TEXT, None, None, deps=deps)

        assert [item["message"] for item in sender.sent] == ["hello from content"]

        await store.close()

    async def test_content_pipe_is_sent_as_one_literal_message(self, monkeypatch):
        config = make_config()
        sender = CaptureSender()
        store = MessageStore(db_path=":memory:")
        await store.initialize()
        registry = build_registry(config, store)

        async def fake_llm_call(messages, deps):
            return LLMResult(content="first | second|third ", input_tokens=3, output_tokens=2)

        monkeypatch.setattr(pipeline_module, "_do_llm_call", fake_llm_call)

        deps = PipelineDeps(
            config=config, registry=registry, sender=sender,
            store=store, window=MessageWindow(), session=SessionState(),
            peer=Peer(chat_type=1, peer_uid="split_test"),
            group_key="private:split_test",
        )

        await pipeline("hello", MessageType.SHORT_TEXT, None, None, deps=deps)

        assert [item["message"] for item in sender.sent] == ["first | second|third "]

        await store.close()

    async def test_backslash_and_pipe_are_not_rewritten(self, monkeypatch):
        config = make_config()
        sender = CaptureSender()
        store = MessageStore(db_path=":memory:")
        await store.initialize()
        registry = build_registry(config, store)

        async def fake_llm_call(messages, deps):
            return LLMResult(content=r"a \| b|c", input_tokens=3, output_tokens=2)

        monkeypatch.setattr(pipeline_module, "_do_llm_call", fake_llm_call)

        deps = PipelineDeps(
            config=config, registry=registry, sender=sender,
            store=store, window=MessageWindow(), session=SessionState(),
            peer=Peer(chat_type=1, peer_uid="escape_split_test"),
            group_key="private:escape_split_test",
        )

        await pipeline("hello", MessageType.SHORT_TEXT, None, None, deps=deps)

        assert [item["message"] for item in sender.sent] == [r"a \| b|c"]

        await store.close()

    async def test_reasoning_content_is_never_sent(self, monkeypatch):
        config = make_config()
        sender = CaptureSender()
        store = MessageStore(db_path=":memory:")
        await store.initialize()
        registry = build_registry(config, store)

        async def fake_llm_call(messages, deps):
            return LLMResult(
                content="visible reply",
                reasoning_content="private chain of thought",
                input_tokens=3,
                output_tokens=2,
            )

        monkeypatch.setattr(pipeline_module, "_do_llm_call", fake_llm_call)

        deps = PipelineDeps(
            config=config, registry=registry, sender=sender,
            store=store, window=MessageWindow(), session=SessionState(),
            peer=Peer(chat_type=1, peer_uid="reasoning_test"),
            group_key="private:reasoning_test",
        )

        await pipeline("hello", MessageType.SHORT_TEXT, None, None, deps=deps)

        assert [item["message"] for item in sender.sent] == ["visible reply"]
        assert "private chain of thought" not in str(sender.sent)

        await store.close()

    async def test_tool_round_only_sends_final_content(self, monkeypatch):
        config = make_config()
        sender = CaptureSender()
        store = MessageStore(db_path=":memory:")
        await store.initialize()
        registry = build_registry(config, store)

        calls = iter([
            LLMResult(
                content="intermediate content",
                tool_calls=[{
                    "id": "call_1",
                    "name": "memory_search",
                    "arguments": {"query": "anything"},
                }],
                input_tokens=3,
                output_tokens=2,
            ),
            LLMResult(content="final content", input_tokens=3, output_tokens=2),
        ])

        async def fake_llm_call(messages, deps):
            return next(calls)

        monkeypatch.setattr(pipeline_module, "_do_llm_call", fake_llm_call)

        deps = PipelineDeps(
            config=config, registry=registry, sender=sender,
            store=store, window=MessageWindow(), session=SessionState(),
            peer=Peer(chat_type=1, peer_uid="tool_final_test"),
            group_key="private:tool_final_test",
        )

        await pipeline("hello", MessageType.SHORT_TEXT, None, None, deps=deps)

        assert [item["message"] for item in sender.sent] == ["final content"]

        await store.close()

    async def test_tool_loop_preserves_reasoning_content_for_deepseek_followup(self, monkeypatch):
        config = make_config()
        sender = CaptureSender()
        store = MessageStore(db_path=":memory:")
        await store.initialize()
        registry = build_registry(config, store)

        captured_messages: list[list[dict]] = []
        calls = iter([
            LLMResult(
                content="",
                reasoning_content="private reasoning required by provider",
                tool_calls=[{
                    "id": "call_1",
                    "name": "memory_search",
                    "arguments": {"query": "anything"},
                }],
                input_tokens=3,
                output_tokens=2,
            ),
            LLMResult(content="final content", input_tokens=3, output_tokens=2),
        ])

        async def fake_llm_call(messages, deps):
            captured_messages.append([dict(m) for m in messages])
            return next(calls)

        monkeypatch.setattr(pipeline_module, "_do_llm_call", fake_llm_call)

        deps = PipelineDeps(
            config=config, registry=registry, sender=sender,
            store=store, window=MessageWindow(), session=SessionState(),
            peer=Peer(chat_type=1, peer_uid="reasoning_tool_loop_test"),
            group_key="private:reasoning_tool_loop_test",
        )

        await pipeline("hello", MessageType.SHORT_TEXT, None, None, deps=deps)

        followup_messages = captured_messages[1]
        assistant_tool_message = next(m for m in followup_messages if m.get("tool_calls"))
        assert assistant_tool_message["reasoning_content"] == "private reasoning required by provider"
        assert [item["message"] for item in sender.sent] == ["final content"]

        await store.close()

    async def test_staged_memory_write_commits_once_when_pipeline_is_cancelled(self, monkeypatch):
        config = make_config()
        sender = CaptureSender()
        store = MessageStore(db_path=":memory:")
        await store.initialize()
        registry = build_registry(config, store)
        second_call_started = asyncio.Event()
        captured_followup = []
        call_count = 0

        async def fake_llm_call(messages, deps):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return LLMResult(tool_calls=[{
                    "id": "write-1",
                    "name": "memory_save",
                    "arguments": {"content": "commit me exactly once"},
                }])
            captured_followup.extend(messages)
            second_call_started.set()
            await asyncio.sleep(60)
            return LLMResult(content="late")

        monkeypatch.setattr(pipeline_module, "_do_llm_call", fake_llm_call)
        deps = PipelineDeps(
            config=config, registry=registry, sender=sender,
            store=store, window=MessageWindow(), session=SessionState(),
            peer=Peer(chat_type=1, peer_uid="staged_cancel"),
            group_key="private:staged_cancel",
        )
        task = asyncio.create_task(
            pipeline("remember this", MessageType.SHORT_TEXT, None, None, deps=deps)
        )
        await second_call_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        tool_result = next(item for item in captured_followup if item.get("role") == "tool")
        assert "staged" in tool_result["content"]
        memories = await store.get_messages(group_key="private:staged_cancel", category="memory")
        assert [item.content for item in memories] == ["commit me exactly once"]
        actions = await store.get_recent_actions("private:staged_cancel")
        memory_actions = [item for item in actions if item["tool_name"] == "memory_save"]
        assert len(memory_actions) == 1
        assert memory_actions[0]["success"] is True
        await store.close()

    async def test_same_tool_stops_after_three_actual_failures(self, monkeypatch):
        from src.mutsumi_sync.tools.registry import Tool

        config = make_config()
        sender = CaptureSender()
        store = MessageStore(db_path=":memory:")
        await store.initialize()
        registry = build_registry(config, store)
        executions = 0
        call_count = 0
        captured_calls = []

        async def fail_tool(args, **deps):
            nonlocal executions
            executions += 1
            return "[Error: expected failure]"

        registry.register(Tool(
            name="always_fail",
            description="test failure",
            parameters={"type": "object", "properties": {}},
            handler=fail_tool,
        ))

        async def fake_llm_call(messages, deps):
            nonlocal call_count
            call_count += 1
            captured_calls.append([dict(item) for item in messages])
            if call_count <= 4:
                return LLMResult(tool_calls=[{
                    "id": f"fail-{call_count}",
                    "name": "always_fail",
                    "arguments": {},
                }])
            return LLMResult(content="reported")

        monkeypatch.setattr(pipeline_module, "_do_llm_call", fake_llm_call)
        deps = PipelineDeps(
            config=config, registry=registry, sender=sender,
            store=store, window=MessageWindow(), session=SessionState(),
            peer=Peer(chat_type=1, peer_uid="failure_counter"),
            group_key="private:failure_counter",
        )

        await pipeline("try tool", MessageType.SHORT_TEXT, None, None, deps=deps)

        assert executions == 3
        fourth_result = [item for item in captured_calls[4] if item.get("role") == "tool"][-1]
        assert "three consecutive failures" in fourth_result["content"]
        await store.close()

    async def test_recent_verified_actions_are_in_context_packet(self):
        config = make_config()
        store = MessageStore(db_path=":memory:")
        await store.initialize()
        await store.save_action(
            group_key="private:action_context",
            tool_name="scheduler",
            call_id="schedule-1",
            success=True,
            arguments={"scheduled_time": "tomorrow"},
            result="task #7 registered",
        )
        deps = PipelineDeps(
            config=config, registry=build_registry(config, store), sender=CaptureSender(),
            store=store, window=MessageWindow(), session=SessionState(),
            peer=Peer(chat_type=1, peer_uid="action_context"),
            group_key="private:action_context",
        )

        context = await _build_context("hello", deps)

        assert "Recent verified actions" in context[1]["content"]
        assert "scheduler" in context[1]["content"]
        assert "task #7 registered" in context[1]["content"]
        await store.close()

    async def test_action_ledger_redacts_sensitive_config_result(self, monkeypatch):
        config = make_config()
        config.model.api_key = "sk-super-secret-value"
        store = MessageStore(db_path=":memory:")
        await store.initialize()
        calls = iter([
            LLMResult(tool_calls=[{
                "id": "secret-read",
                "name": "config_manager",
                "arguments": {"action": "get", "key": "model.api_key"},
            }]),
            LLMResult(content="done"),
        ])

        async def fake_llm_call(messages, deps):
            return next(calls)

        monkeypatch.setattr(pipeline_module, "_do_llm_call", fake_llm_call)
        deps = PipelineDeps(
            config=config, registry=build_registry(config, store), sender=CaptureSender(),
            store=store, window=MessageWindow(), session=SessionState(),
            peer=Peer(chat_type=1, peer_uid="redaction"),
            group_key="private:redaction",
        )

        await pipeline("read config", MessageType.SHORT_TEXT, None, None, deps=deps)

        actions = await store.get_recent_actions("private:redaction")
        config_action = next(item for item in actions if item["tool_name"] == "config_manager")
        assert config_action["result"] == "[redacted sensitive config result]"
        assert "sk-super-secret-value" not in json.dumps(actions)
        await store.close()

    async def test_send_only_tool_round_continues_to_next_llm_call(self, monkeypatch):
        config = make_config()
        sender = CaptureSender()
        store = MessageStore(db_path=":memory:")
        await store.initialize()
        registry = build_registry(config, store)

        calls = iter([
            LLMResult(
                content="可以改，模型配置如下",
                tool_calls=[{
                    "id": "call_text",
                    "name": "send",
                    "arguments": {"text": "可以改，模型配置如下"},
                }],
                input_tokens=3,
                output_tokens=2,
            ),
            LLMResult(
                content="",
                tool_calls=[{
                    "id": "call_markdown",
                    "name": "send",
                    "arguments": {"markdown_image": "# 模型配置\n\n- 模型：deepseek-v4-flash"},
                }],
                input_tokens=3,
                output_tokens=2,
            ),
            LLMResult(content="", input_tokens=3, output_tokens=0),
        ])
        llm_call_count = 0

        async def fake_llm_call(messages, deps):
            nonlocal llm_call_count
            llm_call_count += 1
            return next(calls)

        async def fake_send_tool(args, *, sender, peer, config=None):
            if args.get("text"):
                await sender.send(peer, [{"type": "text", "data": {"text": args["text"]}}])
            if args.get("markdown_image"):
                await sender.send(peer, [{"type": "image", "data": {"file": "rendered.png"}}])
            return '{"status": "ok"}'

        monkeypatch.setattr(pipeline_module, "_do_llm_call", fake_llm_call)
        monkeypatch.setattr(pipeline_module, "send_tool", fake_send_tool)

        deps = PipelineDeps(
            config=config, registry=registry, sender=sender,
            store=store, window=MessageWindow(), session=SessionState(),
            peer=Peer(chat_type=1, peer_uid="send_loop_test"),
            group_key="private:send_loop_test",
        )

        await pipeline("show model config as markdown image", MessageType.SHORT_TEXT, None, None, deps=deps)

        assert llm_call_count == 3
        assert [item["message"][0]["type"] for item in sender.sent] == ["text", "image"]

        await store.close()

    async def test_no_reply_tool_is_registered(self):
        config = make_config()
        store = MessageStore(db_path=":memory:")
        await store.initialize()
        registry = build_registry(config, store)

        tool_names = [
            item["function"]["name"]
            for item in registry.to_openai_schema()
        ]

        assert "no_reply" in tool_names

        await store.close()

    async def test_no_reply_tool_suppresses_reply_without_another_llm_round(self, monkeypatch):
        config = make_config()
        sender = CaptureSender()
        store = MessageStore(db_path=":memory:")
        await store.initialize()
        registry = build_registry(config, store)

        calls = []

        async def fake_llm_call(messages, deps):
            calls.append(messages)
            return LLMResult(
                content="this must not be sent",
                tool_calls=[{
                    "id": "call_1",
                    "name": "no_reply",
                    "arguments": {"reason": "silent maintenance"},
                }],
                input_tokens=3,
                output_tokens=2,
            )

        monkeypatch.setattr(pipeline_module, "_do_llm_call", fake_llm_call)

        deps = PipelineDeps(
            config=config, registry=registry, sender=sender,
            store=store, window=MessageWindow(), session=SessionState(),
            peer=Peer(chat_type=1, peer_uid="no_reply_test"),
            group_key="private:no_reply_test",
        )

        await pipeline("hello", MessageType.SHORT_TEXT, None, None, deps=deps)

        assert sender.sent == []
        assert len(calls) == 1

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
