import pytest
import tempfile
import os
from src.mutsumi_sync.memory.store import MessageStore, StoredMessage


async def make_store():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="test_store_")
    os.close(fd)
    store = MessageStore(db_path=path)
    await store.initialize()
    return store, path


class TestSummaries:
    async def test_add_and_get_summaries(self):
        store, path = await make_store()
        try:
            id1 = await store.add_summary("group:1:1", "user", "summary one")
            id2 = await store.add_summary("group:1:1", "bot", "summary two")
            assert id1 > 0
            assert id2 > id1

            summaries = await store.get_summaries("group:1:1")
            assert len(summaries) == 2
            assert summaries[0]["summary"] == "summary one"
            assert summaries[1]["summary"] == "summary two"
            assert summaries[0]["seq"] == 1
            assert summaries[1]["seq"] == 2
            assert summaries[0]["kind"] == "message"
            assert summaries[0]["covered_through_message_id"] is None
        finally:
            await store.close()
            os.unlink(path)

    async def test_compaction_summary_has_trusted_coverage(self):
        store, path = await make_store()
        try:
            await store.add_summary(
                "g1",
                "mixed",
                "older complete turns",
                kind="compaction",
                covered_through_message_id=42,
            )

            boundary = await store.get_newest_compaction_summary("g1")

            assert boundary is not None
            assert boundary["kind"] == "compaction"
            assert boundary["covered_through_message_id"] == 42
        finally:
            await store.close()
            os.unlink(path)

    async def test_trim_summaries(self):
        store, path = await make_store()
        try:
            for i in range(10):
                await store.add_summary("g1", "user", f"summary {i}")

            deleted = await store.trim_summaries("g1", max_count=5, min_count=3)
            assert deleted >= 5

            summaries = await store.get_summaries("g1")
            assert len(summaries) <= 5
        finally:
            await store.close()
            os.unlink(path)


class TestSelfNote:
    async def test_upsert_and_get(self):
        store, path = await make_store()
        try:
            note = await store.get_current_self_note("group:1:1")
            assert note is None

            await store.upsert_self_note("group:1:1", "test note content")
            note = await store.get_current_self_note("group:1:1")
            assert note is not None
            assert "test note content" in note["content"]
        finally:
            await store.close()
            os.unlink(path)


class TestPriorityOverride:
    async def test_upsert_and_get(self):
        store, path = await make_store()
        try:
            item = await store.get_current_priority_override("group:1:1")
            assert item is None

            await store.upsert_priority_override("group:1:1", "exactly preserve formulas")
            item = await store.get_current_priority_override("group:1:1")
            assert item is not None
            assert "exactly preserve formulas" in item["content"]
            assert item["created_at"] is not None
        finally:
            await store.close()
            os.unlink(path)


class TestMessageUpdates:
    async def test_update_message_content(self):
        store, path = await make_store()
        try:
            msg_id = await store.save(StoredMessage(
                date="2026-06-22",
                group_key="g1",
                category="text",
                content='{"status": "received"}',
            ))

            await store.update_message_content(msg_id, '{"status": "responded"}')
            saved = await store.get_messages_by_ids([msg_id])

            assert saved[0]["content"] == '{"status": "responded"}'
            assert saved[0]["created_at"] is not None
        finally:
            await store.close()
            os.unlink(path)


class TestActionLedger:
    async def test_save_and_get_recent_actions(self):
        store, path = await make_store()
        try:
            action_id = await store.save_action(
                group_key="g1",
                tool_name="send",
                call_id="call-1",
                success=True,
                arguments={"markdown_image_sha256": "abc"},
                result="message_id=99",
                artifact={"file": "rendered.png", "message_id": 99},
            )

            actions = await store.get_recent_actions("g1", limit=5)

            assert action_id > 0
            assert len(actions) == 1
            assert actions[0]["success"] is True
            assert actions[0]["arguments"]["markdown_image_sha256"] == "abc"
            assert actions[0]["artifact"]["message_id"] == 99
        finally:
            await store.close()
            os.unlink(path)

    async def test_multiple_upserts_returns_latest(self):
        store, path = await make_store()
        try:
            await store.upsert_self_note("g1", "first note")
            await store.upsert_self_note("g1", "second note")
            note = await store.get_current_self_note("g1")
            assert "second note" in note["content"]
        finally:
            await store.close()
            os.unlink(path)


class TestSearch:
    async def test_search_memory_finds_content(self):
        store, path = await make_store()
        try:
            await store.save(StoredMessage(
                date="2026-06-22", group_key="g1", category="memory",
                content="user likes python programming"
            ))
            results = await store.search_memory("g1", "python")
            assert len(results) >= 1
        finally:
            await store.close()
            os.unlink(path)

    async def test_search_memory_no_results(self):
        store, path = await make_store()
        try:
            results = await store.search_memory("g1", "nonexistent_xyz")
            assert results == []
        finally:
            await store.close()
            os.unlink(path)
