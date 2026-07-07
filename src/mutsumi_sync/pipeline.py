from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING, Any, Callable, Awaitable

import httpx

from .message.classifier import MessageType
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
    system = config.system_prompt or "You are a helpful assistant."
    system += (
        "\n当前平台是由Mutsumi's SYNC构建的，基于napcat-QQ bot的虚拟社交对话平台。" 
    )
    system += (
        "\n你拥有一个 [私人印象] 空间用于维护对用户的私人印象和关键信息。"
        "\n[私人印象] 标签标注了当前长度与目标上限。"
        "\n请保持内容精炼，优先保留长期价值高的信息。"
        "\n使用 tool 维护和整理印象。"
        "\n如果同一个工具调用连续失败 3 次以上，停止重试并向用户汇报失败原因。"
    )
    system += (
        "\n[输出协议]"
        "\nassistant content 是用户可见回复，系统只会发送最终一轮没有 tool_calls 的 content。"
        "\n如果你想发送多条 QQ 消息，用未转义的 | 分隔每条消息；如果正文里需要字面量竖线，写成 \\|。"
        "\n只有当你想分条发送时才使用未转义的 |；Markdown 表格、代码、正则和命令中的 | 必须转义或避免使用分条。"
        "\n不要为了普通文字回复调用 send 工具。工具只用于记忆、配置、查询、外部 API、特殊消息段或静默控制。"
        "\nsend 工具保留给 image、markdown_image、face、at、reply、forward 等特殊发送场景。"
        "\n如果本轮不应回复用户，调用 no_reply 工具，并保持 content 为空。"
        "\nreasoning_content 永远不会发送给用户。"
        "\n回复需要合理、得体、简洁，尽可能符合人类在社交平台聊天的规律。"
    )
    system += (
        "\n[Context Protocol]"
        "\nThe API message list uses one empty system message only. All platform instructions, summaries, self notes, and memory blocks are packed into the first user message."
        "\nThat first bootstrap user message is context, not a fresh user request. Later user/assistant turns are the working conversation window."
        "\nEvery user-role message may end with [Priority Override]. Treat it as higher priority than ordinary memory and keep paying attention to it."
        "\nHeartbeat messages are silent health checks. They must not create durable memories and should not produce visible chat output."
        "\nImage messages may be described by a configured vision API provider; preserve visible text, formulas, code, and diagrams in memory."
    )
    return system


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


def _with_context_timestamp(content: str, created_at: Any | None) -> str:
    return f"[time: {format_context_timestamp(created_at)}]\n{content}"


async def _build_context(message: str, deps: PipelineDeps) -> list[dict[str, Any]]:
    config = deps.config
    store = deps.store

    system_prompt = _build_default_system_prompt(config)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": ""},
    ]
    bootstrap_sections = [
        "[System Prompt]\n" + system_prompt + "\n[/System Prompt]",
    ]

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

    extra_system_sections = [
        str(m.get("content", ""))
        for m in messages[1:]
        if m.get("role") == "system" and str(m.get("content", "")).strip()
    ]
    if extra_system_sections:
        bootstrap_sections.extend(extra_system_sections)
        messages = messages[:1]

    priority_override = await _inject_priority_override(store, deps.group_key)
    bootstrap = "\n\n".join(section for section in bootstrap_sections if section.strip())
    messages.append({
        "role": "user",
        "content": _append_priority_override(bootstrap, priority_override),
    })

    window_ctx = deps.window.get_context()
    for m in window_ctx:
        role = m["role"]
        content = _with_context_timestamp(str(m["content"]), m.get("created_at"))
        if role == "user":
            content = _append_priority_override(content, priority_override)
        messages.append({"role": role, "content": content})

    messages.append({
        "role": "user",
        "content": _append_priority_override(message, priority_override),
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
) -> str:
    return json.dumps({
        "user": message,
        "bot": response,
        "status": status,
        "source": source,
    }, ensure_ascii=False)


async def _save_inbound_msg(deps: PipelineDeps, message: str, category: str) -> int | None:
    if not deps.remember_input:
        return None
    try:
        today = date.today().isoformat()
        msg_id = await deps.store.save(StoredMessage(
            date=today,
            group_key=deps.group_key,
            category=category,
            content=_message_record_content(message, source=deps.source),
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
) -> None:
    if not deps.remember_input or msg_id is None:
        return
    try:
        await deps.store.update_message_content(
            msg_id,
            _message_record_content(message, response=response, status=status, source=deps.source),
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


async def _save_send_artifacts(deps: PipelineDeps, reply_result: str) -> list[str]:
    if not deps.remember_input:
        return []
    try:
        parsed = json.loads(reply_result)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(parsed, dict):
        return []

    artifacts = parsed.get("artifacts", [])
    if not isinstance(artifacts, list):
        return []

    summaries: list[str] = []
    today = date.today().isoformat()
    message_id = parsed.get("data", {}).get("message_id") if isinstance(parsed.get("data"), dict) else None
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        if artifact.get("kind") != "sent_image":
            continue
        record = dict(artifact)
        if message_id is not None:
            record["message_id"] = message_id
        await deps.store.save(StoredMessage(
            date=today,
            group_key=deps.group_key,
            category="image",
            content=json.dumps(record, ensure_ascii=False),
        ))
        source = record.get("source", "image")
        file = record.get("file", "")
        summaries.append(f"[sent image: {source}, file={file}, message_id={message_id}]")
    return summaries


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
        await _generate_and_save_summary(deps, source, text, summarizer_cfg)


async def _generate_and_save_summary(deps: PipelineDeps, source: str, text: str, summarizer_cfg) -> None:
    api_key = summarizer_cfg.api_key or deps.config.model.api_key
    if not api_key:
        return

    base_url = summarizer_cfg.base_url or deps.config.model.base_url
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            payload = {
                "model": summarizer_cfg.model,
                "messages": [
                    {"role": "system", "content": "用1-2句话中文总结以下对话内容，不超过100字。"},
                    {"role": "user", "content": text[:2000]},
                ],
                "temperature": summarizer_cfg.temperature,
                "max_tokens": 150,
            }
            resp = await client.post(
                f"{base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json=payload,
            )
            if resp.status_code == 200:
                data = resp.json()
                summary = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                if summary:
                    max_id = await deps.store.get_max_message_id(deps.group_key)
                    summary_id = await deps.store.add_summary(
                        deps.group_key, source, summary.strip(),
                        last_message_id=max_id,
                    )
                    await deps.store.trim_summaries(
                        deps.group_key,
                        max_count=deps.config.context.summaries_max_count,
                        min_count=deps.config.context.summaries_min_count,
                    )
    except Exception:
        logger.exception("Failed to generate/save summary")


async def _recycle_window_if_needed(deps: PipelineDeps) -> None:
    config = deps.config.context
    total = sum(_estimate_msg_tokens(m) for m in deps.window.get_context())
    if total <= config.window_max_tokens:
        return

    items = list(deps.window)
    accumulated = 0
    cutoff_idx = 0
    for i, msg in enumerate(items):
        accumulated += _estimate_msg_tokens(msg)
        if (total - accumulated) <= config.window_min_tokens:
            cutoff_idx = i + 1
            break

    if cutoff_idx == 0:
        cutoff_idx = len(items) // 2

    to_archive = items[:cutoff_idx]
    kept = items[cutoff_idx:]

    combined = "\n".join(
        f"[{m.get('role', 'unknown')}]: {str(m.get('content', ''))[:500]}"
        for m in to_archive
    )
    if combined.strip():
        summarizer_cfg = deps.config.summarizer
        await _generate_and_save_summary(deps, "mixed", combined, summarizer_cfg)

    deps.window.replace(kept)
    logger.warning("[RECYCLE] window %d→%d items", len(items), len(kept))


def _split_visible_content(content: str) -> list[str]:
    parts: list[str] = []
    buffer: list[str] = []
    i = 0
    while i < len(content):
        if content.startswith("\\|", i):
            buffer.append("|")
            i += 2
            continue
        char = content[i]
        if char == "|":
            part = "".join(buffer).strip()
            if part:
                parts.append(part)
            buffer = []
        else:
            buffer.append(char)
        i += 1

    part = "".join(buffer).strip()
    if part:
        parts.append(part)
    return parts


async def _send_visible_content(deps: PipelineDeps, content: str) -> list[str]:
    parts = _split_visible_content(content)
    if not parts:
        logger.info("[PIPE] visible content empty after split")
        return []

    if deps.silent:
        logger.info("[PIPE] silent mode suppressed visible content parts=%d chars=%d", len(parts), len(content))
        return parts

    for part in parts:
        await deps.sender.send(deps.peer, part)
        log_send(deps, "content", part)
    logger.info("[PIPE] sent visible content parts=%d chars=%d", len(parts), len(content))
    return parts


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

    if msg_type == MessageType.IMAGE:
        _report_state(deps, "IMAGE")
        logger.info("[PIPE] branch=image unsupported image_file=%s image_url=%s", bool(image_file), bool(image_url))
        image_description: str | None = None
        if deps.config.vision.enabled:
            image_description = await describe_image(image_file=image_file, image_url=image_url, config=deps.config)
            if image_description.startswith("[Error:"):
                bot_reply = f"收到图片，但图像识别失败：{image_description}"
            else:
                bot_reply = f"收到图片：{image_description}"
        else:
            bot_reply = "收到图片，暂不支持图片识别"
        if not deps.silent:
            await deps.sender.send(deps.peer, bot_reply)
        if deps.remember_input:
            deps.window.add(user_id=str(deps.peer.peer_uid), message=message)
            deps.window.add(user_id=str(deps.peer.peer_uid), message=bot_reply, is_bot=True)
            today = date.today().isoformat()
            await deps.store.save(StoredMessage(
                date=today,
                group_key=deps.group_key,
                category=MessageType.IMAGE.value,
                content=json.dumps({
                    "user": message,
                    "bot": bot_reply,
                    "status": "responded",
                    "source": deps.source,
                    "image_file": image_file,
                    "image_url": image_url,
                    "image_description": image_description,
                }, ensure_ascii=False),
            ))
        deps.session.touch()
        return
        await deps.sender.send(deps.peer, "收到图片，暂不支持图片识别")
        deps.window.add(user_id=str(deps.peer.peer_uid), message=message)
        deps.window.add(user_id=str(deps.peer.peer_uid), message="[图片]", is_bot=True)
        deps.session.touch()
        await _save_msg(deps, message, MessageType.IMAGE.value,
                        f"image_file={image_file}, image_url={image_url}" if image_file or image_url else None)
        return

    _pending_writes: list[tuple[str, dict, Callable[[], Awaitable[None]]]] = []
    bot_replies: list[str] = []
    responded = False
    final_status = "received"
    inbound_msg_id: int | None = None

    try:
        inbound_msg_id = await _save_inbound_msg(deps, message, msg_type.value)
        deps.session.mark_pending()

        is_cold = deps.session.is_cold(deps.config.session.timeout)
        if is_cold and not deps.silent:
            _report_state(deps, "POKE")
            logger.info("[PIPE] cold session; sending poke")
            await deps.sender.send_poke(deps.peer)
        deps.session.touch()

        _report_state(deps, "CTX_build")
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
                    )
                break

            send_calls = [tc for tc in result.tool_calls if tc["name"] == "send"]
            other_calls = [tc for tc in result.tool_calls if tc["name"] != "send"]
            no_reply_called = any(tc["name"] == NO_REPLY_TOOL for tc in other_calls)
            logger.info(
                "[PIPE] branch=tool_calls send=%d no_reply=%d other=%d",
                len(send_calls),
                1 if no_reply_called else 0,
                len([tc for tc in other_calls if tc["name"] != NO_REPLY_TOOL]),
            )

            tc_results: dict[str, str] = {}
            _report_state(deps, f"LOOP_{step + 1}:Exec_Tools")

            for tc in send_calls:
                if send_count >= MAX_SENDS_PER_LOOP:
                    reply_result = f"[Error: send limit reached: {MAX_SENDS_PER_LOOP}]"
                    logger.warning("[PIPE] send limit reached max=%d", MAX_SENDS_PER_LOOP)
                    tc_results[tc["id"]] = reply_result
                    log_tool_call(deps, "send", tc["arguments"], reply_result)
                    continue
                try:
                    if deps.silent:
                        reply_result = "[OK] send suppressed by silent pipeline"
                    else:
                        reply_result = await send_tool(
                            tc["arguments"],
                            sender=deps.sender,
                            peer=deps.peer,
                            config=deps.config,
                        )
                    log_send(deps, "tool", tc["arguments"])
                    log_tool_call(deps, "send", tc["arguments"], reply_result)
                    if not str(reply_result).startswith("[Error:"):
                        text_reply = str(tc["arguments"].get("text", ""))
                        if text_reply:
                            bot_replies.append(text_reply)
                        artifact_summaries = await _save_send_artifacts(deps, str(reply_result))
                        bot_replies.extend(artifact_summaries)
                        responded = True
                        final_status = "responded"
                except Exception as e:
                    logger.exception("send_tool failed")
                    reply_result = f"[Error: {e}]"
                tc_results[tc["id"]] = str(reply_result)
                send_count += 1

            for tc in other_calls:
                logger.info("[PIPE] executing tool name=%s queued=%s", tc["name"], tc["name"] in WRITE_TOOLS)
                if tc["name"] == last_tool_name:
                    consecutive_fails += 1
                else:
                    consecutive_fails = 0
                    last_tool_name = tc["name"]

                if consecutive_fails >= 5:
                    msg = (
                        f"[Error: tool '{tc['name']}' called {consecutive_fails} times "
                        f"consecutively without success, please report failure to user]"
                    )
                    tc_results[tc["id"]] = msg
                    log_tool_call(deps, tc["name"], tc["arguments"], msg)
                    continue
                if tc["name"] in WRITE_TOOLS and not deps.remember_input:
                    tr = "[OK] write tool suppressed by non-remembering pipeline]"
                    tc_results[tc["id"]] = tr
                    log_tool_call(deps, tc["name"], tc["arguments"], tr)
                elif tc["name"] in WRITE_TOOLS:
                    tool_name = tc["name"]
                    tool_args = tc["arguments"]
                    _pending_writes.append((
                        tool_name, tool_args,
                        lambda tn=tool_name, ta=tool_args: deps.registry.execute(
                            tn, ta, store=deps.store, group_key=deps.group_key,
                            config=deps.config, sender=deps.sender, peer=deps.peer,
                        ),
                    ))
                    tc_results[tc["id"]] = "[OK] queued"
                    log_tool_call(deps, tool_name, tool_args, "[OK] queued", queued=True)
                else:
                    try:
                        tr = await deps.registry.execute(
                            tc["name"], tc["arguments"],
                            store=deps.store, group_key=deps.group_key,
                            config=deps.config, sender=deps.sender, peer=deps.peer,
                        )
                    except Exception as e:
                        logger.exception("Tool %s failed", tc["name"])
                        tr = f"[Error: {e}]"
                    tc_results[tc["id"]] = tr
                    log_tool_call(deps, tc["name"], tc["arguments"], tr)

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
            deps.window.add(user_id=str(deps.peer.peer_uid), message=message)
            combined_bot_reply = "\n".join(bot_replies)
            if combined_bot_reply:
                deps.window.add(user_id=str(deps.peer.peer_uid), message=combined_bot_reply, is_bot=True)
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
        )

        _report_state(deps, "DONE")

    except asyncio.CancelledError:
        logger.info("[PIPE] cancelled for %s", deps.peer.peer_uid)
        if inbound_msg_id is None:
            inbound_msg_id = await _recover_inbound_msg_id(deps, message, msg_type.value)
        await _update_saved_msg(
            deps,
            inbound_msg_id,
            message,
            msg_type.value,
            response=None,
            status="cancelled",
        )
        raise
    except Exception as e:
        logger.exception("[PIPE] error for %s", deps.peer.peer_uid)
        await _update_saved_msg(
            deps,
            inbound_msg_id,
            message,
            msg_type.value,
            response=None,
            status="error",
        )
        if not responded:
            await deps.sender.send(deps.peer, f"模型暂时不可用: {e}")
    finally:
        deps.session.clear_pending()
        logger.info("[PIPE] cleanup start pending_writes=%d", len(_pending_writes))

        for tool_name, tool_args, write_fn in _pending_writes:
            try:
                result = await write_fn()
                log_tool_call(deps, tool_name, tool_args, str(result), queued=False)
            except Exception:
                logger.exception("Post-write failed for %s", tool_name)

        if deps.remember_input:
            try:
                await _archive_if_needed(deps, message, bot_replies)
            except Exception:
                logger.exception("Archive failed")

            try:
                await _recycle_window_if_needed(deps)
            except Exception:
                logger.exception("Window recycle failed")
        logger.info("[PIPE] cleanup complete")
