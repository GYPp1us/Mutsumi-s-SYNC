from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
import yaml

from src.mutsumi_sync.config import Config, IdentityConfig, ModelConfig
from src.mutsumi_sync.message.sender import Peer, send_succeeded
from src.mutsumi_sync.prototype.adapters import (
    ConsoleSender,
    LiveModel,
    ModelResponseError,
    build_worker_registry,
    load_prototype_prompts,
)


def live_config() -> Config:
    return Config(model=ModelConfig(api_key="test-only-key", base_url="https://model.test/v1"))


@pytest.mark.asyncio
async def test_live_model_separates_chat_and_worker_settings() -> None:
    requests: list[dict] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        assert request.headers["Authorization"] == "Bearer test-only-key"
        assert str(request.url) == "https://model.test/v1/chat/completions"
        return httpx.Response(200, json={
            "choices": [{"message": {"content": "answer", "reasoning_content": "private"}}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3},
        })

    model = LiveModel(live_config(), transport=httpx.MockTransport(handle))
    chat = await model([{"role": "user", "content": "hello"}], [], lane="chat")
    worker = await model([{"role": "user", "content": "work"}], [], lane="worker")
    assert requests[0]["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in requests[0]
    assert requests[0]["max_tokens"] < requests[1]["max_tokens"]
    assert requests[1]["thinking"] == {"type": "enabled"}
    assert requests[1]["reasoning_effort"] == "max"
    assert chat.reasoning_content is None
    assert worker.reasoning_content == "private"
    assert worker.input_tokens == 7


@pytest.mark.asyncio
async def test_live_model_parses_native_tool_calls() -> None:
    tools = [{"type": "function", "function": {"name": "read_file"}}]

    async def handle(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["tools"] == tools
        return httpx.Response(200, json={"choices": [{"message": {"content": None, "tool_calls": [{
            "id": "call_1", "type": "function", "function": {
                "name": "read_file", "arguments": '{"path":"README.md"}',
            },
        }]}}]})

    model = LiveModel(live_config(), transport=httpx.MockTransport(handle))
    result = await model([], tools, lane="worker")
    assert result.tool_calls == [{"id": "call_1", "name": "read_file", "arguments": {"path": "README.md"}}]


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    {}, {"choices": []}, {"choices": [None]}, {"choices": [{"message": {}}]},
    {"choices": [{"message": {"content": ""}}]},
    {"choices": [{"finish_reason": "length", "message": {"content": "partial"}}]},
    {"choices": [{"message": {"content": [], "tool_calls": []}}]},
    {"choices": [{"message": {"content": "answer", "tool_calls": {}}}]},
    {"choices": [{"message": {"tool_calls": [{"id": "c", "function": {"name": "tool", "arguments": "{"}}]}}]},
    {"choices": [{"message": {"tool_calls": [{"id": "c", "function": {"name": "tool", "arguments": "[]"}}]}}]},
])
async def test_live_model_rejects_invalid_answers(body: dict) -> None:
    model = LiveModel(live_config(), transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body)))
    with pytest.raises(ModelResponseError):
        await model([], [], lane="chat")


@pytest.mark.asyncio
async def test_live_model_never_turns_http_error_body_into_reply() -> None:
    model = LiveModel(live_config(), transport=httpx.MockTransport(
        lambda _: httpx.Response(401, text="secret provider diagnostic"),
    ))
    with pytest.raises(ModelResponseError, match="HTTP 401") as error:
        await model([], [], lane="chat")
    assert "secret" not in str(error.value)


@pytest.mark.asyncio
async def test_live_model_transport_failure_is_sanitized() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("secret connection details", request=request)

    model = LiveModel(live_config(), transport=httpx.MockTransport(handle))
    with pytest.raises(ModelResponseError, match="Model transport failed") as error:
        await model([], [], lane="worker")
    assert "secret" not in str(error.value)


@pytest.mark.asyncio
async def test_live_model_cancellation_reaches_transport() -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def handle(request: httpx.Request) -> httpx.Response:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        raise AssertionError("unreachable")

    model = LiveModel(live_config(), transport=httpx.MockTransport(handle))
    task = asyncio.create_task(model([], [], lane="worker"))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()


def test_prototype_prompts_use_selected_file_and_identity(tmp_path: Path) -> None:
    path = tmp_path / "custom-prompts.yaml"
    path.write_text(yaml.safe_dump({
        "persona": "我是 {{bot_nickname}}",
        "prototype_chat": "和 {{user}} 对话",
        "prototype_worker": "后台 {{bot_user_id}}",
        "prototype_wait": "等候 {{user_nickname}}",
        "prototype_error": "失败 {{user_id}}",
    }, allow_unicode=True), encoding="utf-8")
    config = Config(identity=IdentityConfig(
        bot_user_id="100", bot_nickname="角色", owner_user_id="200", owner_nickname="名字", owner_alias="前辈",
    ))
    config._system_prompts_path = str(path)
    prompts = load_prototype_prompts(config)
    assert prompts.chat == "和 前辈 对话"
    assert prompts.persona == "我是 角色"
    assert prompts.wait == "等候 名字"
    assert prompts.error == "失败 200"


def test_prototype_prompts_fail_if_selected_file_lacks_fields(tmp_path: Path) -> None:
    path = tmp_path / "custom-prompts.yaml"
    path.write_text("persona: custom\n", encoding="utf-8")
    config = Config()
    config._system_prompts_path = str(path)
    with pytest.raises(ValueError, match="prototype_chat"):
        load_prototype_prompts(config)


@pytest.mark.asyncio
async def test_console_sender_has_realistic_local_receipts() -> None:
    sender = ConsoleSender(echo=False)
    peer = Peer(chat_type=1, peer_uid="local")
    first = await sender.send(peer, "a | b")
    second = await sender.send(peer, [{"type": "text", "data": {"text": "second"}}])
    assert send_succeeded(first)
    assert first["data"]["message_id"] != second["data"]["message_id"]
    assert sender.sent[0]["message"] == "a | b"


@pytest.mark.asyncio
async def test_worker_files_are_read_only_and_bounded(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (root / "README.md").write_text("project facts", encoding="utf-8")
    (root / "config.yaml").write_text("secret", encoding="utf-8")
    (root / ".env").write_text("secret", encoding="utf-8")
    (root / "data").mkdir()
    (root / "data" / "private.txt").write_text("secret", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("outside", encoding="utf-8")
    registry = build_worker_registry(root)
    assert {tool.name for tool in registry.snapshot()[0]} == {"read_file", "list_files"}
    result = json.loads(await registry.execute("read_file", {"path": "README.md"}))
    assert result["content"] == "project facts"
    listing = json.loads(await registry.execute("list_files"))
    assert listing["entries"] == ["README.md"]
    for path in ("../outside.txt", str(tmp_path / "outside.txt"), "config.yaml", "config.yaml::$DATA", "config.yaml.", ".env", "data/private.txt"):
        assert (await registry.execute("read_file", {"path": path})).startswith("[Error:")
    assert (await registry.execute("read_file", {"path": "README.md", "write": "bad"})).startswith("[Error:")
    assert (root / "README.md").read_text(encoding="utf-8") == "project facts"


@pytest.mark.asyncio
async def test_worker_file_output_is_truncated(tmp_path: Path) -> None:
    (tmp_path / "large.txt").write_text("中" * 30_000, encoding="utf-8")
    registry = build_worker_registry(tmp_path)
    result = json.loads(await registry.execute("read_file", {"path": "large.txt"}))
    assert len(result["content"]) == 20_000
    assert result["truncated"] is True
