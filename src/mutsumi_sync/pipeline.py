from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING

import httpx

from .message.classifier import MessageType
from .memory.store import StoredMessage

if TYPE_CHECKING:
    from .scheduler import PipelineDeps

logger = logging.getLogger("mutsumi.pipeline")


@dataclass
class LLMResult:
    content: str
    reasoning_content: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0


async def pipeline(
    message: str,
    msg_type: MessageType,
    image_file: str | None,
    image_url: str | None,
    *,
    deps: PipelineDeps,
) -> None:
    """端到端消息处理。目前为 Phase 1 stub：分类→去重→冷启动→真实 LLM 调用。"""
    if msg_type == MessageType.MEDIA:
        await deps.sender.send(deps.peer, "\u6682\u4e0d\u652f\u6301\u6b64\u6d88\u606f\u7c7b\u578b")
        await _save_msg(deps, message, MessageType.MEDIA.value, None)
        return

    if msg_type == MessageType.IMAGE:
        await deps.sender.send(deps.peer, "\u6536\u5230\u56fe\u7247\uff0c\u6682\u4e0d\u652f\u6301\u56fe\u7247\u8bc6\u522b")
        deps.window.add(user_id=str(deps.peer.peer_uid), message=message)
        deps.window.add(user_id=str(deps.peer.peer_uid), message="[\u56fe\u7247]", is_bot=True)
        deps.session.touch()
        await _save_msg(deps, message, MessageType.IMAGE.value,
                        f"image_file={image_file}, image_url={image_url}" if image_file or image_url else None)
        return

    try:
        deps.session.mark_pending()

        if deps.session.is_cold(deps.config.session.timeout):
            logger.info("[PIPE] cold start poke for %s", deps.peer.peer_uid)
            await deps.sender.send_poke(deps.peer)

        deps.session.touch()

        start_time = time.monotonic()
        result = await _call_llm(deps, message)
        elapsed = time.monotonic() - start_time
        _log_llm_result(deps, result, elapsed)

        ctx = deps.window.get_context()
        if ctx:
            await deps.sender.send(deps.peer, f"[LLM] {result.content}")
        else:
            await deps.sender.send(deps.peer, result.content)

        deps.window.add(user_id=str(deps.peer.peer_uid), message=message)
        deps.window.add(user_id=str(deps.peer.peer_uid), message=result.content, is_bot=True)

        await _save_msg(deps, message, msg_type.value, result.content)

    except asyncio.CancelledError:
        logger.info("[PIPE] cancelled for %s", deps.peer.peer_uid)
        raise
    except Exception as e:
        logger.exception("[PIPE] error for %s", deps.peer.peer_uid)
        await deps.sender.send(deps.peer, f"\u6a21\u578b\u6682\u65f6\u4e0d\u53ef\u7528: {e}")
    finally:
        deps.session.clear_pending()


_MATH_PROMPT = """请详细、逐步地解答以下复杂数学问题。给出完整推导过程、每一步的计算和最终答案。

问题: %s

要求：
1. 展示完整推导步骤
2. 说明每一步使用的定理或方法
3. 给出最终数值答案（如适用）
4. 验证答案的正确性"""


async def _call_llm(deps: PipelineDeps, user_message: str) -> LLMResult:
    """真实 LLM 调用 — 思考模式 + tool calls。"""
    config = deps.config.model

    if not config.api_key:
        return _stub_response(user_message)

    if not config.base_url:
        return LLMResult(content="[Error: model.base_url not configured]")

    system_prompt = deps.config.system_prompt or "\u4f60\u662f\u4e00\u4e2a\u6570\u5b66\u52a9\u624b\uff0c\u8bf7\u8be6\u7ec6\u9010\u6b65\u89e3\u7b54\u95ee\u9898\u3002"
    prompt_msg = _MATH_PROMPT % user_message

    payload = {
        "model": config.model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt_msg},
        ],
        "thinking": {"type": "enabled"},
        "reasoning_effort": config.reasoning_effort,
        "temperature": config.temperature,
    }

    if tools := deps.registry.to_openai_schema():
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
        msg = data.get("choices", [{}])[0].get("message", {})
        usage = data.get("usage", {})

        return LLMResult(
            content=msg.get("content", "") or "[Error: empty LLM response]",
            reasoning_content=msg.get("reasoning_content"),
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            reasoning_tokens=usage.get("completion_tokens_details", {}).get("reasoning_tokens", 0),
        )


def _log_llm_result(deps: PipelineDeps, result: LLMResult, elapsed: float) -> None:
    provider = deps.config.model.provider
    model = deps.config.model.model

    lines = [f"=========[{provider}][{model}]========="]

    if result.reasoning_content:
        lines.append(f"[reasoning]")
        lines.append(result.reasoning_content)
        lines.append(f"[/reasoning]")

    lines.append(result.content)
    lines.append(f"=========[↑:{result.input_tokens}][↓:{result.output_tokens}]=========")

    logger.info("\n".join(lines))


def _stub_response(user_message: str) -> LLMResult:
    from datetime import datetime
    now = datetime.now().isoformat(timespec="seconds")
    return LLMResult(
        content=f"[LLM Stub @ {now}] I received: {user_message[:200]}",
        input_tokens=0,
        output_tokens=0,
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
