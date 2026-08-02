from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
from typing import Any

from ..message.sender import Peer

_UTC_PLUS_8 = timezone(timedelta(hours=8))
_TZ_WITH_SPACE_RE = re.compile(r"(\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)\s+([+-]\d{2}:\d{2})$")


@dataclass(frozen=True)
class CronSpec:
    minutes: frozenset[int]
    hours: frozenset[int]
    days_of_month: frozenset[int]
    months: frozenset[int]
    days_of_week: frozenset[int]
    day_of_month_wildcard: bool
    day_of_week_wildcard: bool


def _parse_cron_field(text: str, minimum: int, maximum: int, label: str) -> tuple[frozenset[int], bool]:
    raw = text.strip()
    if not raw:
        raise ValueError(f"cron {label} field is empty")
    values: set[int] = set()
    # A stepped wildcard (for example ``*/5``) is still a wildcard for the
    # DOM/DOW matching rule. Comma lists containing ``*`` retain that meaning
    # as well; the parsed value set remains the source for actual matching.
    wildcard = any(
        item.split("/", 1)[0].strip() == "*"
        for item in raw.split(",")
    )
    for item in raw.split(","):
        item = item.strip()
        if not item:
            raise ValueError(f"cron {label} field contains an empty item")
        pieces = item.split("/", 1)
        if len(pieces) == 2:
            try:
                step = int(pieces[1])
            except ValueError as exc:
                raise ValueError(f"cron {label} step must be an integer") from exc
            if step <= 0:
                raise ValueError(f"cron {label} step must be positive")
        else:
            step = 1
        base = pieces[0]
        if base == "*":
            start, end = minimum, maximum
        elif "-" in base:
            bounds = base.split("-", 1)
            if len(bounds) != 2:
                raise ValueError(f"invalid cron {label} range: {base}")
            try:
                start, end = int(bounds[0]), int(bounds[1])
            except ValueError as exc:
                raise ValueError(f"cron {label} range must be numeric") from exc
        else:
            try:
                start = int(base)
            except ValueError as exc:
                raise ValueError(f"cron {label} value must be numeric") from exc
            end = maximum if len(pieces) == 2 else start
        if start < minimum or start > maximum or end < minimum or end > maximum:
            raise ValueError(f"cron {label} value out of range")
        if end < start:
            raise ValueError(f"cron {label} range is reversed")
        values.update(range(start, end + 1, step))
    if label == "day-of-week" and 7 in values:
        values.remove(7)
        values.add(0)
    return frozenset(values), wildcard


def parse_cron_expression(value: str) -> CronSpec:
    fields = str(value or "").split()
    if len(fields) != 5:
        raise ValueError("cron must contain five fields: minute hour day month weekday")
    minutes, _ = _parse_cron_field(fields[0], 0, 59, "minute")
    hours, _ = _parse_cron_field(fields[1], 0, 23, "hour")
    days_of_month, dom_wildcard = _parse_cron_field(fields[2], 1, 31, "day-of-month")
    months, _ = _parse_cron_field(fields[3], 1, 12, "month")
    days_of_week, dow_wildcard = _parse_cron_field(fields[4], 0, 7, "day-of-week")
    return CronSpec(
        minutes=minutes,
        hours=hours,
        days_of_month=days_of_month,
        months=months,
        days_of_week=days_of_week,
        day_of_month_wildcard=dom_wildcard,
        day_of_week_wildcard=dow_wildcard,
    )


def next_cron_time(expression: str, after: datetime) -> datetime:
    """Return the next UTC+8 wall-clock minute matching a five-field cron."""
    spec = parse_cron_expression(expression)
    current = after.astimezone(_UTC_PLUS_8) if after.tzinfo else after.replace(tzinfo=_UTC_PLUS_8)
    candidate = current.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(366 * 24 * 60 * 2):
        if candidate.month not in spec.months:
            candidate += timedelta(minutes=1)
            continue
        if candidate.hour not in spec.hours or candidate.minute not in spec.minutes:
            candidate += timedelta(minutes=1)
            continue
        dom_match = candidate.day in spec.days_of_month
        dow_match = ((candidate.weekday() + 1) % 7) in spec.days_of_week
        if spec.day_of_month_wildcard or spec.day_of_week_wildcard:
            day_match = dom_match and dow_match
        else:
            # Traditional cron uses OR when both day fields are restricted.
            day_match = dom_match or dow_match
        if day_match:
            return candidate
        candidate += timedelta(minutes=1)
    raise ValueError("cron has no matching time in the next two years")


SCHEDULER_SCHEMA = {
    "type": "object",
    "properties": {
        "operation": {
            "type": "string",
            "enum": ["schedule", "list", "pause", "resume", "cancel"],
            "description": "Lifecycle operation. Defaults to schedule.",
        },
        "task_id": {
            "type": "integer",
            "description": "Existing task id for list lifecycle operations.",
        },
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
            "description": (
                "Required for schedule. Natural-language prompt to feed into the "
                "bound pipeline when the task fires."
            ),
        },
        "context": {
            "type": "string",
            "description": (
                "Optional natural-language context stored with the task and injected "
                "only into the firing pipeline. Scheduler does not interpret it."
            ),
        },
        "interval_seconds": {
            "type": "number",
            "description": "Optional positive interval for a recurring task.",
        },
        "cron": {
            "type": "string",
            "description": (
                "Optional five-field cron expression in UTC+8, for example "
                "0 9 * * * for every day at 09:00. Do not combine with interval_seconds."
            ),
        },
    },
    "required": [],
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
    conversation_id: str = "",
    now: datetime | None = None,
    **_,
) -> str:
    """Manage durable one-shot, interval, and cron pipeline triggers."""
    operation = str(args.get("operation") or "schedule").strip().lower()
    bound_group_key = str(conversation_id or group_key).strip()
    if not bound_group_key:
        return "[Error: scheduler tool requires a bound conversation]"

    if operation == "list":
        records = await scheduler.list_scheduled_tasks(group_key=bound_group_key)
        if not records:
            return "[OK] no scheduled tasks"
        lines = ["[OK] scheduled tasks:"]
        for record in records:
            interval = (
                f" every={int(record.interval_seconds)}s"
                if record.interval_seconds
                else ""
            )
            cron = f" cron={record.cron_expression}" if record.cron_expression else ""
            lines.append(
                f"- id={record.id} status={record.status} kind={record.schedule_kind} "
                f"trigger_at={datetime.fromtimestamp(record.scheduled_at, _UTC_PLUS_8).isoformat()}"
                f"{interval}{cron} prompt={record.prompt[:120]}"
                + (f" context={record.context[:120]}" if record.context else "")
            )
        return "\n".join(lines)

    if operation in {"pause", "resume", "cancel"}:
        try:
            task_id = int(args.get("task_id"))
        except (TypeError, ValueError):
            return f"[Error: task_id is required for {operation}]"
        return await scheduler.update_scheduled_task_lifecycle(
            task_id,
            operation,
            group_key=bound_group_key,
        )

    if operation != "schedule":
        return "[Error: operation must be schedule, list, pause, resume, or cancel]"
    if peer is None:
        return "[Error: scheduler tool requires a target peer]"
    raw_time = str(args.get("scheduled_time", "")).strip()
    if not raw_time:
        return "[Error: scheduled_time is required]"
    try:
        scheduled_dt = parse_scheduled_time(raw_time)
    except ValueError as exc:
        return f"[Error: invalid scheduled_time: {exc}]"

    current = now or datetime.now(_UTC_PLUS_8)
    if current.tzinfo is None:
        current = current.replace(tzinfo=_UTC_PLUS_8)
    delay = scheduled_dt - current.astimezone(scheduled_dt.tzinfo)
    if delay.total_seconds() <= 0:
        return "[Error: scheduled_time is in the past]"

    interval_seconds: float | None = None
    if args.get("interval_seconds") is not None:
        try:
            interval_seconds = float(args.get("interval_seconds"))
        except (TypeError, ValueError):
            return "[Error: interval_seconds must be a positive number]"
        if interval_seconds <= 0:
            return "[Error: interval_seconds must be a positive number]"

    cron_expression = str(args.get("cron") or "").strip()
    if cron_expression:
        if interval_seconds is not None:
            return "[Error: cron and interval_seconds cannot be combined]"
        try:
            parse_cron_expression(cron_expression)
        except ValueError as exc:
            return f"[Error: invalid cron: {exc}]"

    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        return "[Error: prompt is required for schedule]"
    context = str(args.get("context") or "").strip()
    if hasattr(scheduler, "schedule_task"):
        schedule_kwargs = {
            "scheduled_at": scheduled_dt.timestamp(),
            "prompt": prompt,
            "group_key": bound_group_key,
            "peer": peer,
            "interval_seconds": interval_seconds,
            "cron_expression": cron_expression,
        }
        if context:
            schedule_kwargs["context"] = context
        task_id = await scheduler.schedule_task(**schedule_kwargs)
    elif interval_seconds is None and hasattr(scheduler, "schedule_once"):
        schedule_kwargs = {
            "scheduled_at": scheduled_dt.timestamp(),
            "prompt": prompt,
            "group_key": bound_group_key,
            "peer": peer,
        }
        if context:
            schedule_kwargs["context"] = context
        task_id = await scheduler.schedule_once(**schedule_kwargs)
    else:
        return "[Error: scheduler does not support the requested schedule type]"
    readable_delay = _format_duration(delay)
    schedule_kind = "cron" if cron_expression else ("interval" if interval_seconds else "once")
    result = (
        f"[OK] scheduled task_id={task_id}; kind={schedule_kind}; "
        f"trigger_at={scheduled_dt.astimezone(_UTC_PLUS_8).isoformat()}; delay={readable_delay}"
    )
    if interval_seconds is not None:
        result += f"; interval={int(interval_seconds)}s"
    if cron_expression:
        result += f"; cron={cron_expression}"
    return result
