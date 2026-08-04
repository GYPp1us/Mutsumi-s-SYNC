"""Use the production Event Ledger and Life Stream for the complex baseline."""

from __future__ import annotations

import json
from typing import Any

from src.mutsumi_sync.memory.projection import build_life_stream
from src.mutsumi_sync.memory.store import EventRecord, EventType, MessageStore
from src.mutsumi_sync.token_estimate import estimate_tokens

from .long_scenario import LongCase, LongRecord


def _actor_kind(actor: str) -> str:
    if actor == "worker":
        return "service"
    if actor == "owner":
        return "human"
    return "human"


async def build_ledger_history(
    records: tuple[LongRecord, ...],
    case: LongCase,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Append complete synthetic turns and project them without compaction."""
    store = MessageStore(db_path=":memory:")
    await store.initialize()
    try:
        for index, record in enumerate(records):
            pipeline_id = f"context-v3-{record.record_id}"
            await store.append_event(EventRecord(
                conversation_id=record.conversation,
                actor_id=record.actor,
                actor_kind=_actor_kind(record.actor),
                actor_name=record.actor_name,
                event_type=EventType.INBOUND.value,
                content=record.text,
                audience="bot:self",
                visibility=record.visibility,
                pipeline_id=pipeline_id,
                status="finalized",
                created_at=1_700_000_000.0 + index,
            ))
            await store.append_event(EventRecord(
                conversation_id=record.conversation,
                actor_id="bot:self",
                actor_kind="bot",
                actor_name="Mutsumi",
                event_type=EventType.OUTBOUND.value,
                content=f"Recorded {record.record_id} for the Atlas matter.",
                audience="bot:self",
                visibility=record.visibility,
                pipeline_id=pipeline_id,
                status="finalized",
                created_at=1_700_000_000.0 + index + 0.1,
            ))
        history = await build_life_stream(
            store,
            limit=max(400, len(records) * 3),
            max_tokens=1_000_000,
            audience_conversation_id=case.conversation,
        )
        return history, {
            "ledger_events": len(records) * 2,
            "history_messages": len(history),
            "history_tokens": estimate_tokens(json.dumps(history, ensure_ascii=False, separators=(",", ":"))),
            "compaction_calls": 0,
            "visible_records": sum(
                1
                for record in records
                if record.visibility == "global" or record.conversation == case.conversation
            ),
        }
    finally:
        await store.close()
