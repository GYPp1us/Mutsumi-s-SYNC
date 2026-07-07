from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
from typing import Any

from ..message.sender import Peer

_UTC_PLUS_8 = timezone(timedelta(hours=8))
_TZ_WITH_SPACE_RE = re.compile(r"(\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)\s+([+-]\d{2}:\d{2})$")


SCHEDULER_SCHEMA = {
    "type": "object",
    "properties": {
        "scheduled_time": {
            "type": "string",
            "description": (
                "Required formatted trigger time. Prefer ISO 8601 with timezone, "
                "for example 2026-07-08 09:30:00 +08:00 or 2026-07-08T09:30:00+08:00. "
                "If timezone is omitted, UTC+8 is assumed."
            ),
        },
        "prompt": {
            "type": "string",
            "description": "Optional prompt to feed into the pipeline when the task fires.",
        },
    },
    "required": ["scheduled_time"],
    "additionalProperties": False,
}


def parse_scheduled_time(value: str) -> datetime:
    text = value.strip()
    if not text:
        raise ValueError("scheduled_time is required")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    text = _TZ_WITH_SPACE_RE.sub(r"\1\2", text)
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_UTC_PLUS_8)
    return dt


def _format_duration(delta: timedelta) -> str:
    total_seconds = max(0, int(delta.total_seconds()))
    days, rem = divmod(total_seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)

    parts: list[str] = []
    if days:
        parts.append(f"{days}天")
    if hours:
        parts.append(f"{hours}小时")
    if minutes:
        parts.append(f"{minutes}分钟")
    if seconds or not parts:
        parts.append(f"{seconds}秒")
    return "".join(parts) + "后"


async def scheduler_tool(
    args: dict[str, Any],
    *,
    scheduler,
    group_key: str,
    peer: Peer,
    now: datetime | None = None,
    **_,
) -> str:
    """Register a one-shot scheduled pipeline trigger."""
    raw_time = str(args.get("scheduled_time", "")).strip()
    if not raw_time:
        return "[Error: scheduled_time is required]"
    if peer is None:
        return "[Error: scheduler tool requires a target peer]"

    try:
        scheduled_dt = parse_scheduled_time(raw_time)
    except ValueError as e:
        return f"[Error: invalid scheduled_time: {e}]"

    current = now or datetime.now(_UTC_PLUS_8)
    if current.tzinfo is None:
        current = current.replace(tzinfo=_UTC_PLUS_8)

    delay = scheduled_dt - current.astimezone(scheduled_dt.tzinfo)
    if delay.total_seconds() <= 0:
        return "[Error: scheduled_time is in the past]"

    prompt = str(args.get("prompt") or "定时任务触发。请根据当前上下文完成提醒或后续处理。").strip()
    task_id = await scheduler.schedule_once(
        scheduled_at=scheduled_dt.timestamp(),
        prompt=prompt,
        group_key=group_key,
        peer=peer,
    )
    readable_delay = _format_duration(delay)
    return (
        f"[OK] scheduled task_id={task_id}; "
        f"trigger_at={scheduled_dt.astimezone(_UTC_PLUS_8).isoformat()}; "
        f"delay={readable_delay}"
    )
