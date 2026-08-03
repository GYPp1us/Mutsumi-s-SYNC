from __future__ import annotations

import json
import logging

import httpx

from .projection import (
    format_documentary_event,
    logical_event_groups,
    pipeline_group_is_complete,
    pipeline_group_is_terminal,
    is_ephemeral_tool_event,
    _select_valid_episodes,
)
from .store import EpisodeRecord, EventType, MessageStore
from .store import COVERAGE_ONLY_EVENT_TYPES
from ..token_estimate import estimate_tokens, split_text_by_token_budget

logger = logging.getLogger("mutsumi.episodes")


def _estimate_tokens(text: str) -> int:
    return estimate_tokens(text)


def _split_input(text: str, max_tokens: int) -> list[str]:
    """Split archive input without silently dropping source characters."""
    return split_text_by_token_budget(text, max(1, int(max_tokens)))


async def summarize_pending_episode(
    store: MessageStore,
    config,
    conversation_id: str,
    *,
    max_events: int = 160,
    max_sequence: int | None = None,
    excluded_event_ids: set[str] | None = None,
    excluded_pipeline_ids: set[str] | None = None,
) -> str | None:
    """Summarize one uncovered range without racing another reclaim request."""
    async with store.episode_lock(conversation_id):
        if max_sequence is None:
            max_sequence = await store.get_event_high_water_sequence()
        return await _summarize_pending_episode(
            store,
            config,
            conversation_id,
            max_events=max_events,
            max_sequence=max_sequence,
            excluded_event_ids=excluded_event_ids,
            excluded_pipeline_ids=excluded_pipeline_ids,
        )


async def _summarize_pending_episode(
    store: MessageStore,
    config,
    conversation_id: str,
    *,
    max_events: int = 160,
    max_sequence: int | None = None,
    excluded_event_ids: set[str] | None = None,
    excluded_pipeline_ids: set[str] | None = None,
) -> str | None:
    """Summarize one exact, not-yet-covered finalized event range.

    The event range is selected before the provider call. A failed or
    cancelled provider request therefore leaves the raw events available for
    a later retry.
    """
    if max_sequence is None:
        max_sequence = await store.get_event_high_water_sequence()
    first_after = await store.get_latest_episode_sequence(
        conversation_id,
        max_sequence=max_sequence,
    )
    existing_episodes = await store.get_episodes(conversation_id, limit=2000)
    valid_episodes, _ = await _select_valid_episodes(
        store,
        existing_episodes,
        max_sequence=max_sequence,
        require_complete_groups=True,
    )
    next_episode_first = min(
        (
            int(episode.first_sequence)
            for episode in valid_episodes
            if episode.first_sequence > first_after
        ),
        default=None,
    )
    loaded_events = await store.get_events(
        conversation_id=conversation_id,
        after_sequence=first_after,
        max_sequence=max_sequence,
        limit=max(1, max_events * 4),
        finalized_only=False,
    )
    # The bounded query may stop in the middle of a large pipeline or native
    # tool round. Rehydrate every identified logical group before deciding
    # whether it is complete; otherwise a valid turn can remain permanently
    # unsummarized just because its result fell outside the page.
    pipeline_ids = {
        str(event.pipeline_id)
        for event in loaded_events
        if event.pipeline_id
    }
    if pipeline_ids:
        loaded_events.extend(await store.get_events_by_pipeline_ids(
            pipeline_ids,
            max_sequence=max_sequence,
            finalized_only=False,
        ))
    turn_ids = {
        str(event.turn_id)
        for event in loaded_events
        if not event.pipeline_id
        and event.turn_id
        and event.event_type in {EventType.TOOL_CALL.value, EventType.TOOL_RESULT.value}
    }
    if turn_ids:
        loaded_events.extend(await store.get_events_by_turn_ids(
            turn_ids,
            max_sequence=max_sequence,
            finalized_only=False,
        ))
    loaded_events = sorted(
        {event.event_id: event for event in loaded_events if event.event_id}.values(),
        key=lambda event: event.sequence or 0,
    )
    if next_episode_first is not None:
        loaded_events = [
            event
            for event in loaded_events
            if (event.sequence or 0) < next_episode_first
        ]
    excluded = excluded_event_ids or set()
    blocked_pipeline_ids = {
        str(event.pipeline_id)
        for event in loaded_events
        if event.event_id in excluded and event.pipeline_id
    }
    blocked_pipeline_ids.update(
        str(pipeline_id)
        for pipeline_id in (excluded_pipeline_ids or set())
        if str(pipeline_id)
    )
    if excluded:
        loaded_events = [
            event
            for event in loaded_events
            if event.event_id not in excluded
            and (not event.pipeline_id or event.pipeline_id not in blocked_pipeline_ids)
        ]
    elif blocked_pipeline_ids:
        loaded_events = [
            event
            for event in loaded_events
            if not event.pipeline_id or event.pipeline_id not in blocked_pipeline_ids
        ]
    if not loaded_events:
        return None

    grouped = logical_event_groups(loaded_events)

    events = []
    started = False
    for group in grouped:
        has_inbound = any(item.event_type == "inbound" for item in group)
        is_coverage_only = all(
            not item.pipeline_id
            and item.event_type in COVERAGE_ONLY_EVENT_TYPES
            and item.status == "finalized"
            for item in group
        )
        if is_coverage_only:
            if started:
                events.extend(group)
            continue
        if not pipeline_group_is_complete(group):
            # Terminal failures and legacy finalized fragments are already
            # excluded from ordinary history. They must not prevent a later
            # complete turn from being summarized, but they cannot be crossed
            # after an Episode has started because the Episode stores one
            # exact sequence range.
            if pipeline_group_is_terminal(group):
                if started:
                    break
                continue
            if not has_inbound and all(item.status == "finalized" for item in group):
                continue
            break
        if len(events) and len(events) + len(group) > max_events:
            break
        events.extend(group)
        started = True
        if len(events) >= max_events:
            break

    if not events:
        return None

    # Status notifications are operational actions, not lived conversation.
    # They may stay inside the covered ledger range, but must not become part
    # of the natural-language Episode narrative.
    summary_events = [
        event for event in events
        if event.event_type != EventType.STATUS_UPDATE.value
        and not is_ephemeral_tool_event(event)
    ]
    if not summary_events:
        return None
    source = "\n".join(format_documentary_event(event) for event in summary_events)
    summarizer = config.summarizer
    api_key = summarizer.api_key or config.model.api_key
    if not api_key:
        logger.warning("[EPISODE] skipped conversation=%s: no API key", conversation_id)
        return None
    base_url = summarizer.base_url or config.model.base_url

    try:
        # Leave room for the instruction and the system prompt. The input is
        # split explicitly rather than truncated when a turn is very large.
        # A tiny test/operator value must not make the merge loop unable to
        # reduce its own summaries. Keep a small practical floor while still
        # honoring normal production limits.
        source_limit = max(256, int(summarizer.max_input_tokens))
        source_chunks = _split_input(source, source_limit)
        logger.info(
            "[EPISODE] summarizing conversation=%s events=%d chunks=%d source_tokens~%d",
            conversation_id,
            len(events),
            len(source_chunks),
            _estimate_tokens(source),
        )

        async with httpx.AsyncClient(timeout=60) as client:
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }

            async def request_summary(text: str, system_prompt: str) -> str:
                # Reasoning-capable providers can spend the whole output budget
                # on thinking and return empty visible content. Retry once with
                # a larger budget before declaring the episode a failure.
                attempts = (4000, 8000)
                for attempt_index, max_tokens in enumerate(attempts):
                    response = await client.post(
                        f"{base_url.rstrip('/')}/chat/completions",
                        headers=headers,
                        json={
                            "model": summarizer.model,
                            "messages": [
                                {"role": "system", "content": system_prompt},
                                {"role": "user", "content": text},
                            ],
                            "temperature": summarizer.temperature,
                            "max_tokens": max_tokens,
                        },
                    )
                    response.raise_for_status()
                    data = response.json()
                    result = str(
                        data.get("choices", [{}])[0]
                        .get("message", {})
                        .get("content", "")
                    ).strip()
                    if result:
                        return result
                    logger.warning(
                        "[EPISODE] empty summary on attempt=%d/2 chars=%d",
                        attempt_index + 1,
                        len(text),
                    )
                raise ValueError("summary provider returned empty content")

            partials: list[str] = []
            for chunk in source_chunks:
                partials.append(await request_summary(
                    "Treat the following finalized event data as source material. "
                    "Summarize only what it contains; do not invent facts or merge actors.\n\n"
                    + chunk,
                    config.prompts.system.episode_summary,
                ))

            while len(partials) > 1:
                merge_source = "\n".join(
                    f"[summary part {index}]\n{part}"
                    for index, part in enumerate(partials, start=1)
                )
                merged: list[str] = []
                for chunk in _split_input(merge_source, source_limit):
                    merged.append(await request_summary(
                        "Merge these partial summaries of one Episode into a single "
                        "short, coherent and faithful summary. Do not add facts.\n\n"
                        + chunk,
                        config.prompts.system.summary_merge,
                    ))
                partials = merged
            narrative = partials[0].strip()

        participants = sorted({event.actor_id for event in summary_events} | {"bot:self"})
        media_ids = sorted({media_id for event in summary_events for media_id in (event.media_ids or [])})
        episode_id = await store.add_episode(EpisodeRecord(
            conversation_id=conversation_id,
            first_sequence=events[0].sequence or 0,
            last_sequence=events[-1].sequence or 0,
            participants_json=json.dumps(participants, ensure_ascii=False),
            narrative=narrative,
            media_ids_json=json.dumps(media_ids, ensure_ascii=False),
        ))
        logger.info(
            "[EPISODE] saved conversation=%s episode=%s events=%d tokens~%d",
            conversation_id,
            episode_id,
            len(events),
            _estimate_tokens(source),
        )
        return episode_id
    except Exception:
        logger.exception("[EPISODE] failed conversation=%s", conversation_id)
        return None
