from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .scheduler import PipelineDeps

logger = logging.getLogger("mutsumi.logging")

_DIM = "\033[2m"
_RESET = "\033[0m"
_BOLD = "\033[1m"
_CYAN = "\033[36m"

ESTIMATE_CHARS_PER_TOKEN = 4


def log_context(messages: list[dict], deps: PipelineDeps) -> None:
    provider = deps.config.model.provider
    model = deps.config.model.model
    total_chars = sum(len(str(m.get("content", ""))) for m in messages)
    total_tokens = total_chars // ESTIMATE_CHARS_PER_TOKEN

    lines = ["", f"{_BOLD}{_CYAN}=========[CONTEXT][{provider}][{model}]========={_RESET}"]
    for msg in messages:
        role = msg.get("role", "?")
        content_str = str(msg.get("content", ""))
        tc_info = ""
        if "tool_calls" in msg:
            tc_names = [
                tc.get("function", {}).get("name", "?")
                for tc in msg["tool_calls"]
            ]
            tc_info = f" {_DIM}[tc: {', '.join(tc_names)}]{_RESET}"
        preview = content_str.replace("\n", "\\n")
        if len(preview) > 150:
            preview = preview[:147] + "..."
        lines.append(f"{_DIM}[{role}]{_RESET} {preview}{tc_info}")
        if "tool_call_id" in msg:
            lines.append(f"{_DIM}  \u21b3 id={msg['tool_call_id']}{_RESET}")
    lines.append(f"{_BOLD}{_CYAN}=========[{len(messages)} msgs][~{total_tokens} tokens]========={_RESET}")
    logger.info("\n".join(lines))


def log_llm_result(deps: PipelineDeps, result, elapsed: float) -> None:
    provider = deps.config.model.provider
    model = deps.config.model.model

    input_total = result.input_tokens
    cache_hit = getattr(result, "cache_hit_tokens", 0)
    cache_miss = getattr(result, "cache_miss_tokens", 0)
    if input_total > 0 and (cache_hit + cache_miss) > 0:
        hit_pct = round(cache_hit / (cache_hit + cache_miss) * 100)
    else:
        hit_pct = 0

    cache_info = f"[▣:{hit_pct}%]" if hit_pct > 0 else ""

    lines = ["", f"=========[{provider}][{model}]========="]

    if result.reasoning_content:
        lines.append(f"{_DIM}[reasoning]{_RESET}")
        lines.append(f"{_DIM}{result.reasoning_content}{_RESET}")
        lines.append(f"{_DIM}[/reasoning]{_RESET}")

    if result.content:
        lines.append(f"{_DIM}{result.content}{_RESET}")

    footer = f"=========[↑:{result.input_tokens}][↓:{result.output_tokens}]{cache_info}========="
    lines.append(footer)
    logger.info("\n".join(lines))


def log_tool_call(deps: PipelineDeps, tool_name: str, args: dict, result: str, queued: bool = False) -> None:
    tag = "queued" if queued else "executed"
    args_preview = json.dumps(args, ensure_ascii=False)
    if len(args_preview) > 200:
        args_preview = args_preview[:197] + "..."
    result_preview = str(result).replace("\n", "\\n")
    if len(result_preview) > 150:
        result_preview = result_preview[:147] + "..."

    lines = [
        "",
        f"{_BOLD}{_CYAN}=========[TOOL][{tool_name}][{tag}]========={_RESET}",
        f"{_DIM}  args: {args_preview}{_RESET}",
        f"{_DIM}  result: {result_preview}{_RESET}",
        f"{_BOLD}{_CYAN}=========[/TOOL]========={_RESET}",
    ]
    logger.info("\n".join(lines))


def log_send(deps: PipelineDeps, kind: str, content_or_segments) -> None:
    label = "private" if deps.peer.chat_type == 1 else "group"
    peer_uid = deps.peer.peer_uid
    header = f"{_BOLD}{_CYAN}=========[SEND][{label}][{peer_uid}]========={_RESET}"

    if kind == "content":
        text = str(content_or_segments)
        preview = text.replace("\n", "\\n")
        if len(preview) > 200:
            preview = preview[:197] + "..."
        lines = ["", header, f"{_DIM}  [text] {preview}{_RESET}", f"{_BOLD}{_CYAN}=========[1 segment]========={_RESET}"]
    elif kind == "tool":
        args = content_or_segments
        seg_info = json.dumps(args, ensure_ascii=False)
        if len(seg_info) > 300:
            seg_info = seg_info[:297] + "..."
        lines = ["", header, f"{_DIM}  [tool_call] {seg_info}{_RESET}", f"{_BOLD}{_CYAN}=========[tool send]========={_RESET}"]
    else:
        lines = ["", header, f"{_DIM}  [{kind}] {str(content_or_segments)[:200]}{_RESET}", f"{_BOLD}{_CYAN}=========[1 segment]========={_RESET}"]

    logger.info("\n".join(lines))
