from __future__ import annotations

from datetime import date, datetime, time, timezone, timedelta
from typing import Any


UTC_PLUS_8 = timezone(timedelta(hours=8))
UNKNOWN_TIME = "很久之前"


def now_timestamp_text() -> str:
    return datetime.now(UTC_PLUS_8).isoformat(timespec="seconds")


def format_context_timestamp(value: Any | None) -> str:
    if value is None:
        return UNKNOWN_TIME
    if isinstance(value, datetime):
        dt = value.astimezone(UTC_PLUS_8)
        return dt.isoformat(timespec="seconds")
    if isinstance(value, date):
        dt = datetime.combine(value, time.min, tzinfo=UTC_PLUS_8)
        return dt.isoformat(timespec="seconds")
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return UNKNOWN_TIME
        try:
            if len(stripped) == 10:
                parsed_date = date.fromisoformat(stripped)
                return format_context_timestamp(parsed_date)
            parsed = datetime.fromisoformat(stripped)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC_PLUS_8)
            return parsed.astimezone(UTC_PLUS_8).isoformat(timespec="seconds")
        except ValueError:
            return stripped
    if isinstance(value, (int, float)):
        numeric = float(value)
        if numeric <= 0:
            return UNKNOWN_TIME
        if 2_000_000 < numeric < 3_000_000:
            numeric = (numeric - 2_440_587.5) * 86_400
        return datetime.fromtimestamp(numeric, tz=UTC_PLUS_8).isoformat(timespec="seconds")
    return UNKNOWN_TIME


def has_timestamp_prefix(line: str) -> bool:
    stripped = line.strip()
    if not stripped.startswith("["):
        return False
    closing = stripped.find("]")
    if closing <= 1:
        return False
    stamp = stripped[1:closing]
    return stamp == UNKNOWN_TIME or "+08:00" in stamp or stamp.endswith("Z")


def ensure_timestamped_lines(content: str, *, fallback: str = UNKNOWN_TIME) -> str:
    lines: list[str] = []
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if has_timestamp_prefix(line):
            lines.append(line)
        else:
            lines.append(f"[{fallback}] {line}")
    return "\n".join(lines)


def timestamp_memory_entry(content: str, *, timestamp: str | None = None) -> str:
    stamp = timestamp or now_timestamp_text()
    text = content.strip()
    if not text:
        return ""
    if has_timestamp_prefix(text.splitlines()[0]):
        return text
    return f"[{stamp}] {text}"
