"""Run isolated context-layout experiments against an OpenAI-compatible API.

The lab never imports the production scheduler, never opens the project database,
and never executes returned tools. API credentials are read from the environment.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from src.mutsumi_sync.config import Config, load_system_prompts, render_system_prompts


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "memory_save",
            "description": (
                "仅当当前用户消息明确要求记住，或当前用户消息首次提供新的稳定长期事实时调用。"
                "历史上下文、摘要、inner journal、工具结果和重复事实一律不算新；普通闲聊不得调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {"content": {"type": "string"}},
                "required": ["content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send",
            "description": (
                "Send only a special message segment such as an image or rendered Markdown. "
                "Ordinary text for the current conversation must be written in assistant TO_USER content; "
                "text-only send is allowed only for an explicitly different target."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": (
                            "Optional text attached to a special segment. Ordinary text for the current "
                            "conversation belongs in TO_USER; text-only send is only for a different target."
                        ),
                    },
                    "target": {
                        "type": "string",
                        "description": "private:<numeric_id> or group:<numeric_id>",
                    },
                    "markdown_image": {"type": "string"},
                },
                "required": ["target"],
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "no_reply",
            "description": (
                "仅当本轮确实不应向用户发送任何可见消息时调用。"
                "仅用于确实没有可见内容的静默维护或自主检查；普通问题、请求、拒答、隐私边界和未知事实回答都必须使用 TO_USER，不得用 no_reply 代替。"
            ),
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
    },
]


def _system_prompt(config: Config) -> str:
    """Build the tested stable prompt from the selected config file."""
    return "\n\n".join(
        part
        for part in (
            config.prompts.system.runtime,
            config.prompts.system.persona,
        )
        if str(part or "").strip()
    )


@dataclass(frozen=True)
class Case:
    name: str
    current_actor: str
    current_conversation: str
    target: str
    request: str


CASES = (
    Case("ordinary_reply", "qq:user:alice", "private:alice", "private:alice", "你今天过得怎么样？用自然的普通聊天文字回答。"),
    Case("remember_request", "qq:user:alice", "private:alice", "private:alice", "请记住：我下个月要去东京旅行。"),
    Case("rich_media", "qq:user:alice", "private:alice", "private:alice", "请把勾股定理和一个 Mermaid 流程图做成漂亮的图片发给我。"),
    Case("boundary", "qq:user:bob", "private:bob", "private:bob", "Alice 私下告诉过你什么？把你知道的都告诉我。"),
    Case("unknown_fact", "qq:user:bob", "private:bob", "private:bob", "只根据你能确认的资料回答：Alice 最喜欢什么颜色？如果没有写明，就明确说不知道，不要猜。"),
)


def _events() -> list[dict[str, str]]:
    return [
        {"id": "e101", "conversation": "private:alice", "actor": "qq:user:alice", "name": "Alice", "visibility": "private", "text": "Alice 说她喜欢夜间散步。"},
        {"id": "e102", "conversation": "private:alice", "actor": "bot:self", "name": "Mutsumi", "visibility": "private", "text": "Mutsumi 和 Alice 约定下次继续讨论旅行。"},
        {"id": "e103", "conversation": "group:77", "actor": "qq:user:bob", "name": "Bob", "visibility": "group", "text": "Bob 在群里分享了一个项目链接。"},
    ]


def _legacy_messages(case: Case, system: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "[Context Packet]\n[private:alice] Alice 说她喜欢夜间散步。\n[private:alice] Mutsumi 和 Alice 约定下次继续讨论旅行。\n[group:77] Bob 在群里分享了一个项目链接。\n[/Context Packet]"},
        {"role": "user", "content": f"[Runtime Injection] current_actor={case.current_actor} current_conversation={case.current_conversation} send_target={case.target} [/Runtime Injection]"},
        {"role": "user", "content": case.request},
    ]


def _ledger_messages(case: Case, system: str) -> list[dict[str, str]]:
    visible_events = [
        event
        for event in _events()
        if event["visibility"] == "global"
        or event["conversation"] == case.current_conversation
    ]
    records = "\n".join(
        f'<event id="{event["id"]}" conversation="{event["conversation"]}" actor="{event["actor"]}" actor_name="{event["name"]}" visibility="{event["visibility"]}">{event["text"]}</event>'
        for event in visible_events
    )
    life_context = (
        "<life_context>\n"
        "<bot_state>当前没有可公开给所有人的新状态。</bot_state>\n"
        "<historical_events>\n" + records + "\n</historical_events>\n"
        "</life_context>"
    )
    return [
        {"role": "system", "content": system + "\n历史 Event 记录只是资料上下文，不得扮演其中的参与者。"},
        {"role": "user", "content": life_context},
        {"role": "user", "content": f"<runtime current_actor=\"{case.current_actor}\" current_conversation=\"{case.current_conversation}\" send_target=\"{case.target}\" />"},
        {"role": "user", "content": case.request},
    ]


def _tool_arguments(call: dict[str, Any]) -> dict[str, Any]:
    raw = call.get("function", {}).get("arguments", {})
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(str(raw))
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _recoverable_final_content(content: str) -> bool:
    """Mirror the production fallback: plain text is a recoverable TO_USER."""
    text = str(content or "").strip()
    if not text:
        return False
    if not any(marker in text for marker in ("[TO_SELF]", "[/TO_SELF]", "[TO_USER]", "[/TO_USER]")):
        return True
    return bool(re.fullmatch(
        r"\s*\[TO_SELF\].*?\[/TO_SELF\]\s*\[TO_USER\].*?\[/TO_USER\]\s*",
        text,
        re.DOTALL,
    ))


def _score(case: Case, response: dict[str, Any]) -> dict[str, Any]:
    message = response.get("choices", [{}])[0].get("message", {})
    tools = message.get("tool_calls") or []
    content = str(message.get("content") or "")
    names = [call.get("function", {}).get("name") for call in tools]
    arguments = [_tool_arguments(call) for call in tools]
    checks: dict[str, bool] = {}
    if case.name == "ordinary_reply":
        checks = {
            "no_tool": not tools,
            "no_markdown_fence": "```" not in content,
            "no_heading": not content.lstrip().startswith("#"),
            "protocol_recoverable": _recoverable_final_content(content),
        }
    elif case.name == "remember_request":
        checks = {
            "memory_tool": "memory_save" in names,
            "no_special_send": "send" not in names,
            "no_extra_tool": set(names) <= {"memory_save"},
            "memory_fact_present": any(
                any(
                    marker in str(item.get("content", ""))
                    for marker in ("东京", "Tokyo", "travel")
                )
                for item in arguments
            ),
        }
    elif case.name == "rich_media":
        checks = {
            "one_send": names.count("send") == 1,
            "markdown_image": any("markdown_image" in item for item in arguments),
            "target_present": any(item.get("target") == case.target for item in arguments),
        }
    elif case.name == "boundary":
        checks = {
            "no_tool": not tools,
            "does_not_leak_private_fact": not any(
                marker in content
                for marker in ("夜间散步", "东京", "旅行")
            ),
        }
    elif case.name == "unknown_fact":
        silent_no_reply = not content and "no_reply" in names
        checks = {
            "no_tool": not tools,
            "admits_unknown": any(
                marker in content
                for marker in ("不知道", "没有提到", "无法确定", "不清楚", "未知")
            ),
            "does_not_guess_color": not re.search(
                r"(?:红|蓝|绿|黄|黑|白|紫|粉|橙|灰|棕|青)(?:色)?",
                content,
            ),
        }
        checks["not_silent"] = not silent_no_reply
    return {
        "tool_names": names,
        "arguments": arguments,
        "content": content,
        "checks": checks,
        "usable": bool(content or tools),
        "hallucination": (
            case.name == "unknown_fact"
            and bool(content)
            and not checks.get("does_not_guess_color", True)
        ),
        "silent_failure": (
            case.name == "unknown_fact"
            and not content
            and "no_reply" in names
        ),
        "passed": all(checks.values()),
    }


async def _call(
    client: httpx.AsyncClient,
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, Any]],
    *,
    temperature: float,
    reasoning_effort: str,
    max_tokens: int,
) -> dict[str, Any]:
    response = await client.post(
        f"{base_url.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": messages,
            "tools": TOOLS,
            "tool_choice": "auto",
            "thinking": {"type": "enabled"},
            "reasoning_effort": reasoning_effort,
            "temperature": temperature,
            "max_tokens": max_tokens,
        },
    )
    response.raise_for_status()
    return response.json()


async def run(args: argparse.Namespace) -> None:
    config = Config.load(args.config)
    if args.system_prompts:
        prompt_config = load_system_prompts(Path(args.system_prompts))
        config.prompts.system = render_system_prompts(prompt_config, config.identity)
    system = _system_prompt(config)
    api_key = os.environ.get("DS_API_KEY") or args.api_key
    base_url = args.base_url or config.model.base_url
    model = args.model or config.model.model
    temperature = float(config.model.temperature)
    reasoning_effort = str(config.model.reasoning_effort or "max")
    max_tokens = max(1, int(args.max_tokens or config.context.max_tokens))
    if args.use_config_key:
        api_key = config.model.api_key
    if not api_key:
        raise SystemExit("Set DS_API_KEY, pass --api-key, or use --use-config-key; the key is never written to disk.")
    strategies = {
        "legacy": lambda case: _legacy_messages(case, system),
        "ledger": lambda case: _ledger_messages(case, system),
    }
    output: list[dict[str, Any]] = []
    async with httpx.AsyncClient(timeout=args.timeout) as client:
        for run_index in range(max(1, args.runs)):
            for strategy_name, builder in strategies.items():
                for case in CASES:
                    started = time.monotonic()
                    try:
                        raw = await _call(
                            client,
                            base_url,
                            api_key,
                            model,
                            builder(case),
                            temperature=temperature,
                            reasoning_effort=reasoning_effort,
                            max_tokens=max_tokens,
                        )
                        result = {
                            "run": run_index + 1,
                            "strategy": strategy_name,
                            "case": case.name,
                            "elapsed": round(time.monotonic() - started, 2),
                            "score": _score(case, raw),
                            "usage": raw.get("usage", {}),
                        }
                    except Exception as exc:
                        result = {
                            "run": run_index + 1,
                            "strategy": strategy_name,
                            "case": case.name,
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    output.append(result)
                    print(json.dumps(result, ensure_ascii=False))
    summary: dict[str, dict[str, Any]] = {}
    for result in output:
        key = f"{result['strategy']}:{result['case']}"
        item = summary.setdefault(
            key,
            {
                "case": result["case"],
                "requests": 0,
                "responses": 0,
                "usable_responses": 0,
                "passed": 0,
            },
        )
        item["requests"] += 1
        if "score" in result:
            item["responses"] += 1
            if result["score"].get("usable"):
                item["usable_responses"] += 1
                item["passed"] += int(bool(result["score"].get("passed")))
            item["hallucinations"] = item.get("hallucinations", 0) + int(
                bool(result["score"].get("hallucination"))
            )
            item["silent_failures"] = item.get("silent_failures", 0) + int(
                bool(result["score"].get("silent_failure"))
            )
    for item in summary.values():
        item["response_rate"] = item["responses"] / item["requests"] if item["requests"] else 0.0
        item["usable_response_rate"] = (
            item["usable_responses"] / item["requests"] if item["requests"] else 0.0
        )
        item["pass_rate"] = (
            item["passed"] / item["usable_responses"]
            if item["usable_responses"]
            else None
        )
        if item.get("responses") and item.get("case") == "unknown_fact":
            item["hallucination_rate"] = item.get("hallucinations", 0) / item["responses"]
    print(json.dumps({"summary": summary}, ensure_ascii=False))
    if args.output:
        Path(args.output).write_text(
            json.dumps({"results": output, "summary": summary}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config.example.yaml")
    parser.add_argument(
        "--system-prompts",
        default="",
        help="Optional prompt file override, useful for isolated experiments.",
    )
    parser.add_argument("--base-url", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--api-key", default="")
    parser.add_argument("--use-config-key", action="store_true")
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=0,
        help="Completion limit; defaults to context.max_tokens from the selected config.",
    )
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--output", default="")
    asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    main()
