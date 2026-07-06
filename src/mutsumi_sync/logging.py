from __future__ import annotations

import atexit
import copy
from datetime import datetime, timezone
import json
import logging
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler
from pathlib import Path
import queue
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config
    from .scheduler import PipelineDeps

logger = logging.getLogger("mutsumi.logging")

_DIM = "\033[2m"
_RESET = "\033[0m"
_BOLD = "\033[1m"
_CYAN = "\033[36m"

ESTIMATE_CHARS_PER_TOKEN = 4
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_STREAM_LISTENER: QueueListener | None = None
_STREAM_FILE_HANDLER: RotatingFileHandler | None = None
_ATEXIT_REGISTERED = False


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


class NdjsonLogFormatter(logging.Formatter):
    def __init__(self, *, keep_ansi: bool = True):
        super().__init__()
        self.keep_ansi = keep_ansi

    def format(self, record: logging.LogRecord) -> str:
        raw_message = record.getMessage()
        has_ansi = bool(_ANSI_RE.search(raw_message))
        message = raw_message if self.keep_ansi else _strip_ansi(raw_message)
        payload: dict[str, object] = {
            "schema": "mutsumi.log.v1",
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": message,
            "ansi": has_ansi and self.keep_ansi,
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
            "process": record.process,
            "thread": record.threadName,
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


class _PreservingQueueHandler(QueueHandler):
    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        return copy.copy(record)


def start_stream_log_store(config: Config) -> logging.Handler | None:
    global _STREAM_LISTENER, _STREAM_FILE_HANDLER, _ATEXIT_REGISTERED
    stop_stream_log_store()

    stream_config = config.logging.stream_store
    if not stream_config.enabled:
        return None

    path = Path(stream_config.path)
    path.parent.mkdir(parents=True, exist_ok=True)

    file_handler = RotatingFileHandler(
        path,
        maxBytes=stream_config.max_bytes,
        backupCount=stream_config.backup_count,
        encoding="utf-8",
    )
    file_handler.setFormatter(NdjsonLogFormatter(keep_ansi=stream_config.keep_ansi))

    log_queue: queue.SimpleQueue[logging.LogRecord] = queue.SimpleQueue()
    queue_handler = _PreservingQueueHandler(log_queue)
    listener = QueueListener(log_queue, file_handler, respect_handler_level=True)
    listener.start()

    _STREAM_LISTENER = listener
    _STREAM_FILE_HANDLER = file_handler
    if not _ATEXIT_REGISTERED:
        atexit.register(stop_stream_log_store)
        _ATEXIT_REGISTERED = True
    return queue_handler


def stop_stream_log_store() -> None:
    global _STREAM_LISTENER, _STREAM_FILE_HANDLER
    listener = _STREAM_LISTENER
    file_handler = _STREAM_FILE_HANDLER
    _STREAM_LISTENER = None
    _STREAM_FILE_HANDLER = None

    if listener is not None:
        listener.stop()
    if file_handler is not None:
        file_handler.close()


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
