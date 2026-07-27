from __future__ import annotations

import json

import pytest

from src.mutsumi_sync.config import Config
from src.mutsumi_sync.main import build_registry
from src.mutsumi_sync.memory.store import MessageStore
from src.mutsumi_sync.tools.media import media_search, sticker_manage


@pytest.mark.asyncio
async def test_sticker_search_without_query_lists_global_media(tmp_path):
    store = MessageStore(str(tmp_path / "media.db"), str(tmp_path / "media"))
    await store.initialize()
    try:
        record = await store.register_media(b"sticker", kind="sticker", ext="png")
        await store.update_media_description(record.media_id, "A smiling sticker", "smiling")
        result = json.loads(await media_search({}, store=store))
        assert result[0]["media_id"] == record.media_id
        assert result[0]["short_description"] == "smiling"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_sticker_manage_archives_media(tmp_path):
    store = MessageStore(str(tmp_path / "media.db"), str(tmp_path / "media"))
    await store.initialize()
    try:
        record = await store.register_media(b"sticker", kind="sticker", ext="png")
        result = await sticker_manage({"action": "archive", "media_id": record.media_id}, store=store)
        assert result.startswith("[OK]")
        assert (await store.get_media(record.media_id)).status == "archived"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_media_tools_work_through_production_registry(tmp_path):
    store = MessageStore(str(tmp_path / "registry.db"), str(tmp_path / "media"))
    await store.initialize()
    try:
        config = Config.load("config.example.yaml")
        registry = build_registry(config, store)
        record = await store.register_media(b"registry sticker", kind="sticker", ext="png")

        result = json.loads(await registry.execute(
            "sticker_search",
            {},
            store=store,
            group_key="private:test",
        ))
        assert result[0]["media_id"] == record.media_id

        manage_result = await registry.execute(
            "sticker_manage",
            {"action": "archive", "media_id": record.media_id},
            store=store,
            group_key="private:test",
        )
        assert manage_result.startswith("[OK]")
        assert (await store.get_media(record.media_id)).status == "archived"
    finally:
        await store.close()
