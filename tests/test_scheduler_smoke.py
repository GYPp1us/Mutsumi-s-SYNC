import asyncio
import tempfile
import os
from src.mutsumi_sync.config import Config
from src.mutsumi_sync.memory.store import MessageStore
from src.mutsumi_sync.message.sender import Peer
from src.mutsumi_sync.message.receiver import MessageEvent
from src.mutsumi_sync.scheduler import PipelineScheduler
from src.mutsumi_sync.tools.registry import ToolRegistry


class FakeSender:
    def __init__(self):
        self.sent: list[str] = []
        self.pokes: list[str] = []

    async def send(self, peer: Peer, msg: str | list) -> dict:
        preview = str(msg)[:100]
        print(f"  [FAKE SEND] to {peer.peer_uid}: {preview}")
        self.sent.append(preview)
        return {"status": "ok"}

    async def send_poke(self, peer: Peer) -> dict:
        print(f"  [FAKE POKE] {peer.peer_uid}")
        self.pokes.append(peer.peer_uid)
        return {"status": "ok"}


def make_store():
    fd, path = tempfile.mkstemp(suffix=".db", prefix="mutsumi_test_")
    os.close(fd)
    store = MessageStore(db_path=path)
    return store, path


async def test_scheduler():
    config = Config.load("config.example.yaml")
    config.session.timeout = 0
    config.context.debounce_timeout = 0.05

    registry = ToolRegistry()
    sender = FakeSender()
    store, store_path = make_store()
    await store.initialize()
    scheduler = PipelineScheduler(config=config, registry=registry, sender=sender, store=store)

    event = MessageEvent(
        post_type="message",
        message_type="private",
        user_id=123456,
        message=[{"type": "text", "data": {"text": "hello test"}}],
        raw_message="hello test",
        message_id=1,
        sender={"user_id": 123456, "nickname": "test"},
    )

    print("Dispatching message...")
    await scheduler.dispatch(event)
    await asyncio.sleep(0.3)

    count = await store.count()
    print(f"Store count: {count}")

    print(f"Active keys: {scheduler.active_keys()}")
    print(f"Status: {scheduler.status()}")
    print(f"Sent messages: {len(sender.sent)}")
    print(f"Pokes: {len(sender.pokes)}")

    assert "private:123456" in scheduler._windows
    assert "private:123456" in scheduler._sessions
    assert count >= 1, f"Expected at least 1 message in store, got {count}"
    print("ALL ASSERTIONS PASSED")
    await store.close()
    os.unlink(store_path)


async def test_cancel():
    config = Config.load("config.example.yaml")
    registry = ToolRegistry()
    sender = FakeSender()
    store, store_path = make_store()
    await store.initialize()
    scheduler = PipelineScheduler(config=config, registry=registry, sender=sender, store=store)

    key = "private:999"
    scheduler._ensure_user_state(key)

    task_was_cancelled = False

    async def slow_task():
        nonlocal task_was_cancelled
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            task_was_cancelled = True
            raise

    task = asyncio.create_task(slow_task())
    scheduler._tasks[key] = task
    await asyncio.sleep(0)

    print("\nCancelling task for key=%s..." % key)
    await scheduler.cancel_user(key)
    await asyncio.sleep(0)

    assert task_was_cancelled, "Task should have been cancelled"
    assert key not in scheduler._tasks or scheduler._tasks[key].done(), \
        "Task should be removed or done"
    print("CANCEL TEST PASSED")
    await store.close()
    os.unlink(store_path)


async def test_group_key():
    config = Config.load("config.example.yaml")
    config.context.debounce_timeout = 0.05
    registry = ToolRegistry()
    sender = FakeSender()
    store, store_path = make_store()
    await store.initialize()
    scheduler = PipelineScheduler(config=config, registry=registry, sender=sender, store=store)

    group_event = MessageEvent(
        post_type="message",
        message_type="group",
        user_id=111,
        group_id=888,
        message=[{"type": "text", "data": {"text": "group hello"}}],
        raw_message="group hello",
        message_id=4,
        sender={"user_id": 111, "nickname": "group_user"},
    )

    print("\nDispatching group message...")
    await scheduler.dispatch(group_event)
    await asyncio.sleep(0.3)

    msgs = await store.get_context_for_group("group:888:111")
    print(f"Group messages: {len(msgs)}")
    assert len(msgs) >= 1

    assert "group:888:111" in scheduler._windows, f"Expected group:888:111, got {list(scheduler._windows.keys())}"
    print("GROUP KEY TEST PASSED")
    await store.close()
    os.unlink(store_path)


async def test_shutdown_does_not_write_placeholder_summaries():
    config = Config.load("config.example.yaml")
    registry = ToolRegistry()
    sender = FakeSender()
    store, store_path = make_store()
    await store.initialize()
    scheduler = PipelineScheduler(config=config, registry=registry, sender=sender, store=store)

    key = "private:shutdown"
    scheduler._ensure_user_state(key)
    scheduler._windows[key].add(user_id=key, message="important user context")
    scheduler._windows[key].add(user_id=key, message="important assistant context", is_bot=True)

    await scheduler.shutdown()

    reopened = MessageStore(db_path=store_path)
    await reopened.initialize()
    try:
        summaries = await reopened.get_summaries(key)
        assert all("archived on shutdown" not in s["summary"] for s in summaries)
        assert summaries == []
    finally:
        await reopened.close()
        os.unlink(store_path)


async def main():
    await test_scheduler()
    await test_cancel()
    await test_group_key()
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
