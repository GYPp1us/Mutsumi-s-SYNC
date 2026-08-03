"""Baseline context builder: uses the REAL production projection stack.

Writes synthetic rounds into a real MessageStore as finalized Event Ledger
rows, compacts them with the real Episode summarizer, and projects the real
Life Stream. This makes the baseline benchmark faithful to the current
codebase rather than an approximation.
"""

from __future__ import annotations

import json
import logging
import types
from typing import Any

import httpx

from src.mutsumi_sync.config import Config
from src.mutsumi_sync.memory.actors import format_actor_source, format_outbound_source
from src.mutsumi_sync.memory.episodes import summarize_pending_episode
from src.mutsumi_sync.memory.projection import build_life_stream
from src.mutsumi_sync.memory.store import EventRecord, MessageStore
from src.mutsumi_sync.token_estimate import estimate_tokens

logger = logging.getLogger("mutsumi.exp.baseline")

EPISODE_SUMMARY_TOKEN_BUDGET = 600  # approx tokens per episode narrative
#: Production reclaims until the request estimate is below the target ratio
#: (0.5 x capacity). We apply the same idea to the history block alone; 0.6
#: keeps a raw suffix visible while making Episodes enter the projection.
COMPACTION_TARGET_RATIO = 0.6


def make_summarizer_config(base_url: str, model: str, api_key: str) -> Config:
    config = Config()
    config.summarizer.api_key = api_key
    config.summarizer.base_url = base_url
    config.summarizer.model = model
    config.summarizer.temperature = 0.3
    config.summarizer.max_input_tokens = 32000
    return config


class CountingAsyncClient:
    """Drop-in httpx.AsyncClient replacement that records every call's usage."""

    def __init__(self, timeout: float = 60.0):
        self.timeout = timeout
        self.calls: list[dict[str, Any]] = []

    async def __aenter__(self) -> "CountingAsyncClient":
        self._real = httpx.AsyncClient(timeout=self.timeout)
        await self._real.__aenter__()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self._real.__aexit__(*args)

    async def post(self, url: str, *, headers: dict, json: dict) -> httpx.Response:
        resp = await self._real.post(url, headers=headers, json=json)
        usage = None
        if resp.status_code == 200:
            try:
                usage = resp.json().get("usage")
            except Exception:
                usage = None
        self.calls.append({
            "url": url,
            "payload": json,
            "status": resp.status_code,
            "usage": usage,
        })
        return resp


async def _rendered_tokens(store: MessageStore, conversation_id: str, after_sequence: int) -> int:
    """Estimate the token cost of the RENDERED projection for uncovered events."""
    uncovered = await store.get_events(
        conversation_id=conversation_id,
        after_sequence=after_sequence,
        limit=1_000_000,
        finalized_only=False,
    )
    total = 0
    for event in uncovered:
        if event.event_type == "inbound":
            text = format_actor_source(
                conversation_id=event.conversation_id,
                actor_id=event.actor_id,
                actor_name=event.actor_name,
                actor_kind=event.actor_kind,
                content=event.content,
                created_at=event.created_at,
            )
        elif event.event_type == "outbound":
            text = format_outbound_source(
                conversation_id=event.conversation_id,
                content=event.content,
                created_at=event.created_at,
            )
        else:
            text = event.content
        total += estimate_tokens(text)
    return total


async def build_baseline_history(
    rounds_pairs: list[tuple[str, str]],
    *,
    conversation_id: str,
    budget_tokens: int,
    config: Config,
    counter: CountingAsyncClient,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Compact the ledger until uncovered raw events + episodes fit the budget,
    then project the real Life Stream. Returns (history, stats)."""
    import src.mutsumi_sync.memory.episodes as episodes_module

    original_httpx = episodes_module.httpx
    episodes_module.httpx = types.SimpleNamespace(
        AsyncClient=lambda timeout=60: counter
    )
    try:
        store = MessageStore(db_path=":memory:")
        await store.initialize()
        for index, (user, assistant) in enumerate(rounds_pairs):
            pipeline_id = f"exp-{index}"
            await store.append_event(EventRecord(
                conversation_id=conversation_id,
                actor_id="qq:user:exp",
                actor_kind="human",
                actor_name="实验用户",
                event_type="inbound",
                content=user,
                pipeline_id=pipeline_id,
                status="finalized",
            ))
            await store.append_event(EventRecord(
                conversation_id=conversation_id,
                actor_id="bot:self",
                actor_kind="bot",
                actor_name="Mutsumi",
                event_type="outbound",
                content=assistant,
                pipeline_id=pipeline_id,
                status="finalized",
            ))

        episodes_created = 0
        target = int(budget_tokens * COMPACTION_TARGET_RATIO)
        for _ in range(200):
            frontier = await store.get_latest_episode_sequence(conversation_id)
            remaining_tokens = await _rendered_tokens(store, conversation_id, frontier)
            if remaining_tokens + episodes_created * EPISODE_SUMMARY_TOKEN_BUDGET <= target:
                break
            episode_id = await summarize_pending_episode(
                store,
                config,
                conversation_id,
                max_events=160,
            )
            if episode_id is None:
                logger.warning("[baseline] episode compaction returned None")
                break
            episodes_created += 1

        summary_calls = list(counter.calls)
        counter.calls.clear()
        history = await build_life_stream(
            store,
            limit=max(400, len(rounds_pairs) * 2 + 50),
            max_tokens=budget_tokens,
            audience_conversation_id=conversation_id,
        )
        episodes = await store.get_episodes(conversation_id, limit=10_000)
        total_events = len(await store.get_events(
            conversation_id=conversation_id, limit=1_000_000, finalized_only=False,
        ))
        stats = {
            "episodes_created": len(episodes),
            "episode_ranges": [
                [int(e.first_sequence), int(e.last_sequence)] for e in episodes
            ],
            "events_total": total_events,
            "history_messages": len(history),
            "history_tokens": estimate_tokens(json.dumps(history, ensure_ascii=False, separators=(",", ":"))),
            "summary_calls": len(summary_calls),
            "summary_input_tokens": sum(
                int(c["usage"]["prompt_tokens"]) for c in summary_calls if c["usage"]
            ),
            "summary_output_tokens": sum(
                int(c["usage"]["completion_tokens"]) for c in summary_calls if c["usage"]
            ),
        }
        await store.close()
        return history, stats
    finally:
        episodes_module.httpx = original_httpx
