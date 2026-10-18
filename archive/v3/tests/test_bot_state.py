from __future__ import annotations

import pytest

from src.mutsumi_sync.config import Config
from src.mutsumi_sync.main import build_registry
from src.mutsumi_sync.memory.store import MessageStore
from src.mutsumi_sync.tools.bot_state import bot_state_tool


@pytest.mark.asyncio
async def test_bot_state_is_global_and_versioned(tmp_path):
    store = MessageStore(str(tmp_path / "state.db"), str(tmp_path / "media"))
    await store.initialize()
    try:
        assert (await bot_state_tool({"action": "replace", "content": "I value honest answers."}, store=store)).startswith("[OK]")
        assert (await bot_state_tool({"action": "add", "content": "I am learning patience."}, store=store)).startswith("[OK]")
        state = await store.get_canonical_state()
        assert state["version"] == 2
        assert "honest answers" in state["content"]
        assert "learning patience" in state["content"]
        await bot_state_tool({"action": "clear"}, store=store)
        assert await store.get_canonical_state() is None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_bot_state_works_through_production_registry(tmp_path):
    store = MessageStore(str(tmp_path / "state.db"), str(tmp_path / "media"))
    await store.initialize()
    try:
        registry = build_registry(Config.load("config.example.yaml"), store)
        result = await registry.execute(
            "bot_state",
            {"action": "replace", "content": "I value honest answers."},
            store=store,
            group_key="private:test",
        )

        assert result == "[Error: unknown tool: bot_state]"
        assert await store.get_canonical_state() is None
    finally:
        await store.close()
