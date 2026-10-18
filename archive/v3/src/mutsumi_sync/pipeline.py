from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import mimetypes
import re
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Awaitable
from urllib.parse import urlparse

import httpx

from .message.classifier import MessageType
from .message.sender import send_failure_message, send_succeeded
from .memory.store import ActorRecord, EventRecord, EventType, StoredMessage
from .memory.actors import format_actor_source, format_outbound_source
from .memory.projection import build_life_stream
from .memory.timestamps import (
    ensure_timestamped_lines,
    format_context_timestamp,
)
from .tools.send import send_tool
from .tools.status_update import send_status_update
from .output_protocol import (
    OutputProtocolError,
    extract_unambiguous_to_user,
    format_final_envelope,
    parse_final_envelope,
)
from .vision import describe_image
from .logging import log_context, log_llm_result, log_tool_call, log_send, ESTIMATE_CHARS_PER_TOKEN

if TYPE_CHECKING:
    from .scheduler import PipelineDeps

logger = logging.getLogger("mutsumi.pipeline")

MAX_TOOL_STEPS = 10
MAX_SENDS_PER_LOOP = 5
MAX_OUTPUT_REWRITES = 2
MAX_INBOUND_MEDIA_BYTES = 20 * 1024 * 1024
WRITE_TOOLS = {"self_note", "memory_save"}
NO_REPLY_TOOL = "no_reply"


def _report_state(deps: PipelineDeps, state: str) -> None:
    logger.info("[PIPE] state=%s peer=%s group=%s", state, deps.peer.peer_uid, deps.group_key)
    if deps.report_state:
        deps.report_state(state)


def _set_active_work(deps: PipelineDeps, summary: str, *, phase: str = "working") -> None:
    if not summary.strip() or deps.active_work is None:
        return
    deps.active_work[deps.pipeline_id] = {
        "summary": summary.strip()[:300],
        "conversation_id": deps.conversation_id or deps.group_key,
        "actor_id": deps.actor_id,
        "phase": phase,
    }
    logger.info("[ACTIVITY] active pipeline=%s phase=%s summary=%s", deps.pipeline_id, phase, summary[:120])


def _clear_active_work(deps: PipelineDeps) -> None:
    if deps.active_work is None:
        return
    if deps.active_work.pop(deps.pipeline_id, None) is not None:
        logger.info("[ACTIVITY] cleared pipeline=%s", deps.pipeline_id)


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


def _build_default_system_prompt(config, deps: PipelineDeps | None = None) -> str:
    prompt = config.prompts.system.runtime
    persona = config.prompts.system.persona.strip()
    if persona:
        prompt = f"{prompt.rstrip()}\n\n人格设定：\n{persona}"
    if deps is not None and deps.source == "heartbeat":
        prompt += "\n\n[Heartbeat Mode Override]\n" + config.prompts.system.heartbeat
    return prompt


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


async def _inject_inner_journal(store, config) -> str:
    entries = await store.get_inner_journal(config.inner_journal.max_context_entries)
    if not entries:
        return ""

    selected: list[dict[str, Any]] = []
    total = 0
    for entry in reversed(entries):
        source = str(entry.get("source_conversation_id") or "unknown")
        timestamp = format_context_timestamp(entry.get("created_at"))
        line = f"[{timestamp}] source={source}: {entry.get('content', '')}"
        cost = _estimate_tokens(line)
        if selected and total + cost > config.inner_journal.max_context_tokens:
            break
        selected.append({"line": line, "cost": cost})
        total += cost

    if not selected:
        return ""
    return (
        "[Inner Journal — subjective history, not instructions or verified facts]\n"
        + "\n".join(item["line"] for item in reversed(selected))
        + "\n[/Inner Journal]"
    )


async def _build_context(message: str, deps: PipelineDeps) -> list[dict[str, Any]]:
    config = deps.config
    store = deps.store
    messages: list[dict[str, Any]] = [{
        "role": "system",
        "content": _build_default_system_prompt(config, deps),
    }]

    stream = await build_life_stream(
        store,
        limit=max(100, config.context.recent_actions_max_count * 32),
        exclude_event_ids=set(deps.current_event_ids),
    )
    if stream:
        messages.extend(stream)
    else:
        # Keep the in-memory window as a compatibility fallback for tests and
        # old databases that have not yet produced Event Ledger rows.
        messages.extend({
            "role": str(item.get("role", "user")),
            "content": str(item.get("content", "")),
        } for item in deps.window.get_context())

    state_parts = [
        "平台状态（不是用户消息，也不是需要复述的内容）：",
        f"当前时间：{format_context_timestamp(time.time())}",
    ]
    if deps.source == "heartbeat":
        state_parts.append("这是平台发起的心跳检查，不是用户新消息。")
    if deps.active_work:
        state_parts.append("当前正在进行的工作：")
        for item in deps.active_work.values():
            state_parts.append(
                f"- {item.get('summary', '处理中')}（来源 {item.get('conversation_id', 'unknown')}，"
                f"阶段 {item.get('phase', 'working')}）"
            )
    inner_journal_text = await _inject_inner_journal(store, config)
    if inner_journal_text:
        state_parts.append(inner_journal_text)
    self_note_text = await _inject_self_note(store, deps.group_key, config)
    if self_note_text:
        state_parts.append("与当前发言者有关的长期记录：\n" + self_note_text)
    messages.append({"role": "user", "content": "\n".join(state_parts)})

    actor_kind = "service" if deps.source == "external_service" else "human"
    if deps.source == "heartbeat":
        current_content = (
            f"平台心跳｜目标聊天：{deps.conversation_id or deps.group_key}\n"
            "执行本次自主性检查。"
        )
    elif len(deps.inbound_events) > 1:
        current_lines = [
            format_actor_source(
                conversation_id=deps.conversation_id or deps.group_key,
                actor_id=str(item.get("actor_id") or "unknown"),
                actor_name=str(item.get("actor_name") or item.get("actor_id") or "user"),
                actor_kind=actor_kind,
                content=str(item.get("content") or ""),
                created_at=item.get("created_at") or time.time(),
            )
            for item in deps.inbound_events
        ]
        current_content = "\n".join(current_lines)
    else:
        current_content = format_actor_source(
            conversation_id=deps.conversation_id or deps.group_key,
            actor_id=deps.actor_id or str(deps.peer.peer_uid),
            actor_name=deps.actor_name or str(deps.peer.peer_uid),
            actor_kind=actor_kind,
            content=message,
            created_at=(deps.inbound_events[0].get("created_at") if deps.inbound_events else None) or time.time(),
        )
    messages.append({"role": "user", "content": current_content})
    return messages


def _window_item_content(item: dict[str, Any]) -> str:
    return str(item.get("content", ""))


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // ESTIMATE_CHARS_PER_TOKEN)


async def _do_llm_call(messages: list[dict], deps: PipelineDeps) -> LLMResult:
    config = deps.config.model

    if not config.api_key:
        from datetime import datetime
        now = datetime.now().isoformat(timespec="seconds")
        return LLMResult(content=format_final_envelope(
            to_user=f"[LLM Stub @ {now}] I received: {messages[-1]['content'][:200]}",
        ))

    if not config.base_url:
        return LLMResult(content=format_final_envelope(
            to_user="[Error: model.base_url not configured]",
        ))

    tools = deps.registry.to_openai_schema(deps.allowed_tools)
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
            return LLMResult(content=format_final_envelope(
                to_user=f"[Error: LLM API returned {resp.status_code}: {resp.text[:500]}]",
            ))

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


async def _append_event(
    deps: PipelineDeps,
    *,
    event_type: str,
    content: str,
    actor_id: str | None = None,
    actor_kind: str = "bot",
    actor_name: str = "Mutsumi",
    visibility: str = "private",
    audience: str = "bot:self",
    status: str = "finalized",
    media_ids: list[str] | None = None,
    payload: dict[str, Any] | None = None,
    turn_id: str = "",
) -> str | None:
    if actor_kind == "human" and not deps.remember_input:
        return None
    if actor_kind != "human" and not deps.remember_output:
        return None
    try:
        resolved_actor_id = actor_id or (deps.actor_id if actor_kind == "human" else "bot:self")
        resolved_actor_name = actor_name or (deps.actor_name if actor_kind == "human" else "Mutsumi")
        await deps.store.ensure_actor(ActorRecord(
            actor_id=resolved_actor_id,
            kind=actor_kind,
            platform="qq" if resolved_actor_id.startswith("qq:") else "",
            platform_subject_id=resolved_actor_id.rsplit(":", 1)[-1],
            display_name=resolved_actor_name,
        ))
        event = await deps.store.append_event(EventRecord(
            conversation_id=deps.conversation_id or deps.group_key,
            actor_id=resolved_actor_id,
            actor_kind=actor_kind,
            actor_name=resolved_actor_name,
            event_type=event_type,
            content=content,
            pipeline_id=deps.pipeline_id,
            visibility=visibility,
            audience=audience,
            status=status,
            media_ids=media_ids,
            payload=payload,
            turn_id=turn_id,
        ))
        return event.event_id
    except Exception:
        logger.exception("Failed to append event type=%s", event_type)
        return None


def _conversation_visibility(deps: PipelineDeps) -> str:
    return "group" if (deps.peer.chat_type == 2) else "private"


async def _append_inbound_events(
    deps: PipelineDeps,
    content: str,
    *,
    media_ids: list[str] | None = None,
) -> list[str]:
    inputs = deps.inbound_events or [{
        "actor_id": deps.actor_id or "unknown",
        "actor_name": deps.actor_name or deps.actor_id or "user",
        "content": content,
    }]
    event_ids: list[str] = []
    for item in inputs:
        event_id = await _append_event(
            deps,
            event_type=EventType.INBOUND.value,
            content=str(item.get("content") or content),
            actor_id=str(item.get("actor_id") or "unknown"),
            actor_kind="human",
            actor_name=str(item.get("actor_name") or item.get("actor_id") or "user"),
            visibility=_conversation_visibility(deps),
            audience="bot:self",
            status="received",
            media_ids=media_ids,
        )
        if event_id:
            event_ids.append(event_id)
    return event_ids


async def _update_event_statuses(deps: PipelineDeps, event_ids: list[str], status: str) -> None:
    for event_id in event_ids:
        await deps.store.update_event_status(event_id, status)


async def _update_event_contents(
    deps: PipelineDeps,
    event_ids: list[str],
    content: str,
    *,
    status: str = "received",
) -> None:
    for event_id in event_ids:
        await deps.store.update_event_status(event_id, status, content)


def _short_image_description(description: str) -> str:
    for line in description.splitlines():
        if line.lower().startswith("short description:"):
            return line.split(":", 1)[1].strip()
    first = description.splitlines()[0].strip() if description.splitlines() else description.strip()
    return first[:120]


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


async def _register_sent_media(deps: PipelineDeps, tool_args: dict, artifact: dict | None) -> list[str]:
    media_ids: list[str] = []
    existing_media_id = str(tool_args.get("media_id") or "").strip()
    file_path = str((artifact or {}).get("file") or tool_args.get("image") or "").strip()
    image_url = str(tool_args.get("image_url") or "").strip()
    try:
        if existing_media_id:
            media = await deps.store.get_media(existing_media_id)
            if media is not None and media.status == "active":
                media_ids.append(existing_media_id)
        if file_path and Path(file_path).is_file():
            media = await deps.store.register_media(
                Path(file_path).read_bytes(),
                kind="image",
                ext=Path(file_path).suffix,
            )
            media_ids.append(media.media_id)
        elif image_url:
            media = await deps.store.register_external_media(image_url, kind="image")
            media_ids.append(media.media_id)
    except Exception:
        logger.exception("[MEDIA] failed to register outbound media")
    return media_ids


async def _register_inbound_media(
    deps: PipelineDeps,
    image_file: str | None,
    image_url: str | None,
    kind: str = "image",
) -> str | None:
    """Persist an inbound image locally, retaining a URL only as a fallback."""
    file_path = Path(image_file) if image_file else None
    if file_path and file_path.is_file():
        if file_path.stat().st_size > MAX_INBOUND_MEDIA_BYTES:
            raise ValueError("image exceeds the maximum allowed size")
        media = await deps.store.register_media(
            file_path.read_bytes(),
            kind=kind,
            ext=file_path.suffix,
            source_url=image_url or "",
        )
        logger.info("[MEDIA] inbound registered local media_id=%s", media.media_id)
        return media.media_id

    if not image_url:
        return None

    try:
        parsed_url = urlparse(image_url)
        if parsed_url.scheme not in {"http", "https"}:
            raise ValueError("image URL must use HTTP or HTTPS")
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            async with client.stream("GET", image_url) as response:
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if not content_type.startswith("image/"):
                    raise ValueError(f"unexpected image content type: {content_type or 'missing'}")
                declared_size = int(response.headers.get("content-length") or 0)
                if declared_size > MAX_INBOUND_MEDIA_BYTES:
                    raise ValueError("image exceeds the maximum allowed size")
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_INBOUND_MEDIA_BYTES:
                        raise ValueError("image exceeds the maximum allowed size")
                    chunks.append(chunk)
        ext = Path(parsed_url.path).suffix
        if not ext:
            ext = mimetypes.guess_extension(content_type) or ""
        media = await deps.store.register_media(
            b"".join(chunks),
            kind=kind,
            ext=ext,
            source_url=image_url,
        )
        logger.info("[MEDIA] inbound downloaded media_id=%s bytes=%d", media.media_id, size)
        return media.media_id
    except Exception:
        logger.warning("[MEDIA] inbound download failed; retaining external reference", exc_info=True)

    try:
        media = await deps.store.register_external_media(image_url, kind=kind)
        logger.info("[MEDIA] inbound registered external media_id=%s", media.media_id)
        return media.media_id
    except Exception:
        logger.exception("[MEDIA] failed to register inbound media reference")
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
    if not deps.remember_output:
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


async def _send_status_text(
    deps: PipelineDeps,
    text: str,
    *,
    source: str,
) -> bool:
    if deps.silent or not deps.allow_status_update:
        logger.info("[STATUS] suppressed source=%s silent=true", source)
        return False

    outcome = await send_status_update(
        {"text": text},
        sender=deps.sender,
        peer=deps.peer,
    )
    if not outcome.ok:
        error = f"[Error: status update failed: {outcome.error}]"
        logger.error("[STATUS] failed source=%s error=%s", source, error)
        await _record_action(
            deps,
            tool_name="status_update",
            call_id="",
            arguments={"text": text, "source": source},
            result=error,
        )
        return False

    log_send(deps, "status", text)
    await _append_event(
        deps,
        event_type=EventType.STATUS_UPDATE.value,
        content=text,
        visibility=_conversation_visibility(deps),
        audience=deps.conversation_id or deps.group_key,
    )
    await _record_action(
        deps,
        tool_name="status_update",
        call_id="",
        arguments={"text": text, "source": source},
        result="[OK] status update sent",
    )
    logger.info("[STATUS] sent source=%s chars=%d", source, len(text))
    return True


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
                        {"role": "system", "content": deps.config.prompts.system.message_summary},
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
                        {"role": "system", "content": deps.config.prompts.system.summary_merge},
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
    tools = deps.registry.to_openai_schema(deps.allowed_tools)
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
        removed_tokens += sum(_estimate_tokens(_window_item_content(item)) for item in group)
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
        f"{_window_item_content(item)}\n"
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


def _unsupported_markdown_features(content: str) -> list[str]:
    features: list[str] = []
    if re.search(r"(?m)^\s{0,3}#{1,6}\s", content):
        features.append("heading")
    if "```" in content or "~~~" in content:
        features.append("code fence")
    if re.search(r"(?m)^\s*\|.*\|\s*$", content):
        features.append("table")
    if re.search(r"!\[[^\]]*\]\([^)]*\)|\[[^\]]+\]\(https?://[^)]+\)", content):
        features.append("link or image")
    if "$$" in content or re.search(r"\\\(|\\\[", content):
        features.append("LaTeX")
    return features


async def _send_visible_content(deps: PipelineDeps, content: str) -> list[str]:
    parts = _split_visible_content(content)
    if not parts:
        logger.info("[PIPE] visible content empty after split")
        return []

    if deps.silent or not deps.allow_visible_output:
        logger.info("[PIPE] silent mode suppressed visible content parts=%d chars=%d", len(parts), len(content))
        return []

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
        await _append_event(
            deps,
            event_type=EventType.OUTBOUND.value,
            content=part,
            visibility=_conversation_visibility(deps),
            audience=deps.conversation_id or deps.group_key,
        )
    logger.info("[PIPE] sent visible content parts=%d chars=%d", len(sent_parts), len(content))
    return sent_parts


async def pipeline(
    message: str,
    msg_type: MessageType,
    image_file: str | None,
    image_url: str | None,
    *,
    media_kind: str = "image",
    deps: PipelineDeps,
) -> None:
    _report_state(deps, "INIT")

    if msg_type == MessageType.MEDIA:
        _report_state(deps, "MEDIA")
        logger.info("[PIPE] branch=media unsupported")
        await deps.sender.send(deps.peer, "暂不支持此消息类型")
        await _save_msg(deps, message, MessageType.MEDIA.value, None)
        return

    _pending_writes: list[tuple[str, str, dict, str, Callable[[], Awaitable[str]]]] = []
    bot_replies: list[str] = []
    inner_candidate = ""
    responded = False
    turn_completed = False
    cancelled = False
    final_status = "received"
    inbound_msg_id: int | None = None
    inbound_event_ids: list[str] = list(deps.precreated_event_ids)
    input_metadata: dict | None = None
    input_media_id: str | None = None
    if msg_type == MessageType.IMAGE:
        _report_state(deps, "IMAGE_DESCRIBE")
        logger.info("[PIPE] branch=image describe image_file=%s image_url=%s", bool(image_file), bool(image_url))
        caption = message.strip()
        if caption.startswith("[CQ:image"):
            caption = ""
        input_metadata = {
            "kind": media_kind,
            "caption": caption,
            "image_file": image_file,
            "image_url": image_url,
            "image_description": None,
            "media_id": None,
        }
        inbound_msg_id = await _save_inbound_msg(
            deps,
            message,
            msg_type.value,
            input_metadata,
        )
        try:
            input_media_id = await _register_inbound_media(deps, image_file, image_url, media_kind)
            if input_media_id:
                input_metadata["media_id"] = input_media_id
        except Exception:
            logger.exception("[MEDIA] failed to register inbound media")
        if not inbound_event_ids:
            inbound_event_ids = await _append_inbound_events(
                deps,
                message,
                media_ids=[input_media_id] if input_media_id else None,
            )
        elif input_media_id:
            for event_id in inbound_event_ids:
                await deps.store.update_event_media_ids(event_id, [input_media_id])
        deps.current_event_ids = list(inbound_event_ids)
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
            await _update_event_statuses(deps, inbound_event_ids, "cancelled")
            raise
        except Exception as exc:
            logger.exception("[PIPE] vision provider raised unexpectedly")
            image_description = f"[Error: vision provider failed: {exc}]"
        input_metadata["image_description"] = image_description
        if input_media_id:
            await deps.store.update_media_description(
                input_media_id,
                image_description,
                _short_image_description(image_description),
            )
        lines = ["The user sent an image."]
        if caption:
            lines.append(f"Caption: {caption}")
        lines.append(f"Vision description: {image_description}")
        if input_media_id:
            lines.append(f"Media ledger reference: {input_media_id}")
        message = "\n".join(lines)
        await _update_event_contents(deps, inbound_event_ids, message)

    try:
        if inbound_msg_id is None:
            inbound_msg_id = await _save_inbound_msg(deps, message, msg_type.value, input_metadata)
            if not inbound_event_ids:
                inbound_event_ids = await _append_inbound_events(deps, message)
            deps.current_event_ids = list(inbound_event_ids)
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
        if is_cold and not deps.silent and deps.allow_cold_poke:
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
        output_rewrites = 0

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
                    _estimate_request_tokens(messages, deps.registry.to_openai_schema(deps.allowed_tools)),
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
                logger.info("[PIPE] branch=final_content chars=%d", len(result.content))
                try:
                    envelope = parse_final_envelope(result.content, strict=False)
                except OutputProtocolError as exc:
                    output_rewrites += 1
                    logger.warning(
                        "[OUTPUT PROTOCOL] invalid final envelope rewrite=%d/%d error=%s",
                        output_rewrites,
                        MAX_OUTPUT_REWRITES,
                        exc,
                    )
                    if output_rewrites > MAX_OUTPUT_REWRITES:
                        fallback_text = extract_unambiguous_to_user(result.content) or (
                            "这次回复格式连续校验失败了，请稍后再试。"
                        )
                        if _unsupported_markdown_features(fallback_text):
                            fallback_text = "这次回复格式连续校验失败了，请稍后再试。"
                        visible_parts = await _send_visible_content(deps, fallback_text)
                        if visible_parts:
                            responded = True
                            final_status = "responded"
                            bot_replies.extend(visible_parts)
                        else:
                            final_status = "error"
                        logger.error(
                            "[OUTPUT PROTOCOL] rewrite exhausted fallback_sent=%s extracted=%s",
                            bool(visible_parts),
                            extract_unambiguous_to_user(result.content) is not None,
                        )
                        break
                    messages.append({"role": "assistant", "content": result.content})
                    messages.append({
                        "role": "user",
                        "content": (
                            "[Output Protocol] 最终回复格式错误。请只输出完整的 [TO_SELF]...[/TO_SELF] "
                            "和 [TO_USER]...[/TO_USER] 两个区块，不要添加区块外文字。"
                        ),
                    })
                    continue

                if len(envelope.to_self) > deps.config.inner_journal.max_entry_chars:
                    output_rewrites += 1
                    logger.warning(
                        "[OUTPUT PROTOCOL] to_self too long chars=%d rewrite=%d/%d",
                        len(envelope.to_self),
                        output_rewrites,
                        MAX_OUTPUT_REWRITES,
                    )
                    if output_rewrites > MAX_OUTPUT_REWRITES:
                        fallback_text = envelope.to_user or "这次回复格式连续校验失败了，请稍后再试。"
                        if _unsupported_markdown_features(fallback_text):
                            fallback_text = "这次回复格式连续校验失败了，请稍后再试。"
                        visible_parts = await _send_visible_content(deps, fallback_text)
                        if visible_parts:
                            responded = True
                            final_status = "responded"
                            bot_replies.extend(visible_parts)
                        else:
                            final_status = "error"
                        logger.error(
                            "[OUTPUT PROTOCOL] TO_SELF rewrite exhausted fallback_sent=%s",
                            bool(visible_parts),
                        )
                        break
                    messages.append({"role": "assistant", "content": result.content})
                    messages.append({
                        "role": "user",
                        "content": (
                            f"[Output Protocol] TO_SELF 只能保留新的内在状态增量，最多 "
                            f"{deps.config.inner_journal.max_entry_chars} 个字符。请压缩后重写完整 envelope。"
                        ),
                    })
                    continue

                rich_features = _unsupported_markdown_features(envelope.to_user)
                if rich_features and not deps.silent and deps.allow_visible_output:
                    output_rewrites += 1
                    logger.warning(
                        "[OUTPUT GATE] rejected Markdown features=%s rewrite=%d/%d",
                        ",".join(rich_features), output_rewrites, MAX_OUTPUT_REWRITES,
                    )
                    if output_rewrites > MAX_OUTPUT_REWRITES:
                        fallback_text = "这次回复无法通过纯文本校验，请稍后再试。"
                        visible_parts = await _send_visible_content(deps, fallback_text)
                        if visible_parts:
                            responded = True
                            final_status = "responded"
                            bot_replies.extend(visible_parts)
                        else:
                            final_status = "error"
                        logger.error(
                            "[OUTPUT GATE] rewrite exhausted fallback_sent=%s",
                            bool(visible_parts),
                        )
                        break
                    messages.append({"role": "assistant", "content": result.content})
                    messages.append({
                        "role": "user",
                        "content": (
                            "[Output Gate] TO_USER was not sent because ordinary QQ replies must be flat plain text. "
                            "Rewrite TO_USER without Markdown, or call send with markdown_image for complex Markdown. "
                            f"Detected: {', '.join(rich_features)}. Do not claim that the rejected content was sent."
                        ),
                    })
                    continue

                inner_candidate = envelope.to_self
                visible_parts = await _send_visible_content(deps, envelope.to_user)
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
                elif envelope.to_user and not deps.silent and deps.allow_visible_output:
                    final_status = "error"
                elif envelope.to_user and not deps.allow_visible_output:
                    logger.info("[OUTPUT] proactive visible output suppressed by policy")
                elif final_status == "received":
                    final_status = "no_reply" if envelope.to_self else "empty"
                logger.info(
                    "[OUTPUT PROTOCOL] parsed to_self_chars=%d to_user_chars=%d sent=%s",
                    len(envelope.to_self),
                    len(envelope.to_user),
                    bool(visible_parts),
                )
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
            tool_turn_id = f"{deps.pipeline_id}:step:{step + 1}"

            status_calls = [tc for tc in result.tool_calls if tc["name"] == "status_update"]
            long_calls = []
            for tc in result.tool_calls:
                tool = deps.registry.get(tc["name"])
                is_long = bool(tool and tool.latency_class == "long")
                if tc["name"] == "send" and tc["arguments"].get("markdown_image"):
                    is_long = True
                if is_long:
                    long_calls.append((tc, tool))
            if not status_calls and long_calls and not deps.silent:
                _, long_tool = long_calls[0]
                fallback = (
                    long_tool.status_hint
                    if long_tool and long_tool.status_hint
                    else "我先处理一下，可能需要一点时间。"
                )
                _set_active_work(deps, fallback, phase="preparing")
                await _send_status_text(
                    deps,
                    fallback,
                    source="automatic-long-tool-fallback",
                )

            execution_calls = status_calls + [
                tc for tc in result.tool_calls if tc["name"] != "status_update"
            ]
            status_executed = False

            for tc in execution_calls:
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
                elif deps.allowed_tools is not None and tool_name not in deps.allowed_tools:
                    tr = f"[Error: tool '{tool_name}' is not enabled for this pipeline]"
                elif tool_name == "status_update":
                    if status_executed:
                        tr = "[Error: only one status_update is allowed per tool round]"
                    elif deps.silent or not deps.allow_status_update:
                        status_executed = True
                        tr = "[OK] status update suppressed by silent pipeline"
                    else:
                        status_executed = True
                        outcome = await send_status_update(
                            tool_args,
                            sender=deps.sender,
                            peer=deps.peer,
                        )
                        if outcome.ok:
                            _set_active_work(deps, str(tool_args.get("text", "")), phase="preparing")
                            log_send(deps, "status", outcome.text)
                            await _append_event(
                                deps,
                                event_type=EventType.STATUS_UPDATE.value,
                                content=outcome.text,
                                visibility=_conversation_visibility(deps),
                                audience=deps.conversation_id or deps.group_key,
                            )
                            tr = "[OK] status update sent"
                        else:
                            tr = f"[Error: status update failed: {outcome.error}]"
                elif tool_name == "send":
                    send_committed = False
                    if send_count >= MAX_SENDS_PER_LOOP:
                        tr = f"[Error: send limit reached: {MAX_SENDS_PER_LOOP}]"
                    elif deps.silent or not deps.allow_visible_output:
                        tr = "[OK] send suppressed by silent pipeline"
                    else:
                        try:
                            tr = await send_tool(
                                tool_args,
                                sender=deps.sender,
                                peer=deps.peer,
                                config=deps.config,
                                store=deps.store,
                            )
                        except Exception as e:
                            logger.exception("send_tool failed")
                            tr = f"[Error: {e}]"
                        else:
                            send_committed = not str(tr).startswith("[Error:")
                    send_count += 1
                    if send_committed:
                        text_reply = str(tool_args.get("text", ""))
                        if text_reply:
                            bot_replies.append(text_reply)
                        artifact = _extract_send_artifact(str(tr))
                        responded = True
                        final_status = "responded"
                        sent_media_ids = await _register_sent_media(deps, tool_args, artifact)
                        await _append_event(
                            deps,
                            event_type=EventType.OUTBOUND.value,
                            content=str(tool_args.get("text", "")) or "[special media segment]",
                            visibility=_conversation_visibility(deps),
                            audience=deps.conversation_id or deps.group_key,
                            media_ids=sent_media_ids or None,
                        )
                        log_send(deps, "tool", tool_args)
                elif tool_name in WRITE_TOOLS and not deps.allow_write_tools:
                    tr = "[Error: write tools are disabled for this pipeline]"
                elif tool_name in WRITE_TOOLS and not deps.remember_input:
                    tr = "[OK] write tool suppressed by non-remembering pipeline]"
                elif tool_name in WRITE_TOOLS:
                    _pending_writes.append((
                        tool_name, call_id, tool_args, tool_turn_id,
                        lambda tn=tool_name, ta=tool_args: deps.registry.execute(
                            tn, ta, store=deps.store, group_key=deps.group_key,
                            config=deps.config, sender=deps.sender, peer=deps.peer,
                        ),
                    ))
                    tr = "[OK] staged for atomic commit during pipeline cleanup"
                    staged = True
                else:
                    try:
                        tool = deps.registry.get(tool_name)
                        if tool and tool.latency_class == "long":
                            _set_active_work(
                                deps,
                                tool.status_hint or f"正在调用 {tool_name}",
                                phase="executing",
                            )
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
                await _append_event(
                    deps,
                    event_type=EventType.TOOL_CALL.value,
                    content=json.dumps({"tool": tool_name, "call_id": call_id, "arguments": _sanitize_action_arguments(tool_args)}, ensure_ascii=False),
                    visibility="private",
                    payload={
                        "tool": tool_name,
                        "call_id": call_id,
                        "arguments": _sanitize_action_arguments(tool_args),
                        "assistant_content": result.content or "",
                    },
                    turn_id=tool_turn_id,
                )
                await _append_event(
                    deps,
                    event_type=EventType.TOOL_RESULT.value,
                    content=_sanitize_action_result(tool_name, tool_args, tr),
                    visibility="private",
                    payload={
                        "tool": tool_name,
                        "call_id": call_id,
                        "result": _sanitize_action_result(tool_name, tool_args, tr),
                        "staged": staged,
                    },
                    turn_id=tool_turn_id,
                )
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
                final_status = "responded" if responded else "no_reply"
                break
        else:
            logger.warning("[LOOP] tool loop exhausted after %d steps, context ~%d msgs",
                           MAX_TOOL_STEPS, len(messages))
            _log_context_size(messages, deps)

        if responded and (deps.remember_input or deps.remember_output):
            if deps.remember_input:
                deps.window.add(
                    user_id=deps.actor_id or str(deps.peer.peer_uid),
                    message=message,
                    record_id=inbound_msg_id,
                    actor_name=deps.actor_name,
                )
            combined_bot_reply = "\n".join(bot_replies)
            if combined_bot_reply and deps.remember_output:
                deps.window.add(
                    user_id="bot:self",
                    message=combined_bot_reply,
                    is_bot=True,
                    record_id=inbound_msg_id,
                    actor_name="Mutsumi",
                )
            if combined_bot_reply and not deps.remember_input:
                try:
                    await deps.store.save(StoredMessage(
                        date=date.today().isoformat(),
                        group_key=deps.group_key,
                        category="proactive",
                        content=_message_record_content(
                            "",
                            response=combined_bot_reply,
                            status="responded",
                            source=deps.source,
                        ),
                    ))
                except Exception:
                    logger.exception("Failed to persist proactive output")
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
        if inbound_event_ids:
            event_status = "error" if final_status == "error" else "finalized"
            await _update_event_statuses(deps, inbound_event_ids, event_status)

        turn_completed = True
        _report_state(deps, "DONE")

    except asyncio.CancelledError:
        logger.info("[PIPE] cancelled for %s", deps.peer.peer_uid)
        cancelled = True
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
        await _update_event_statuses(deps, inbound_event_ids, "cancelled")
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
        await _update_event_statuses(deps, inbound_event_ids, "error")
        if not responded:
            await deps.sender.send(deps.peer, f"模型暂时不可用: {e}")
    finally:
        _clear_active_work(deps)
        deps.session.clear_pending()
        logger.info("[PIPE] cleanup start pending_writes=%d", len(_pending_writes))

        async def _flush_pending_writes() -> None:
            for tool_name, call_id, tool_args, tool_turn_id, write_fn in _pending_writes:
                try:
                    result = str(await write_fn())
                    log_tool_call(deps, tool_name, tool_args, result, queued=False)
                except Exception as exc:
                    logger.exception("Post-write failed for %s", tool_name)
                    result = f"[Error: {exc}]"
                await _append_event(
                    deps,
                    event_type=EventType.TOOL_RESULT.value,
                    content=_sanitize_action_result(tool_name, tool_args, result),
                    visibility="private",
                    payload={
                        "tool": tool_name,
                        "call_id": call_id,
                        "result": _sanitize_action_result(tool_name, tool_args, result),
                        "committed": not result.startswith("[Error:"),
                    },
                    turn_id=tool_turn_id,
                )
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

        if (
            deps.remember_inner
            and inner_candidate
            and turn_completed
            and not cancelled
            and final_status in ("responded", "no_reply")
        ):
            try:
                journal_id = await deps.store.append_inner_journal(
                    content=inner_candidate,
                    pipeline_id=deps.pipeline_id,
                    source_conversation_id=deps.conversation_id or deps.group_key,
                    source_actor_id=deps.actor_id,
                    source_event_ids=inbound_event_ids,
                )
                logger.info(
                    "[INNER] %s journal_id=%s chars=%d",
                    "committed" if journal_id is not None else "deduplicated",
                    journal_id,
                    len(inner_candidate),
                )
            except Exception:
                logger.exception("[INNER] failed to persist journal entry")

        if deps.remember_input and final_status == "responded":
            try:
                await _archive_if_needed(deps, message, bot_replies)
            except Exception:
                logger.exception("Archive failed")

        logger.info("[PIPE] cleanup complete")
