import tempfile
import os
import asyncio
from src.mutsumi_sync.memory.store import MessageStore, StoredMessage, MessageCategory


class TestMessageStore:
    @staticmethod
    async def make_store() -> tuple[MessageStore, str]:
        fd, path = tempfile.mkstemp(suffix=".db", prefix="mutsumi_test_")
        os.close(fd)
        store = MessageStore(db_path=path)
        await store.initialize()
        return store, path

    async def test_initialize_creates_tables(self):
        store, store_path = await self.make_store()
        try:
            messages = await store.get_messages(limit=1)
            assert messages == []
        finally:
            await store.close()
            os.unlink(store_path)

    async def test_save_and_retrieve(self):
        store, store_path = await self.make_store()
        try:
            msg = StoredMessage(
                date="2026-06-10",
                group_key="private:123",
                category=MessageCategory.TEXT,
                content="hello world",
            )
            msg_id = await store.save(msg)
            assert msg_id == 1

            results = await store.get_messages(group_key="private:123")
            assert len(results) == 1
            assert results[0].id == 1
            assert results[0].content == "hello world"
            assert results[0].category == MessageCategory.TEXT
        finally:
            await store.close()
            os.unlink(store_path)

    async def test_get_context_for_group(self):
        store, store_path = await self.make_store()
        try:
            await store.save(StoredMessage(date="2026-06-10", group_key="private:1", category="text", content="msg1"))
            await store.save(StoredMessage(date="2026-06-10", group_key="private:1", category="text", content="msg2"))
            await store.save(StoredMessage(date="2026-06-10", group_key="private:2", category="text", content="other"))

            results = await store.get_context_for_group("private:1")
            assert len(results) == 2
            assert results[0].content == "msg2"  # most recent first
            assert results[1].content == "msg1"
        finally:
            await store.close()
            os.unlink(store_path)

    async def test_filter_by_date(self):
        store, store_path = await self.make_store()
        try:
            await store.save(StoredMessage(date="2026-06-09", group_key="g", category="text", content="old"))
            await store.save(StoredMessage(date="2026-06-10", group_key="g", category="text", content="new"))
            await store.save(StoredMessage(date="2026-06-11", group_key="g", category="text", content="newest"))

            results = await store.get_messages(date_from="2026-06-10", date_to="2026-06-10")
            assert len(results) == 1
            assert results[0].content == "new"
        finally:
            await store.close()
            os.unlink(store_path)

    async def test_filter_by_category(self):
        store, store_path = await self.make_store()
        try:
            await store.save(StoredMessage(date="2026-06-10", group_key="g", category="text", content="t1"))
            await store.save(StoredMessage(date="2026-06-10", group_key="g", category="image", content="img1"))
            await store.save(StoredMessage(date="2026-06-10", group_key="g", category="text", content="t2"))

            results = await store.get_messages(category="text")
            assert len(results) == 2
        finally:
            await store.close()
            os.unlink(store_path)

    async def test_save_media(self):
        store, store_path = await self.make_store()
        try:
            data = b"\x89PNG test image data"
            msg_id = await store.save_media(group_key="g1", category="image", data=data, ext="png")
            assert msg_id == 1

            results = await store.get_messages(group_key="g1")
            assert len(results) == 1
            assert results[0].category == "image"
            assert "file" in results[0].content
        finally:
            await store.close()
            os.unlink(store_path)

    async def test_count(self):
        store, store_path = await self.make_store()
        try:
            assert await store.count() == 0
            await store.save(StoredMessage(date="2026-06-10", group_key="g", category="text", content="x"))
            await store.save(StoredMessage(date="2026-06-10", group_key="g", category="text", content="y"))
            assert await store.count() == 2
            assert await store.count(group_key="g") == 2
            assert await store.count(group_key="other") == 0
        finally:
            await store.close()
            os.unlink(store_path)

    async def test_not_initialized_raises(self):
        store = MessageStore(db_path="/tmp/nonexistent_test.db")
        try:
            await store.save(StoredMessage(date="2026-06-10", group_key="g", category="text", content="x"))
            assert False, "Should have raised"
        except RuntimeError:
            pass

    async def test_limit_and_offset(self):
        store, store_path = await self.make_store()
        try:
            for i in range(5):
                await store.save(StoredMessage(date="2026-06-10", group_key="g", category="text", content=f"msg{i}"))

            results = await store.get_messages(limit=3)
            assert len(results) == 3

            results = await store.get_messages(limit=2, offset=2)
            assert len(results) == 2
        finally:
            await store.close()
            os.unlink(store_path)
