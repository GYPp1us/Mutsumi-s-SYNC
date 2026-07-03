import pytest
import tempfile
import os
from src.mutsumi_sync.tools.memory import memory_save, memory_search, MEMORY_SAVE_SCHEMA, MEMORY_SEARCH_SCHEMA
from src.mutsumi_sync.memory.store import MessageStore, StoredMessage


async def make_store():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="test_mem_")
    os.close(fd)
    store = MessageStore(db_path=path)
    await store.initialize()
    return store, path


class TestMemorySave:
    async def test_save_fact(self):
        store, path = await make_store()
        try:
            result = await memory_save(
                {"content": "user birthday is May 20"},
                store=store, group_key="private:123",
            )
            assert result.startswith("[OK]")
            assert "saved" in result.lower()
        finally:
            await store.close()
            os.unlink(path)

    async def test_save_empty_content(self):
        store, path = await make_store()
        try:
            result = await memory_save({"content": ""}, store=store, group_key="g1")
            assert result.startswith("[Error:")
        finally:
            await store.close()
            os.unlink(path)


class TestMemorySearch:
    async def test_search_finds_saved(self):
        store, path = await make_store()
        try:
            await store.save(StoredMessage(
                date="2026-06-22", group_key="private:123",
                category="memory", content="user likes hiking on weekends"
            ))
            result = await memory_search(
                {"query": "hiking"},
                store=store, group_key="private:123",
            )
            assert "hiking" in result.lower()
        finally:
            await store.close()
            os.unlink(path)

    async def test_search_no_results(self):
        store, path = await make_store()
        try:
            result = await memory_search(
                {"query": "nonexistent_xyz_123"},
                store=store, group_key="private:123",
            )
            assert "no matching" in result.lower()
        finally:
            await store.close()
            os.unlink(path)

    async def test_search_empty_query(self):
        store, path = await make_store()
        try:
            result = await memory_search(
                {"query": ""},
                store=store, group_key="private:123",
            )
            assert result.startswith("[Error:")
        finally:
            await store.close()
            os.unlink(path)

    async def test_search_with_limit(self):
        store, path = await make_store()
        try:
            for i in range(10):
                await store.save(StoredMessage(
                    date="2026-06-22", group_key="g1",
                    category="memory", content=f"test memory item {i}"
                ))
            result = await memory_search(
                {"query": "test memory", "limit": 3},
                store=store, group_key="g1",
            )
            assert result.count("[") <= 3 + 1
        finally:
            await store.close()
            os.unlink(path)


class TestSchemas:
    def test_memory_save_schema(self):
        assert "content" in MEMORY_SAVE_SCHEMA["properties"]
        assert MEMORY_SAVE_SCHEMA["required"] == ["content"]

    def test_memory_search_schema(self):
        assert "query" in MEMORY_SEARCH_SCHEMA["properties"]
        assert MEMORY_SEARCH_SCHEMA["required"] == ["query"]
