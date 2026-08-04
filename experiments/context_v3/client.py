"""Small environment-only client for Chat Completions and Responses APIs."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(frozen=True)
class Provider:
    name: str
    model: str
    base_url: str
    protocol: str
    api_key_env: str


@dataclass(frozen=True)
class ModelResponse:
    content: str
    tool_calls: tuple[dict[str, Any], ...]
    usage: dict[str, Any]


def _first_env(*names: str) -> tuple[str, str] | None:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return name, value
    return None


def resolve_provider() -> tuple[Provider, str]:
    """Prefer DeepSeek, then use the requested gpt-5.6-luna fallback."""
    deepseek = _first_env("DEEPSEEK_API_KEY", "DS_API_KEY")
    if deepseek:
        return (
            Provider(
                "deepseek-v4-flash",
                os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash"),
                os.environ.get("DEEPSEEK_BASE_URL", "https://opencode.ai/zen/go/v1"),
                "chat",
                deepseek[0],
            ),
            deepseek[1],
        )

    fallback = _first_env("HUNIUHUB_API_KEY", "OPENAI_API_KEY", "MUTSUMI_EXPERIMENT_API_KEY")
    if fallback:
        return (
            Provider(
                "gpt-5.6-luna",
                os.environ.get("LUNA_MODEL", "gpt-5.6-luna"),
                os.environ.get("LUNA_BASE_URL", "https://api.huniuhub.com"),
                "responses",
                fallback[0],
            ),
            fallback[1],
        )
    raise RuntimeError(
        "No experiment API key found. Set DEEPSEEK_API_KEY/DS_API_KEY, "
        "or HUNIUHUB_API_KEY/OPENAI_API_KEY/MUTSUMI_EXPERIMENT_API_KEY."
    )


def _chat_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["parameters"],
            },
        }
        for tool in tools
    ]


def _response_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "name": tool["name"],
            "description": tool["description"],
            "parameters": tool["parameters"],
        }
        for tool in tools
    ]


def _text_from_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("text", item.get("content", "")) or ""))
        return "".join(parts)
    return str(value or "")


def _parse_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value or "{}"))
    except json.JSONDecodeError:
        return {"_malformed_arguments": str(value)}
    return parsed if isinstance(parsed, dict) else {"_arguments": parsed}


def _parse_chat(data: dict[str, Any]) -> ModelResponse:
    message = data.get("choices", [{}])[0].get("message", {})
    calls = []
    for call in message.get("tool_calls") or []:
        function = call.get("function", {})
        calls.append(
            {
                "id": call.get("id", ""),
                "name": function.get("name", ""),
                "arguments": _parse_json_object(function.get("arguments")),
            }
        )
    return ModelResponse(
        content=_text_from_value(message.get("content", "")),
        tool_calls=tuple(calls),
        usage=data.get("usage", {}) or {},
    )


def _parse_responses(data: dict[str, Any]) -> ModelResponse:
    calls = []
    text_parts: list[str] = []
    for item in data.get("output", []) or []:
        if item.get("type") == "function_call":
            calls.append(
                {
                    "id": item.get("call_id", item.get("id", "")),
                    "name": item.get("name", ""),
                    "arguments": _parse_json_object(item.get("arguments")),
                }
            )
        elif item.get("type") == "message":
            for part in item.get("content", []) or []:
                if part.get("type") in {"output_text", "text"}:
                    text_parts.append(str(part.get("text", "")))
    content = str(data.get("output_text", "") or "") or "".join(text_parts)
    return ModelResponse(
        content=content,
        tool_calls=tuple(calls),
        usage=data.get("usage", {}) or {},
    )


class LLMClient:
    def __init__(self, provider: Provider, api_key: str, timeout: float = 180.0) -> None:
        self.provider = provider
        self._api_key = api_key
        self.timeout = timeout

    async def complete(
        self,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]],
        *,
        max_tokens: int = 900,
    ) -> ModelResponse:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        if self.provider.protocol == "chat":
            payload: dict[str, Any] = {
                "model": self.provider.model,
                "messages": messages,
                "tools": _chat_tools(tools),
                "tool_choice": "auto",
                "temperature": 0.2,
                "max_tokens": max_tokens,
            }
            endpoint = f"{self.provider.base_url.rstrip('/')}/chat/completions"
        else:
            payload = {
                "model": self.provider.model,
                "input": messages,
                "tools": _response_tools(tools),
                "temperature": 0.2,
                "max_output_tokens": max_tokens,
                "reasoning": {"effort": "high"},
                "store": False,
                "disable_response_storage": True,
                "model_reasoning_effort": "high",
            }
            endpoint = f"{self.provider.base_url.rstrip('/')}/responses"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(endpoint, headers=headers, json=payload)
        if response.status_code >= 400:
            raise RuntimeError(
                f"{self.provider.name} returned HTTP {response.status_code}: {response.text[:500]}"
            )
        data = response.json()
        return _parse_chat(data) if self.provider.protocol == "chat" else _parse_responses(data)
