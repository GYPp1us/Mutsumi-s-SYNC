from __future__ import annotations

import json

from ..memory.store import MessageStore

MEDIA_SEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "可选关键词；省略时列出全部可复用媒体"},
        "kind": {"type": "string", "enum": ["image", "audio", "video", "sticker"]},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
    },
}

STICKER_MANAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["describe", "archive", "restore"]},
        "media_id": {"type": "string"},
        "description": {"type": "string"},
        "short_description": {"type": "string"},
    },
    "required": ["action"],
}


async def media_search(args: dict, *, store: MessageStore, **deps) -> str:
    records = await store.list_media(
        kind=args.get("kind"),
        limit=max(1, min(int(args.get("limit", 100)), 100)),
    )
    query = str(args.get("query", "")).strip().lower()
    terms = [term for term in query.split() if term]
    if query:
        scored: list[tuple[int, object]] = []
        for record in records:
            haystack = " ".join((record.media_id, record.description, record.short_description)).lower()
            score = sum(1 for term in terms if term in haystack)
            if score:
                scored.append((score, record))
        records = [record for _, record in sorted(scored, key=lambda item: item[0], reverse=True)]
    return json.dumps([
        {
            "media_id": record.media_id,
            "kind": record.kind,
            "short_description": record.short_description,
            "reusable": record.status == "active" and bool(record.path or record.source_url),
        }
        for record in records
    ], ensure_ascii=False)


async def sticker_manage(args: dict, *, store: MessageStore, **deps) -> str:
    action = str(args.get("action", "")).strip()
    media_id = str(args.get("media_id", "")).strip()
    if action == "describe":
        if not media_id or not args.get("description"):
            return "[Error: describe requires media_id and description]"
        await store.update_media_description(
            media_id,
            str(args["description"]),
            str(args.get("short_description", "")),
        )
        return f"[OK] sticker description updated: {media_id}"
    if action in {"archive", "restore"}:
        if not media_id:
            return f"[Error: {action} requires media_id]"
        await store.set_media_status(media_id, "archived" if action == "archive" else "active")
        return f"[OK] sticker {action}d: {media_id}"
    return f"[Error: unknown sticker action: {action}]"
