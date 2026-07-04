from __future__ import annotations

import asyncio
import json

import src.mutsumi_sync.message.receiver as receiver_module
from src.mutsumi_sync.message.receiver import MessageEvent, MessageReceiver
from websockets.exceptions import ConnectionClosedOK


class ClosedWebSocket:
    def __aiter__(self):
        return self

    async def __anext__(self):
        raise ConnectionClosedOK(None, None)

    async def close(self) -> None:
        return None


class OneMessageWebSocket:
    def __init__(self, payload: dict):
        self._payload = payload
        self._sent = False
        self._closed = asyncio.Event()

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._sent:
            self._sent = True
            return json.dumps(self._payload)
        await self._closed.wait()
        raise StopAsyncIteration

    async def close(self) -> None:
        self._closed.set()


async def test_receiver_reconnects_with_a_fresh_websocket_after_close(monkeypatch):
    payload = {
        "post_type": "message",
        "message_type": "private",
        "user_id": 123,
        "message": [{"type": "text", "data": {"text": "hello"}}],
        "raw_message": "hello",
        "message_id": 1,
        "sender": {"user_id": 123, "nickname": "test"},
    }
    websockets = [
        ClosedWebSocket(),
        OneMessageWebSocket(payload),
    ]
    connect_calls: list[str] = []

    async def fake_connect(url: str):
        connect_calls.append(url)
        return websockets.pop(0)

    monkeypatch.setattr(receiver_module, "connect", fake_connect)

    receiver = MessageReceiver("ws://example.test/ws")
    events: list[MessageEvent] = []

    async def handler(event: MessageEvent) -> None:
        events.append(event)
        await receiver.close()

    receiver.on_message(handler)
    task = asyncio.create_task(receiver.run())

    for _ in range(20):
        if events:
            break
        await asyncio.sleep(0.1)

    await receiver.close()
    await asyncio.wait_for(task, timeout=1)

    assert len(connect_calls) == 2
    assert [event.raw_message for event in events] == ["hello"]
