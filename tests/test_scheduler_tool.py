from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from src.mutsumi_sync.message.sender import Peer
from src.mutsumi_sync.tools.scheduler import parse_scheduled_time, scheduler_tool
from src.mutsumi_sync.tools.registry import ToolRegistry
from src.mutsumi_sync.main import register_scheduler_tool


class FakeScheduler:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def schedule_once(self, *, scheduled_at: float, prompt: str, group_key: str, peer: Peer) -> int:
        self.calls.append({
            "scheduled_at": scheduled_at,
            "prompt": prompt,
            "group_key": group_key,
            "peer": peer,
        })
        return 42


class CronScheduler:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def schedule_task(
        self,
        *,
        scheduled_at: float,
        prompt: str,
        group_key: str,
        peer: Peer,
        interval_seconds: float | None,
        cron_expression: str,
        context: str = "",
    ) -> int:
        self.calls.append({
            "scheduled_at": scheduled_at,
            "prompt": prompt,
            "group_key": group_key,
            "peer": peer,
            "interval_seconds": interval_seconds,
            "cron_expression": cron_expression,
            "context": context,
        })
        return 99


def test_parse_scheduled_time_accepts_llm_formatted_time_with_timezone() -> None:
    dt = parse_scheduled_time("2026-07-08 09:30:00 +08:00")

    assert dt.tzinfo is not None
    assert dt.isoformat() == "2026-07-08T09:30:00+08:00"


async def test_scheduler_tool_registers_task_and_returns_readable_delay() -> None:
    now = datetime(2026, 7, 7, 9, 0, 0, tzinfo=timezone(timedelta(hours=8)))
    scheduled_time = "2026-07-08 10:02:03 +08:00"
    fake_scheduler = FakeScheduler()
    peer = Peer(chat_type=1, peer_uid="123")

    result = await scheduler_tool(
        {"scheduled_time": scheduled_time, "prompt": "提醒我检查日志"},
        scheduler=fake_scheduler,
        group_key="private:123",
        peer=peer,
        now=now,
    )

    assert "[OK]" in result
    assert "task_id=42" in result
    assert "1天1小时2分钟3秒后" in result
    assert fake_scheduler.calls == [{
        "scheduled_at": parse_scheduled_time(scheduled_time).timestamp(),
        "prompt": "提醒我检查日志",
        "group_key": "private:123",
        "peer": peer,
    }]


async def test_scheduler_tool_requires_scheduled_time() -> None:
    result = await scheduler_tool(
        {"prompt": "missing time"},
        scheduler=FakeScheduler(),
        group_key="private:123",
        peer=Peer(chat_type=1, peer_uid="123"),
        now=datetime.now(timezone.utc),
    )

    assert result.startswith("[Error:")


async def test_scheduler_tool_requires_prompt_for_future_schedule() -> None:
    result = await scheduler_tool(
        {"scheduled_time": "2026-07-08 10:02:03 +08:00"},
        scheduler=FakeScheduler(),
        group_key="private:123",
        peer=Peer(chat_type=1, peer_uid="123"),
        now=datetime(2026, 7, 7, 9, 0, 0, tzinfo=timezone(timedelta(hours=8))),
    )

    assert result == "[Error: prompt is required for schedule]"


async def test_scheduler_tool_passes_cron_to_generic_scheduler() -> None:
    now = datetime(2026, 7, 7, 9, 0, 0, tzinfo=timezone(timedelta(hours=8)))
    scheduled_time = "2026-07-08 10:02:03 +08:00"
    fake_scheduler = CronScheduler()
    peer = Peer(chat_type=1, peer_uid="123")

    result = await scheduler_tool(
        {
            "scheduled_time": scheduled_time,
            "cron": "0 10 * * *",
            "prompt": "daily work",
        },
        scheduler=fake_scheduler,
        group_key="private:123",
        peer=peer,
        now=now,
    )

    assert "kind=cron" in result
    assert fake_scheduler.calls[0]["cron_expression"] == "0 10 * * *"
    assert fake_scheduler.calls[0]["interval_seconds"] is None


async def test_scheduler_tool_passes_optional_context_unchanged() -> None:
    now = datetime(2026, 7, 7, 9, 0, 0, tzinfo=timezone(timedelta(hours=8)))
    fake_scheduler = CronScheduler()
    peer = Peer(chat_type=1, peer_uid="123")

    result = await scheduler_tool(
        {
            "scheduled_time": "2026-07-08 10:02:03 +08:00",
            "prompt": "daily work",
            "context": "Read skills/research/BOT.md; deliver the result as plain text.",
        },
        scheduler=fake_scheduler,
        group_key="private:123",
        peer=peer,
        now=now,
    )

    assert "[OK]" in result
    assert fake_scheduler.calls[0]["context"] == (
        "Read skills/research/BOT.md; deliver the result as plain text."
    )


async def test_scheduler_tool_rejects_past_time() -> None:
    result = await scheduler_tool(
        {"scheduled_time": "2026-07-07 08:59:59 +08:00"},
        scheduler=FakeScheduler(),
        group_key="private:123",
        peer=Peer(chat_type=1, peer_uid="123"),
        now=datetime(2026, 7, 7, 9, 0, 0, tzinfo=timezone(timedelta(hours=8))),
    )

    assert "past" in result


def test_scheduler_tool_is_registered_with_required_time_schema() -> None:
    registry = ToolRegistry()
    register_scheduler_tool(registry, FakeScheduler())

    schema = registry.to_openai_schema()
    tool = next(item["function"] for item in schema if item["function"]["name"] == "scheduler")

    assert tool["parameters"]["required"] == []
    assert "operation" in tool["parameters"]["properties"]
    assert "interval_seconds" in tool["parameters"]["properties"]
    assert "prompt" in tool["parameters"]["properties"]
    assert "context" in tool["parameters"]["properties"]
