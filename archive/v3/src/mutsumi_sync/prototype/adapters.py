"""Explicit local adapters for the conversation/background-work prototype."""

from __future__ import annotations

import asyncio
import copy
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import yaml

from ..config import Config, render_system_prompts, resolve_system_prompts_path
from ..message.sender import Peer
from ..pipeline import LLMResult
from ..tools.registry import Tool, ToolRegistry

logger = logging.getLogger("mutsumi.prototype.adapters")


@dataclass(frozen=True)
class PrototypePrompts:
    chat: str
    worker: str
    wait: str
    error: str
    persona: str = ""


def load_prototype_prompts(config: Config) -> PrototypePrompts:
    """Load only the selected prompt file, with the production identity renderer."""
    selected = config._system_prompts_path
    config_dir = Path(config._config_path).parent if config._config_path else None
    path = (
        Path(selected)
        if selected
        else resolve_system_prompts_path(config.prompts.system_file, config_dir)
    )
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Prototype prompts must be a YAML mapping")
    values: dict[str, str] = {}
    for name in ("chat", "worker", "wait", "error"):
        value = raw.get(f"prototype_{name}")
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"Missing non-empty prototype_{name} in {path}")
        # Reuse the same placeholder substitution as ordinary runtime prompts.
        rendered = render_system_prompts(
            config.prompts.system.model_copy(update={"persona": value}),
            config.identity,
        )
        values[name] = rendered.persona.strip()
    persona = raw.get("persona", "")
    if not isinstance(persona, str):
        raise ValueError("Prototype persona must be text")
    values["persona"] = render_system_prompts(
        config.prompts.system.model_copy(update={"persona": persona}), config.identity,
    ).persona.strip()
    return PrototypePrompts(**values)


class ModelResponseError(RuntimeError):
    """Provider or response failure; never a user-visible model answer."""


class LiveModel:
    """One Chat Completions endpoint, with explicit short/long request settings."""

    def __init__(
        self,
        config: Config,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if config.model.provider not in {"deepseek", "openai-compatible"}:
            raise ValueError("Prototype supports deepseek/openai-compatible only")
        if not config.model.api_key.strip() or not config.model.base_url.strip():
            raise ValueError("Live mode requires model.api_key and model.base_url")
        self.config = config.model.model_copy(deep=True)
        self.transport = transport

    async def __call__(
        self, messages: list[dict], tools: list[dict], *, lane: str,
    ) -> LLMResult:
        if lane not in {"chat", "worker"}:
            raise ValueError("Unknown prototype model lane")
        config = self.config
        payload: dict[str, Any] = {
            "model": config.model,
            "messages": messages,
            "temperature": config.temperature,
            "thinking": {"type": "disabled" if lane == "chat" else "enabled"},
            "max_tokens": 1024 if lane == "chat" else 8192,
        }
        if lane == "worker":
            payload["reasoning_effort"] = config.reasoning_effort
        if tools:
            payload.update(tools=tools, tool_choice="auto")
        url = f"{config.base_url.rstrip('/')}/chat/completions"
        headers = {"Authorization": f"Bearer {config.api_key}"}
        logger.info("Model request lane=%s model=%s tools=%d", lane, config.model, len(tools))
        try:
            async with httpx.AsyncClient(timeout=120, transport=self.transport) as client:
                response = await client.post(url, headers=headers, json=payload)
        except httpx.HTTPError as exc:
            logger.warning("Model transport failed lane=%s type=%s", lane, type(exc).__name__)
            raise ModelResponseError("Model transport failed") from None
        if response.status_code != 200:
            logger.warning("Model response rejected lane=%s status=%d", lane, response.status_code)
            raise ModelResponseError(f"Model returned HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError:
            raise ModelResponseError("Model response is not JSON") from None
        result = _parse_response(data)
        if lane == "chat":
            result.reasoning_content = None
        return result


def _parse_response(data: object) -> LLMResult:
    if not isinstance(data, dict):
        raise ModelResponseError("Model response is not an object")
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise ModelResponseError("Model response has no valid choice")
    choice = choices[0]
    if choice.get("finish_reason") in {"length", "content_filter"}:
        raise ModelResponseError("Model response did not finish normally")
    message = choice.get("message")
    if not isinstance(message, dict):
        raise ModelResponseError("Model response has no message")
    content = message.get("content")
    if content is not None and not isinstance(content, str):
        raise ModelResponseError("Model content must be text")
    reasoning = message.get("reasoning_content")
    if reasoning is not None and not isinstance(reasoning, str):
        raise ModelResponseError("Model reasoning_content must be text")
    raw_calls = message.get("tool_calls")
    if raw_calls is None:
        raw_calls = []
    if not isinstance(raw_calls, list):
        raise ModelResponseError("Model tool_calls must be a list")
    calls: list[dict] = []
    seen: set[str] = set()
    for call in raw_calls:
        if not isinstance(call, dict) or call.get("type", "function") != "function":
            raise ModelResponseError("Model returned an invalid tool call")
        call_id = call.get("id")
        function = call.get("function")
        if not isinstance(call_id, str) or not call_id or call_id in seen:
            raise ModelResponseError("Model tool call requires a unique id")
        if not isinstance(function, dict) or not isinstance(function.get("name"), str) or not function["name"]:
            raise ModelResponseError("Model tool call requires a function name")
        arguments = function.get("arguments")
        if not isinstance(arguments, str):
            raise ModelResponseError("Model tool arguments must be JSON text")
        try:
            parsed = json.loads(arguments)
        except ValueError:
            raise ModelResponseError("Model tool arguments are invalid JSON") from None
        if not isinstance(parsed, dict):
            raise ModelResponseError("Model tool arguments must be an object")
        calls.append({"id": call_id, "name": function["name"], "arguments": parsed})
        seen.add(call_id)
    if not calls and not (content or "").strip():
        raise ModelResponseError("Model response is empty")
    usage = data.get("usage") or {}
    if not isinstance(usage, dict):
        usage = {}
    details = usage.get("completion_tokens_details") or {}
    if not isinstance(details, dict):
        details = {}

    def count(source: dict, key: str, default: int = 0) -> int:
        value = source.get(key, default)
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else default

    input_tokens = count(usage, "prompt_tokens")
    return LLMResult(
        content=content or "", reasoning_content=reasoning, tool_calls=calls,
        input_tokens=input_tokens,
        output_tokens=count(usage, "completion_tokens"),
        reasoning_tokens=count(details, "reasoning_tokens"),
        cache_hit_tokens=count(usage, "prompt_cache_hit_tokens"),
        cache_miss_tokens=count(usage, "prompt_cache_miss_tokens", input_tokens),
    )


class ConsoleSender:
    """NapCat-shaped local receipt; this adapter never connects to QQ."""

    def __init__(self, *, echo: bool = True) -> None:
        self.sent: list[dict] = []
        self.echo = echo

    async def send(self, peer: Peer, message: str | list[dict]) -> dict:
        message_id = len(self.sent) + 1
        self.sent.append({
            "peer": peer.model_dump(), "message": copy.deepcopy(message),
            "message_id": message_id,
        })
        if self.echo:
            print(f"[{peer.peer_uid} #{message_id}] {message}", flush=True)
        return {"status": "ok", "retcode": 0, "data": {"message_id": message_id}}


def build_worker_registry(root: Path) -> ToolRegistry:
    """Read-only local files, bounded to a selected project without credentials."""
    root = root.resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Worker root must be a directory")
    registry = ToolRegistry()
    denied = {"data", "node_modules", "config.yaml", "__pycache__"}

    def resolve(value: object) -> Path:
        if not isinstance(value, str) or not value or Path(value).is_absolute():
            raise ValueError("path must be relative to the selected project")
        # Check the requested and resolved forms, including directory symlinks.
        requested = Path(value)
        target = (root / requested).resolve(strict=True)
        try:
            relative = target.relative_to(root)
        except ValueError:
            raise ValueError("path leaves the selected project") from None
        for part in (*requested.parts, *relative.parts):
            if (
                part == ".." or part.lower() in denied or ":" in part
                or (part != "." and (part.startswith(".") or part.endswith((".", " "))))
            ):
                raise ValueError("path is excluded from prototype file access")
        return target

    def read(args: dict) -> str:
        if set(args) != {"path"}:
            raise ValueError("read_file requires only path")
        target = resolve(args["path"])
        if not target.is_file():
            raise ValueError("path is not a regular file")
        with target.open(encoding="utf-8") as stream:
            text = stream.read(20_001)
        if "\0" in text:
            raise ValueError("binary files are not supported")
        return json.dumps({
            "path": target.relative_to(root).as_posix(), "content": text[:20_000],
            "truncated": len(text) > 20_000,
        }, ensure_ascii=False)

    def listing(args: dict) -> str:
        if set(args) - {"path"}:
            raise ValueError("list_files accepts only path")
        target = resolve(args.get("path", "."))
        if not target.is_dir():
            raise ValueError("path is not a directory")
        entries: list[str] = []
        for item in sorted(target.iterdir(), key=lambda entry: entry.name.lower()):
            try:
                checked = resolve(item.relative_to(root).as_posix())
            except (ValueError, OSError):
                continue
            entries.append(item.name + ("/" if checked.is_dir() else ""))
            if len(entries) == 201:
                break
        return json.dumps({
            "path": target.relative_to(root).as_posix(), "entries": entries[:200],
            "truncated": len(entries) > 200,
        }, ensure_ascii=False)

    async def read_file(args: dict) -> str:
        try:
            return await asyncio.to_thread(read, args)
        except (ValueError, OSError, UnicodeError):
            return "[Error: file is unavailable or outside the permitted text-file scope]"

    async def list_files(args: dict) -> str:
        try:
            return await asyncio.to_thread(listing, args)
        except (ValueError, OSError):
            return "[Error: directory is unavailable or outside the permitted scope]"

    registry.register(Tool(
        name="read_file", description="读取选定项目中的 UTF-8 文本文件，最多返回 20000 字符。config.yaml、隐藏路径和 data 目录不可读取。",
        parameters={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"], "additionalProperties": False},
        handler=read_file, source="prototype",
    ))
    registry.register(Tool(
        name="list_files", description="列出选定项目内一个目录的可读子项，最多 200 项；默认列出项目根目录。",
        parameters={"type": "object", "properties": {"path": {"type": "string"}}, "additionalProperties": False},
        handler=list_files, source="prototype",
    ))
    return registry
