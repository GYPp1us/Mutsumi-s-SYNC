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
from .tools.send import send_tool
from .logging import log_context, log_llm_result, log_tool_call, log_send, ESTIMATE_CHARS_PER_TOKEN

if TYPE_CHECKING:
    from .scheduler import PipelineDeps

logger = logging.getLogger("mutsumi.pipeline")

MAX_TOOL_STEPS = 10
MAX_SENDS_PER_LOOP = 5
WRITE_TOOLS = {"self_note", "memory_save"}


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


def _build_default_system_prompt(config) -> str:
    system = config.system_prompt or "You are a helpful assistant."
    system += (
        "\n你拥有一个 [私人印象] 空间用于维护对用户的私人印象和关键信息。"
        "\n[私人印象] 标签标注了当前长度与目标上限。"
        "\n请保持内容精炼，优先保留长期价值高的信息。"
        "\n使用 self_note(add) 追加新信息，使用 self_note(replace) 重新整理。"
        "\n如果当前已接近或超出目标长度，请在下一轮对话中主动用 replace 模式精简。"
        "\n如果同一个工具调用连续失败 3 次以上，停止重试并向用户汇报失败原因。"
    )
    return system


async def _inject_self_note(store, group_key: str, config) -> str:
    note = await store.get_current_self_note(group_key)
    if not note or not note.get("content"):
        return ""

    content = note["content"]
    current = _estimate_tokens(content)
    target = config.memory.self_note_target_tokens
    limit = int(target * config.memory.self_note_max_multiplier)

    if current > limit:
        chars = limit * ESTIMATE_CHARS_PER_TOKEN
        content = content[:chars] + "\n[truncated]"

    return f"[私人印象 — current: {current} / target: {target} tokens]\n{content}\n[/私人印象]"


async def _build_context(message: str, deps: PipelineDeps) -> list[dict[str, Any]]:
    config = deps.config
    store = deps.store

    system_prompt = _build_default_system_prompt(config)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
    ]

    self_note_text = await _inject_self_note(store, deps.group_key, config)
    if self_note_text:
        messages.append({"role": "system", "content": self_note_text})

    summaries = await store.get_summaries(deps.group_key, limit=config.context.summaries_max_count)
    if summaries:
        summary_texts = []
        for s in summaries:
            source_label = "user" if s["source"] == "user" else "assistant"
            summary_texts.append(f"[{source_label}]: {s['summary']}")
        if summary_texts:
            messages.append({"role": "system", "content": "[摘要]\n" + "\n".join(summary_texts) + "\n[/摘要]"})

    window_ctx = deps.window.get_context()
    for m in window_ctx:
        messages.append({"role": m["role"], "content": m["content"]})

    messages.append({"role": "user", "content": message})

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
    except Exception:
        logger.exception("Failed to save message to store")


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
                    await deps.store.add_summary(deps.group_key, source, summary.strip())
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


async def pipeline(
    message: str,
    msg_type: MessageType,
    image_file: str | None,
    image_url: str | None,
    *,
    deps: PipelineDeps,
) -> None:
    if msg_type == MessageType.MEDIA:
        await deps.sender.send(deps.peer, "暂不支持此消息类型")
        await _save_msg(deps, message, MessageType.MEDIA.value, None)
        return

    if msg_type == MessageType.IMAGE:
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

    try:
        deps.session.mark_pending()

        is_cold = deps.session.is_cold(deps.config.session.timeout)
        if is_cold:
            await deps.sender.send_poke(deps.peer)
        deps.session.touch()

        messages = await _build_context(message, deps)
        _log_context_size(messages, deps)
        send_count = 0
        last_tool_name = ""
        consecutive_fails = 0

        for step in range(MAX_TOOL_STEPS):
            log_context(messages, deps)
            start_time = time.monotonic()
            result = await _do_llm_call(messages, deps)
            elapsed = time.monotonic() - start_time
            log_llm_result(deps, result, elapsed)

            if not result.tool_calls and not result.content:
                if not responded and is_cold:
                    await deps.sender.send(deps.peer, "在的，请说。")
                    responded = True
                    bot_replies.append("在的，请说。")
                break

            if not result.tool_calls and result.content:
                await deps.sender.send(deps.peer, result.content)
                log_send(deps, "content", result.content)
                responded = True
                bot_replies.append(result.content)
                await _save_msg(deps, message, msg_type.value, result.content)
                break

            send_calls = [tc for tc in result.tool_calls if tc["name"] == "send"]
            other_calls = [tc for tc in result.tool_calls if tc["name"] != "send"]

            for tc in send_calls:
                if send_count >= MAX_SENDS_PER_LOOP:
                    break
                try:
                    reply_result = await send_tool(tc["arguments"], sender=deps.sender, peer=deps.peer)
                    log_send(deps, "tool", tc["arguments"])
                    bot_replies.append(str(tc["arguments"].get("text", "")))
                    responded = True
                except Exception as e:
                    logger.exception("send_tool failed")
                send_count += 1

            tc_results: dict[str, str] = {}
            for tc in other_calls:
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
                if tc["name"] in WRITE_TOOLS:
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

            if other_calls:
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
                        for tc in other_calls
                    ],
                }
                if result.reasoning_content:
                    assistant_msg["reasoning_content"] = result.reasoning_content
                messages.append(assistant_msg)

                for tc in other_calls:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": tc_results[tc["id"]],
                    })
            elif result.content:
                msg: dict[str, Any] = {"role": "assistant", "content": result.content}
                if result.reasoning_content:
                    msg["reasoning_content"] = result.reasoning_content
                messages.append(msg)

            if not other_calls:
                if result.content:
                    await deps.sender.send(deps.peer, result.content)
                    log_send(deps, "content", result.content)
                    responded = True
                    bot_replies.append(result.content)
                break
        else:
            logger.warning("[LOOP] tool loop exhausted after %d steps, context ~%d msgs",
                           MAX_TOOL_STEPS, len(messages))
            _log_context_size(messages, deps)

        if responded:
            deps.window.add(user_id=str(deps.peer.peer_uid), message=message)
            last_bot_reply = bot_replies[-1] if bot_replies else ""
            if last_bot_reply:
                deps.window.add(user_id=str(deps.peer.peer_uid), message=last_bot_reply, is_bot=True)

    except asyncio.CancelledError:
        logger.info("[PIPE] cancelled for %s", deps.peer.peer_uid)
        raise
    except Exception as e:
        logger.exception("[PIPE] error for %s", deps.peer.peer_uid)
        if not responded:
            await deps.sender.send(deps.peer, f"模型暂时不可用: {e}")
    finally:
        deps.session.clear_pending()

        for tool_name, tool_args, write_fn in _pending_writes:
            try:
                result = await write_fn()
                log_tool_call(deps, tool_name, tool_args, str(result), queued=False)
            except Exception:
                logger.exception("Post-write failed for %s", tool_name)

        try:
            await _archive_if_needed(deps, message, bot_replies)
        except Exception:
            logger.exception("Archive failed")

        try:
            await _recycle_window_if_needed(deps)
        except Exception:
            logger.exception("Window recycle failed")
