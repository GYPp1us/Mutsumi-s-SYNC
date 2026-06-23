import os
import tempfile

import pytest

from src.mutsumi_sync.memory.store import MessageStore
from src.mutsumi_sync.tools.self_note import SELF_NOTE_SCHEMA, self_note_tool


async def make_store():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="test_sn_")
    os.close(fd)
    store = MessageStore(db_path=path)
    await store.initialize()
    return store, path


class TestSelfNoteTool:
    async def test_add_creates_new(self):
        store, path = await make_store()
        try:
            result = await self_note_tool(
                {"action": "add", "content": "user is friendly"},
                store=store, group_key="private:123",
            )
            assert result.startswith("[OK]")
            note = await store.get_current_self_note("private:123")
            assert "user is friendly" in note["content"]
        finally:
            await store.close()
            os.unlink(path)

    async def test_add_appends(self):
        store, path = await make_store()
        try:
            await self_note_tool(
                {"action": "add", "content": "line one"},
                store=store, group_key="private:123",
            )
            await self_note_tool(
                {"action": "add", "content": "line two"},
                store=store, group_key="private:123",
            )
            note = await store.get_current_self_note("private:123")
            assert "line one" in note["content"]
            assert "line two" in note["content"]
        finally:
            await store.close()
            os.unlink(path)

    async def test_replace_overwrites(self):
        store, path = await make_store()
        try:
            await self_note_tool(
                {"action": "add", "content": "old content"},
                store=store, group_key="private:123",
            )
            await self_note_tool(
                {"action": "replace", "content": "fresh content"},
                store=store, group_key="private:123",
            )
            note = await store.get_current_self_note("private:123")
            assert "fresh content" in note["content"]
            assert "old content" not in note["content"]
        finally:
            await store.close()
            os.unlink(path)

    async def test_empty_content_error(self):
        store, path = await make_store()
        try:
            result = await self_note_tool(
                {"action": "add", "content": ""},
                store=store, group_key="g1",
            )
            assert result.startswith("[Error:")
        finally:
            await store.close()
            os.unlink(path)

    async def test_unknown_action_error(self):
        store, path = await make_store()
        try:
            result = await self_note_tool(
                {"action": "delete", "content": "x"},
                store=store, group_key="g1",
            )
            assert result.startswith("[Error:")
        finally:
            await store.close()
            os.unlink(path)

    def test_schema(self):
        assert "action" in SELF_NOTE_SCHEMA["properties"]
        assert "replace" in SELF_NOTE_SCHEMA["properties"]["action"]["enum"]
