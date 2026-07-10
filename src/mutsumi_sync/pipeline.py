from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING, Any, Callable, Awaitable

import httpx

from .message.classifier import MessageType
from .message.sender import send_failure_message, send_succeeded
from .memory.store import StoredMessage
from .memory.timestamps import (
    ensure_timestamped_lines,
    format_context_timestamp,
)
from .tools.send import send_tool
from .vision import describe_image
from .logging import log_context, log_llm_result, log_tool_call, log_send, ESTIMATE_CHARS_PER_TOKEN

if TYPE_CHECKING:
    from .scheduler import PipelineDeps

logger = logging.getLogger("mutsumi.pipeline")

MAX_TOOL_STEPS = 10
MAX_SENDS_PER_LOOP = 5
WRITE_TOOLS = {"self_note", "memory_save", "priority_override"}
NO_REPLY_TOOL = "no_reply"


def _report_state(deps: PipelineDeps, state: str) -> None:
    logger.info("[PIPE] state=%s peer=%s group=%s", state, deps.peer.peer_uid, deps.group_key)
    if deps.report_state:
        deps.report_state(state)


@dataclass
class LLMResult:
    content: str = ""
    reasoning_content: str | None = None
    tool_calls: list[dict] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // ESTIMATE_CHARS_PER_TOKEN)


def _estimate_msg_tokens(msg: dict) -> int:
    return _estimate_tokens(str(msg.get("content", "")))


def _is_placeholder_summary(summary: str) -> bool:
    normalized = summary.strip().lower()
    return (
        "messages archived on shutdown" in normalized
        and (
            normalized.startswith("[会话结束]")
            or normalized.startswith("[會話結束]")
            or normalized.startswith("[conversation ended]")
        )
    )


def _build_default_system_prompt(config) -> str:
    return (
        "You are an assistant running on Mutsumi's SYNC, a NapCat-based QQ social agent platform.\n"
        "The provider tool schema is authoritative. Use only tools present in that schema and obey each returned result.\n"
        "Never claim that a tool or send action succeeded without a real successful tool result.\n"
        "Memory write tools are staged during the tool loop and committed atomically during pipeline cleanup; a staged result is not yet a persisted result.\n"
        "If the same tool returns an error three consecutive times, stop retrying it and explain the failure when a visible reply is appropriate.\n"
        "Assistant content from the final round without tool_calls is the ordinary user-visible reply and is currently sent as one QQ message.\n"
        "Use tools for actual side effects, external queries, memory maintenance, special message segments, or deliberate silence.\n"
        "reasoning_content is private chain-of-thought state: it may be retained only inside the current provider tool loop and is never sent to the user.\n"
        "Keep replies natural, context-aware, and appropriate for an ongoing social conversation.\n"
        "The first user message may be a [Context Packet]. It is persistent background context, not a fresh user request.\n"
        "A later [Runtime Injection] user message is temporary platform state, not user-authored chat or durable history.\n"
        "Timestamps, current time, source, peer data, and runtime flags are supplied by the platform. Do not invent or rewrite them.\n"
        "Priority Override appears once per request and has higher priority than ordinary memory.\n"
        "Heartbeat requests are silent health checks and must not create durable conversation or memory state.\n"
        "Image descriptions are supplied by a configured vision provider and should be handled as ordinary user context."
    )


async def _inject_self_note(store, group_key: str, config) -> str:
    note = await store.get_current_self_note(group_key)
    if not note or not note.get("content"):
        return ""

    content = ensure_timestamped_lines(note["content"])
    current = _estimate_tokens(content)
    target = config.memory.self_note_target_tokens
    limit = int(target * config.memory.self_note_max_multiplier)

    if current > limit:
        chars = limit * ESTIMATE_CHARS_PER_TOKEN
        content = content[:chars] + "\n[truncated]"

    return f"[私人印象 — current: {current} / target: {target} tokens]\n{content}\n[/私人印象]"


async def _inject_priority_override(store, group_key: str) -> str:
    item = await store.get_current_priority_override(group_key)
    if not item:
        return ""
    content = ensure_timestamped_lines(str(item.get("content", "")))
    if not content.strip():
        return ""
    return f"[Priority Override]\n{content}\n[/Priority Override]"


def _append_priority_override(content: str, priority_override: str) -> str:
    if not priority_override:
        return content
    return f"{content.rstrip()}\n\n{priority_override}"


def _build_runtime_injection(deps: PipelineDeps, priority_override: str) -> str:
    sections = [
        "[Runtime Injection]",
        f"Current time: {format_context_timestamp(time.time())}",
        f"Source: {deps.source}",
        f"Silent mode: {'true' if deps.silent else 'false'}",
        f"Remember input: {'true' if deps.remember_input else 'false'}",
        f"Peer: chat_type={deps.peer.chat_type}, peer_uid={deps.peer.peer_uid}",
        f"Group key: {deps.group_key}",
    ]
    if priority_override:
        sections.append(priority_override)
    sections.append("[/Runtime Injection]")
    return "\n".join(sections)


def _with_context_timestamp(content: str, created_at: Any | None) -> str:
    return f"[time: {format_context_timestamp(created_at)}]\n{content}"


async def _build_context(message: str, deps: PipelineDeps) -> list[dict[str, Any]]:
    config = deps.config
    store = deps.store

    system_prompt = _build_default_system_prompt(config)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
    ]
    bootstrap_sections: list[str] = []

    self_note_text = await _inject_self_note(store, deps.group_key, config)
    if self_note_text:
        bootstrap_sections.append(self_note_text)

    summaries = await store.get_summaries(deps.group_key, limit=config.context.summaries_max_count)
    if summaries:
        summary_texts = []
        for s in summaries:
            if _is_placeholder_summary(str(s["summary"])):
                continue
            source_label = "user" if s["source"] == "user" else "assistant"
            timestamp = format_context_timestamp(s.get("created_at"))
            summary_texts.append(f"[{timestamp}][{source_label}]: {s['summary']}")
        if summary_texts:
            messages.append({"role": "system", "content": "[摘要]\n" + "\n".join(summary_texts) + "\n[/摘要]"})

    actions = await store.get_recent_actions(
        deps.group_key,
        limit=config.context.recent_actions_max_count,
    )
    if actions:
        action_lines = []
        for action in actions:
            stamp = format_context_timestamp(action.get("created_at"))
            outcome = "success" if action.get("success") else "failure"
            result_text = str(action.get("result", "")).replace("\n", " ")[:240]
            action_lines.append(
                f"{stamp} | {action.get('tool_name', 'unknown')} | {outcome} | {result_text}"
            )
        bootstrap_sections.append(
            "Recent verified actions (platform records, not assistant claims):\n"
            + "\n".join(action_lines)
        )

    extra_system_sections = [
        str(m.get("content", ""))
        for m in messages[1:]
        if m.get("role") == "system" and str(m.get("content", "")).strip()
    ]
    if extra_system_sections:
        bootstrap_sections.extend(extra_system_sections)
        messages = messages[:1]

    priority_override = await _inject_priority_override(store, deps.group_key)
    bootstrap_body = "\n\n".join(section for section in bootstrap_sections if section.strip())
    if not bootstrap_body:
        bootstrap_body = "No persistent context is currently available."
    persona = config.prompts.persona.strip()
    if persona:
        bootstrap_body += f"\n\n[Persona]\n{persona}\n[/Persona]"
    bootstrap = "[Context Packet]\n" + bootstrap_body + "\n[/Context Packet]"
    messages.append({
        "role": "user",
        "content": bootstrap,
    })

    window_ctx = deps.window.get_context()
    for m in window_ctx:
        role = m["role"]
        content = _with_context_timestamp(str(m["content"]), m.get("created_at"))
        messages.append({"role": role, "content": content})

    messages.append({
        "role": "user",
        "content": _build_runtime_injection(deps, priority_override),
    })
    messages.append({
        "role": "user",
        "content": message,
    })

    return messages


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // ESTIMATE_CHARS_PER_TOKEN)


async def _do_llm_call(messages: list[dict], deps: PipelineDeps) -> LLMResult:
    config = deps.config.model

    if not config.api_key:
        from datetime import datetime
        now = datetime.now().isoformat(timespec="seconds")
        return LLMResult(content=f"[LLM Stub @ {now}] I received: {messages[-1]['content'][:200]}")

    if not config.base_url:
        return LLMResult(content="[Error: model.base_url not configured]")

    tools = deps.registry.to_openai_schema()
    payload: dict[str, Any] = {
        "model": config.model,
        "messages": messages,
        "thinking": {"type": "enabled"},
        "reasoning_effort": config.reasoning_effort,
        "temperature": config.temperature,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    url = f"{config.base_url.rstrip('/')}/chat/completions"
    headers = {
        "Authorization": f"Bearer {config.api_key}",
        "Content-Type": "application/json",
    }

    logger.info("[LLM] calling url=%s model=%s tools=%d effort=%s",
                url, config.model, len(tools), config.reasoning_effort)

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(url, headers=headers, json=payload)
        if resp.status_code != 200:
            return LLMResult(content=f"[Error: LLM API returned {resp.status_code}: {resp.text[:500]}]")

        data = resp.json()
        choice = data.get("choices", [{}])[0]
        msg = choice.get("message", {})
        usage = data.get("usage", {})

        raw_tool_calls = msg.get("tool_calls", [])
        tool_calls = []
        for tc in raw_tool_calls:
            func = tc.get("function", {})
            try:
                parsed_args = json.loads(func.get("arguments", "{}"))
            except json.JSONDecodeError:
                parsed_args = {}
            tool_calls.append({
                "id": tc.get("id", ""),
                "name": func.get("name", ""),
                "arguments": parsed_args,
            })

        prompt_tokens = usage.get("prompt_tokens", 0)
        cache_hit = usage.get("prompt_cache_hit_tokens", 0)
        cache_miss = usage.get("prompt_cache_miss_tokens", prompt_tokens)

        return LLMResult(
            content=msg.get("content", "") or "",
            reasoning_content=msg.get("reasoning_content"),
            tool_calls=tool_calls,
            input_tokens=prompt_tokens,
            output_tokens=usage.get("completion_tokens", 0),
            reasoning_tokens=usage.get("completion_tokens_details", {}).get("reasoning_tokens", 0),
            cache_hit_tokens=cache_hit,
            cache_miss_tokens=cache_miss,
        )


async def _save_msg(deps: PipelineDeps, message: str, category: str, response: str | None) -> None:
    try:
        today = date.today().isoformat()
        content = json.dumps({"user": message, "bot": response}) if response else message
        await deps.store.save(StoredMessage(
            date=today,
            group_key=deps.group_key,
            category=category,
            content=content,
        ))
        logger.info("[PIPE] saved message category=%s response=%s", category, "yes" if response else "no")
    except Exception:
        logger.exception("Failed to save message to store")


def _message_record_content(
    message: str,
    *,
    response: str | None = None,
    status: str = "received",
    source: str = "user",
    input_metadata: dict | None = None,
) -> str:
    payload = {
        "user": message,
        "bot": response,
        "status": status,
        "source": source,
    }
    if input_metadata:
        payload["input_metadata"] = input_metadata
    return json.dumps(payload, ensure_ascii=False)


async def _save_inbound_msg(
    deps: PipelineDeps,
    message: str,
    category: str,
    input_metadata: dict | None = None,
) -> int | None:
    if not deps.remember_input:
        return None
    try:
        today = date.today().isoformat()
        msg_id = await deps.store.save(StoredMessage(
            date=today,
            group_key=deps.group_key,
            category=category,
            content=_message_record_content(
                message,
                source=deps.source,
                input_metadata=input_metadata,
            ),
        ))
        logger.info("[PIPE] saved inbound message category=%s id=%s source=%s", category, msg_id, deps.source)
        return msg_id
    except Exception:
        logger.exception("Failed to save inbound message to store")
        return None


async def _update_saved_msg(
    deps: PipelineDeps,
    msg_id: int | None,
    message: str,
    category: str,
    *,
    response: str | None,
    status: str,
    input_metadata: dict | None = None,
) -> None:
    if not deps.remember_input or msg_id is None:
        return
    try:
        await deps.store.update_message_content(
            msg_id,
            _message_record_content(
                message,
                response=response,
                status=status,
                source=deps.source,
                input_metadata=input_metadata,
            ),
        )
        logger.info("[PIPE] saved message category=%s response=%s", category, "yes" if response else "no")
    except Exception:
        logger.exception("Failed to update saved message")


async def _recover_inbound_msg_id(deps: PipelineDeps, message: str, category: str) -> int | None:
    if not deps.remember_input:
        return None
    try:
        recent = await deps.store.get_messages(group_key=deps.group_key, category=category, limit=5)
    except Exception:
        logger.exception("Failed to recover inbound message id")
        return None
    for item in recent:
        try:
            parsed = json.loads(item.content)
        except (json.JSONDecodeError, TypeError):
            continue
        if (
            isinstance(parsed, dict)
            and parsed.get("user") == message
            and parsed.get("status") == "received"
            and parsed.get("source") == deps.source
        ):
            return item.id
    return None


def _extract_send_artifact(reply_result: str) -> dict | None:
    try:
        parsed = json.loads(reply_result)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None

    artifacts = parsed.get("artifacts", [])
    if not isinstance(artifacts, list):
        return None
    message_id = parsed.get("data", {}).get("message_id") if isinstance(parsed.get("data"), dict) else None
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        if artifact.get("kind") != "sent_image":
            continue
        record = dict(artifact)
        if message_id is not None:
            record["message_id"] = message_id
        markdown = str(record.pop("markdown", ""))
        if markdown:
            record["markdown_sha256"] = hashlib.sha256(markdown.encode("utf-8")).hexdigest()
            record["markdown_chars"] = len(markdown)
        return record
    return None


def _sanitize_action_arguments(value: Any) -> Any:
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if any(secret in lowered for secret in ("api_key", "token", "secret", "password", "authorization")):
                sanitized[key] = "[redacted]"
            elif key == "markdown_image" and isinstance(item, str):
                sanitized["markdown_image_sha256"] = hashlib.sha256(item.encode("utf-8")).hexdigest()
                sanitized["markdown_image_chars"] = len(item)
            else:
                sanitized[key] = _sanitize_action_arguments(item)
        return sanitized
    if isinstance(value, list):
        return [_sanitize_action_arguments(item) for item in value]
    return value


def _is_sensitive_name(name: str) -> bool:
    lowered = name.lower()
    return any(
        secret in lowered
        for secret in ("api_key", "access_token", "session_token", "secret", "password", "authorization")
    )


def _sanitize_action_result(tool_name: str, arguments: dict, result: str) -> str:
    if tool_name == "config_manager" and _is_sensitive_name(str(arguments.get("key", ""))):
        return "[redacted sensitive config result]"
    text = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~-]+", "Bearer [redacted]", result)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "sk-[redacted]", text)
    text = re.sub(
        r'(?i)(["\'](?:api_key|access_token|session_token|secret|password|authorization)["\']\s*:\s*["\'])[^"\']+(["\'])',
        r"\1[redacted]\2",
        text,
    )
    return text[:2000]


async def _record_action(
    deps: PipelineDeps,
    *,
    tool_name: str,
    call_id: str,
    arguments: dict,
    result: str,
    artifact: dict | None = None,
) -> None:
    if not deps.remember_input:
        return
    try:
        await deps.store.save_action(
            group_key=deps.group_key,
            tool_name=tool_name,
            call_id=call_id,
            success=not result.startswith("[Error:"),
            arguments=_sanitize_action_arguments(arguments),
            result=_sanitize_action_result(tool_name, arguments, result),
            artifact=artifact,
        )
    except Exception:
        logger.exception("Failed to persist action ledger entry for %s", tool_name)


def _log_context_size(messages: list[dict], deps: PipelineDeps) -> None:
    total_chars = sum(len(str(m.get("content", ""))) for m in messages)
    total_tokens = total_chars // ESTIMATE_CHARS_PER_TOKEN
    if total_tokens > 15000:
        logger.warning("[CONTEXT] large context: %d msgs, ~%d tokens", len(messages), total_tokens)
    elif total_tokens > 5000:
        logger.info("[CONTEXT] context: %d msgs, ~%d tokens", len(messages), total_tokens)


async def _archive_if_needed(deps: PipelineDeps, user_msg: str, bot_replies: list[str]) -> None:
    config = deps.config
    threshold = config.memory.archive_threshold_tokens

    all_msgs = [("user", user_msg)] + [("bot", r) for r in bot_replies]
    summarizer_cfg = config.summarizer

    for source, text in all_msgs:
        if _estimate_tokens(text) <= threshold:
            continue
        logger.warning("[ARCHIVE] archiving %s msg ~%d tokens", source, _estimate_tokens(text))
        await _generate_and_save_summary(
            deps,
            source,
            text,
            summarizer_cfg,
            kind="message",
        )


async def _generate_and_save_summary(
    deps: PipelineDeps,
    source: str,
    text: str,
    summarizer_cfg,
    *,
    kind: str = "message",
    covered_through_message_id: int | None = None,
) -> bool:
    api_key = summarizer_cfg.api_key or deps.config.model.api_key
    if not api_key:
        logger.warning("[SUMMARY] skipped kind=%s because no API key is configured", kind)
        return False

    base_url = summarizer_cfg.base_url or deps.config.model.base_url
    try:
        max_chars = max(1000, int(summarizer_cfg.max_input_tokens) * ESTIMATE_CHARS_PER_TOKEN)
        chunks = [text[i:i + max_chars] for i in range(0, len(text), max_chars)] or [""]
        if len(chunks) > 1:
            logger.info("[SUMMARY] explicitly chunking input chars=%d chunks=%d", len(text), len(chunks))

        async with httpx.AsyncClient(timeout=30) as client:
            partials: list[str] = []
            for index, chunk in enumerate(chunks, start=1):
                payload = {
                    "model": summarizer_cfg.model,
                    "messages": [
                        {"role": "system", "content": "用1-2句话中文总结以下内容，不超过100字，保留事实、时间和未解决事项。"},
                        {"role": "user", "content": chunk},
                    ],
                    "temperature": summarizer_cfg.temperature,
                    "max_tokens": 150,
                }
                resp = await client.post(
                    f"{base_url.rstrip('/')}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json=payload,
                )
                if resp.status_code != 200:
                    logger.error("[SUMMARY] chunk=%d failed status=%d", index, resp.status_code)
                    return False
                data = resp.json()
                partial = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                if not str(partial).strip():
                    logger.error("[SUMMARY] chunk=%d returned empty content", index)
                    return False
                partials.append(str(partial).strip())

            summary = "\n".join(partials)
            if len(partials) > 1:
                synthesis_payload = {
                    "model": summarizer_cfg.model,
                    "messages": [
                        {"role": "system", "content": "将这些分段摘要合并为简洁、无重复的中文摘要，保留时间顺序。"},
                        {"role": "user", "content": summary},
                    ],
                    "temperature": summarizer_cfg.temperature,
                    "max_tokens": 300,
                }
                resp = await client.post(
                    f"{base_url.rstrip('/')}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json=synthesis_payload,
                )
                if resp.status_code != 200:
                    return False
                summary = str(
                    resp.json().get("choices", [{}])[0].get("message", {}).get("content", "")
                ).strip()
                if not summary:
                    return False

            await deps.store.add_summary(
                deps.group_key,
                source,
                summary,
                kind=kind,
                covered_through_message_id=covered_through_message_id,
            )
            await deps.store.trim_summaries(
                deps.group_key,
                max_count=deps.config.context.summaries_max_count,
                min_count=deps.config.context.summaries_min_count,
            )
            logger.info(
                "[SUMMARY] saved kind=%s covered_through=%s chars=%d",
                kind,
                covered_through_message_id,
                len(summary),
            )
            return True
    except Exception:
        logger.exception("Failed to generate/save summary")
        return False


def _estimate_request_tokens(messages: list[dict], tools: list[dict]) -> int:
    serialized = json.dumps(
        {"messages": messages, "tools": tools},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return _estimate_tokens(serialized)


def _window_turn_groups(items: list[dict]) -> list[list[dict]]:
    groups: list[list[dict]] = []
    group_keys: list[tuple[str, int]] = []
    legacy_turn = 0
    for item in items:
        record_id = item.get("record_id")
        if record_id is not None:
            key = ("record", int(record_id))
        else:
            if item.get("role") == "user" or not groups:
                legacy_turn += 1
            key = ("legacy", legacy_turn)
        if not groups or group_keys[-1] != key:
            groups.append([])
            group_keys.append(key)
        groups[-1].append(item)
    return groups


async def _compact_context_for_request(
    message: str,
    deps: PipelineDeps,
    messages: list[dict],
) -> bool:
    config = deps.config.context
    tools = deps.registry.to_openai_schema()
    estimate = _estimate_request_tokens(messages, tools)
    trigger = max(1, int(config.model_context_tokens * config.compression_trigger_ratio) - config.reserved_output_tokens)
    target = max(1, int(config.model_context_tokens * config.compression_target_ratio) - config.reserved_output_tokens)
    if estimate <= trigger:
        logger.info(
            "[CONTEXT BUDGET] estimated=%d trigger=%d target=%d capacity=%d",
            estimate,
            trigger,
            target,
            config.model_context_tokens,
        )
        return False

    items = list(deps.window)
    groups = _window_turn_groups(items)
    if len(groups) <= 1:
        logger.warning(
            "[CONTEXT BUDGET] over trigger estimated=%d but no complete old turn is compactable",
            estimate,
        )
        return False

    removed_groups: list[list[dict]] = []
    removed_tokens = 0
    for group in groups[:-1]:
        removed_groups.append(group)
        removed_tokens += sum(
            _estimate_tokens(_with_context_timestamp(str(item.get("content", "")), item.get("created_at")))
            for item in group
        )
        if estimate - removed_tokens <= target:
            break

    to_archive = [item for group in removed_groups for item in group]
    kept = [item for group in groups[len(removed_groups):] for item in group]
    record_ids = [int(item["record_id"]) for item in to_archive if item.get("record_id") is not None]
    covered_through = (
        max(record_ids)
        if record_ids and deps.window.coverage_trusted
        else None
    )
    combined = "\n".join(
        f"{_with_context_timestamp(str(item.get('content', '')), item.get('created_at'))}\n"
        f"role: {item.get('role', 'unknown')}"
        for item in to_archive
    )
    saved = await _generate_and_save_summary(
        deps,
        "mixed",
        combined,
        deps.config.summarizer,
        kind="compaction",
        covered_through_message_id=covered_through,
    )
    if not saved:
        logger.error("[CONTEXT BUDGET] compaction summary failed; window left unchanged")
        return False

    deps.window.replace(kept)
    logger.warning(
        "[CONTEXT BUDGET] compacted items=%d->%d estimated=%d target=%d covered_through=%s",
        len(items),
        len(kept),
        estimate,
        target,
        covered_through,
    )
    return True


async def _recycle_window_if_needed(deps: PipelineDeps) -> None:
    messages = await _build_context("", deps)
    await _compact_context_for_request("", deps, messages)


def _split_visible_content(content: str) -> list[str]:
    return [content] if content.strip() else []


async def _send_visible_content(deps: PipelineDeps, content: str) -> list[str]:
    parts = _split_visible_content(content)
    if not parts:
        logger.info("[PIPE] visible content empty after split")
        return []

    if deps.silent:
        logger.info("[PIPE] silent mode suppressed visible content parts=%d chars=%d", len(parts), len(content))
        return parts

    sent_parts: list[str] = []
    for part in parts:
        result = await deps.sender.send(deps.peer, part)
        if not send_succeeded(result):
            error = f"[Error: NapCat send failed: {send_failure_message(result)}]"
            logger.error("[PIPE] visible content send failed: %s", error)
            await _record_action(
                deps,
                tool_name="assistant_content",
                call_id="",
                arguments={"chars": len(part)},
                result=error,
            )
            continue
        log_send(deps, "content", part)
        await _record_action(
            deps,
            tool_name="assistant_content",
            call_id="",
            arguments={"chars": len(part)},
            result=json.dumps(result, ensure_ascii=False),
        )
        sent_parts.append(part)
    logger.info("[PIPE] sent visible content parts=%d chars=%d", len(sent_parts), len(content))
    return sent_parts


async def pipeline(
    message: str,
    msg_type: MessageType,
    image_file: str | None,
    image_url: str | None,
    *,
    deps: PipelineDeps,
) -> None:
    _report_state(deps, "INIT")

    if msg_type == MessageType.MEDIA:
        _report_state(deps, "MEDIA")
        logger.info("[PIPE] branch=media unsupported")
        await deps.sender.send(deps.peer, "暂不支持此消息类型")
        await _save_msg(deps, message, MessageType.MEDIA.value, None)
        return

    _pending_writes: list[tuple[str, str, dict, Callable[[], Awaitable[str]]]] = []
    bot_replies: list[str] = []
    responded = False
    final_status = "received"
    inbound_msg_id: int | None = None
    input_metadata: dict | None = None
    if msg_type == MessageType.IMAGE:
        _report_state(deps, "IMAGE_DESCRIBE")
        logger.info("[PIPE] branch=image describe image_file=%s image_url=%s", bool(image_file), bool(image_url))
        caption = message.strip()
        input_metadata = {
            "kind": "image",
            "caption": caption,
            "image_file": image_file,
            "image_url": image_url,
            "image_description": None,
        }
        inbound_msg_id = await _save_inbound_msg(
            deps,
            message,
            msg_type.value,
            input_metadata,
        )
        try:
            if deps.config.vision.enabled:
                image_description = await describe_image(
                    image_file=image_file,
                    image_url=image_url,
                    config=deps.config,
                )
            else:
                image_description = "Vision provider is not enabled; visual contents are unavailable."
        except asyncio.CancelledError:
            await _update_saved_msg(
                deps,
                inbound_msg_id,
                message,
                msg_type.value,
                response=None,
                status="cancelled",
                input_metadata=input_metadata,
            )
            raise
        except Exception as exc:
            logger.exception("[PIPE] vision provider raised unexpectedly")
            image_description = f"[Error: vision provider failed: {exc}]"
        input_metadata["image_description"] = image_description
        lines = ["The user sent an image."]
        if caption:
            lines.append(f"Caption: {caption}")
        lines.append(f"Vision description: {image_description}")
        if image_file:
            lines.append(f"Image file reference: {image_file}")
        if image_url:
            lines.append(f"Image URL reference: {image_url}")
        message = "\n".join(lines)

    try:
        if inbound_msg_id is None:
            inbound_msg_id = await _save_inbound_msg(deps, message, msg_type.value, input_metadata)
        else:
            await _update_saved_msg(
                deps,
                inbound_msg_id,
                message,
                msg_type.value,
                response=None,
                status="received",
                input_metadata=input_metadata,
            )
        deps.session.mark_pending()

        is_cold = deps.session.is_cold(deps.config.session.timeout)
        if is_cold and not deps.silent:
            _report_state(deps, "POKE")
            logger.info("[PIPE] cold session; sending poke")
            await deps.sender.send_poke(deps.peer)
        deps.session.touch()

        _report_state(deps, "CTX_build")
        messages = await _build_context(message, deps)
        if await _compact_context_for_request(message, deps, messages):
            messages = await _build_context(message, deps)
        _log_context_size(messages, deps)
        logger.info("[PIPE] context built msgs=%d", len(messages))
        send_count = 0
        last_tool_name = ""
        consecutive_fails = 0

        for step in range(MAX_TOOL_STEPS):
            log_context(messages, deps)
            _report_state(deps, f"LOOP_{step + 1}:Pending_LLM")
            start_time = time.monotonic()
            result = await _do_llm_call(messages, deps)
            elapsed = time.monotonic() - start_time
            log_llm_result(deps, result, elapsed)
            logger.info(
                "[PIPE] LLM result step=%d elapsed=%.2fs content=%s tool_calls=%d input=%d output=%d",
                step + 1,
                elapsed,
                "yes" if result.content else "no",
                len(result.tool_calls),
                result.input_tokens,
                result.output_tokens,
            )
            if result.input_tokens:
                logger.info(
                    "[CONTEXT BUDGET] provider_prompt_tokens=%d estimated_request_tokens=%d",
                    result.input_tokens,
                    _estimate_request_tokens(messages, deps.registry.to_openai_schema()),
                )

            if deps.token_counter is not None:
                deps.token_counter["input"] += result.input_tokens
                deps.token_counter["output"] += result.output_tokens
                deps.token_counter["cache_hit"] += result.cache_hit_tokens
                deps.token_counter["cache_miss"] += result.cache_miss_tokens
            if deps.report_llm_health:
                deps.report_llm_health(not result.content.startswith("[Error:"))

            if not result.tool_calls and not result.content:
                logger.info("[PIPE] branch=empty_response responded=%s cold=%s", responded, is_cold)
                if final_status == "received":
                    final_status = "empty"
                break

            if not result.tool_calls and result.content:
                logger.info("[PIPE] branch=content_only chars=%d", len(result.content))
                visible_parts = await _send_visible_content(deps, result.content)
                if visible_parts:
                    responded = True
                    final_status = "responded"
                    bot_replies.extend(visible_parts)
                    await _update_saved_msg(
                        deps,
                        inbound_msg_id,
                        message,
                        msg_type.value,
                        response="\n".join(visible_parts),
                        status=final_status,
                        input_metadata=input_metadata,
                    )
                elif not deps.silent:
                    final_status = "error"
                break

            send_calls = [tc for tc in result.tool_calls if tc["name"] == "send"]
            other_calls = [tc for tc in result.tool_calls if tc["name"] != "send"]
            no_reply_called = any(tc["name"] == NO_REPLY_TOOL for tc in result.tool_calls)
            logger.info(
                "[PIPE] branch=tool_calls send=%d no_reply=%d other=%d",
                len(send_calls),
                1 if no_reply_called else 0,
                len([tc for tc in other_calls if tc["name"] != NO_REPLY_TOOL]),
            )

            tc_results: dict[str, str] = {}
            _report_state(deps, f"LOOP_{step + 1}:Exec_Tools")

            for tc in result.tool_calls:
                logger.info("[PIPE] executing tool name=%s queued=%s", tc["name"], tc["name"] in WRITE_TOOLS)
                tool_name = tc["name"]
                tool_args = tc["arguments"]
                call_id = tc["id"]
                staged = False
                artifact = None

                if tool_name == last_tool_name and consecutive_fails >= 3:
                    tr = (
                        f"[Error: tool '{tool_name}' stopped after three consecutive failures; "
                        "report the failure instead of retrying]"
                    )
                elif tool_name == "send":
                    if send_count >= MAX_SENDS_PER_LOOP:
                        tr = f"[Error: send limit reached: {MAX_SENDS_PER_LOOP}]"
                    elif deps.silent:
                        tr = "[OK] send suppressed by silent pipeline"
                    else:
                        try:
                            tr = await send_tool(
                                tool_args,
                                sender=deps.sender,
                                peer=deps.peer,
                                config=deps.config,
                            )
                        except Exception as e:
                            logger.exception("send_tool failed")
                            tr = f"[Error: {e}]"
                    send_count += 1
                    if not str(tr).startswith("[Error:"):
                        text_reply = str(tool_args.get("text", ""))
                        if text_reply:
                            bot_replies.append(text_reply)
                        artifact = _extract_send_artifact(str(tr))
                        responded = True
                        final_status = "responded"
                    log_send(deps, "tool", tool_args)
                elif tool_name in WRITE_TOOLS and not deps.remember_input:
                    tr = "[OK] write tool suppressed by non-remembering pipeline]"
                elif tool_name in WRITE_TOOLS:
                    _pending_writes.append((
                        tool_name, call_id, tool_args,
                        lambda tn=tool_name, ta=tool_args: deps.registry.execute(
                            tn, ta, store=deps.store, group_key=deps.group_key,
                            config=deps.config, sender=deps.sender, peer=deps.peer,
                        ),
                    ))
                    tr = "[OK] staged for atomic commit during pipeline cleanup"
                    staged = True
                else:
                    try:
                        tr = await deps.registry.execute(
                            tool_name, tool_args,
                            store=deps.store, group_key=deps.group_key,
                            config=deps.config, sender=deps.sender, peer=deps.peer,
                        )
                    except Exception as e:
                        logger.exception("Tool %s failed", tool_name)
                        tr = f"[Error: {e}]"

                tr = str(tr)
                tc_results[call_id] = tr
                is_failure = tr.startswith("[Error:")
                if tool_name != last_tool_name:
                    last_tool_name = tool_name
                    consecutive_fails = 1 if is_failure else 0
                elif is_failure:
                    consecutive_fails += 1
                else:
                    consecutive_fails = 0
                log_tool_call(deps, tool_name, tool_args, tr, queued=staged)
                if not staged:
                    await _record_action(
                        deps,
                        tool_name=tool_name,
                        call_id=call_id,
                        arguments=tool_args,
                        result=tr,
                        artifact=artifact,
                    )

            logger.info("[PIPE] appended %d tool results to context", len(result.tool_calls))
            assistant_msg: dict[str, Any] = {
                "role": "assistant",
                "content": result.content or "",
                "tool_calls": [
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": json.dumps(tc["arguments"], ensure_ascii=False),
                        },
                    }
                    for tc in result.tool_calls
                ],
            }
            if result.reasoning_content:
                assistant_msg["reasoning_content"] = result.reasoning_content
            messages.append(assistant_msg)

            for tc in result.tool_calls:
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": tc_results.get(tc["id"], "[Error: tool did not run]"),
                })

            if no_reply_called:
                logger.info("[PIPE] branch=no_reply suppressing assistant content and ending loop step=%d", step + 1)
                final_status = "no_reply"
                break
        else:
            logger.warning("[LOOP] tool loop exhausted after %d steps, context ~%d msgs",
                           MAX_TOOL_STEPS, len(messages))
            _log_context_size(messages, deps)

        if responded and deps.remember_input:
            deps.window.add(
                user_id=str(deps.peer.peer_uid),
                message=message,
                record_id=inbound_msg_id,
            )
            combined_bot_reply = "\n".join(bot_replies)
            if combined_bot_reply:
                deps.window.add(
                    user_id=str(deps.peer.peer_uid),
                    message=combined_bot_reply,
                    is_bot=True,
                    record_id=inbound_msg_id,
                )
            logger.info("[PIPE] window updated replies=%d window_items=%d", len(bot_replies), len(deps.window))
        elif responded:
            logger.info("[PIPE] response produced; window unchanged for source=%s", deps.source)
        else:
            logger.info("[PIPE] no response produced; window unchanged")

        combined_response = "\n".join(bot_replies) if bot_replies else None
        await _update_saved_msg(
            deps,
            inbound_msg_id,
            message,
            msg_type.value,
            response=combined_response,
            status=final_status,
            input_metadata=input_metadata,
        )

        _report_state(deps, "DONE")

    except asyncio.CancelledError:
        logger.info("[PIPE] cancelled for %s", deps.peer.peer_uid)
        final_status = "cancelled"
        if inbound_msg_id is None:
            inbound_msg_id = await _recover_inbound_msg_id(deps, message, msg_type.value)
        await _update_saved_msg(
            deps,
            inbound_msg_id,
            message,
            msg_type.value,
            response=None,
            status="cancelled",
            input_metadata=input_metadata,
        )
        raise
    except Exception as e:
        logger.exception("[PIPE] error for %s", deps.peer.peer_uid)
        final_status = "error"
        await _update_saved_msg(
            deps,
            inbound_msg_id,
            message,
            msg_type.value,
            response=None,
            status="error",
            input_metadata=input_metadata,
        )
        if not responded:
            await deps.sender.send(deps.peer, f"模型暂时不可用: {e}")
    finally:
        deps.session.clear_pending()
        logger.info("[PIPE] cleanup start pending_writes=%d", len(_pending_writes))

        async def _flush_pending_writes() -> None:
            for tool_name, call_id, tool_args, write_fn in _pending_writes:
                try:
                    result = str(await write_fn())
                    log_tool_call(deps, tool_name, tool_args, result, queued=False)
                except Exception as exc:
                    logger.exception("Post-write failed for %s", tool_name)
                    result = f"[Error: {exc}]"
                await _record_action(
                    deps,
                    tool_name=tool_name,
                    call_id=call_id,
                    arguments=tool_args,
                    result=result,
                )

        flush_task = asyncio.create_task(_flush_pending_writes())
        try:
            await asyncio.shield(flush_task)
        except asyncio.CancelledError:
            await flush_task
            raise

        if deps.remember_input and final_status == "responded":
            try:
                await _archive_if_needed(deps, message, bot_replies)
            except Exception:
                logger.exception("Archive failed")

        logger.info("[PIPE] cleanup complete")
