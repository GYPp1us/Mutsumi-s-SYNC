from __future__ import annotations

import asyncio
import json

import pytest

from src.mutsumi_sync.config import IngressConfig
from src.mutsumi_sync.ingress import ServiceIngress
from src.mutsumi_sync.memory.store import MessageStore
from src.mutsumi_sync.message.receiver import ServiceMessageEvent


async def _post(port: int, body: dict, *, token: str = "secret", content_type: str = "application/json") -> tuple[int, dict]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = (
        f"POST /v1/events HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        f"Authorization: Bearer {token}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(raw)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode("ascii") + raw
    writer.write(request)
    await writer.drain()
    response = await reader.read()
    writer.close()
    await writer.wait_closed()
    head, payload = response.split(b"\r\n\r\n", 1)
    status = int(head.splitlines()[0].split()[1])
    return status, json.loads(payload.decode("utf-8"))


def _event(message_id: str = "calendar-1") -> dict:
    return {
        "post_type": "message",
        "message_type": "private",
        "user_id": "calendar",
        "message_id": message_id,
        "message": [{"type": "text", "data": {"text": "明天九点开会"}}],
        "raw_message": "明天九点开会",
        "sender": {"nickname": "日程服务"},
        "time": 1785400000,
    }


@pytest.mark.asyncio
async def test_ingress_auth_persists_and_deduplicates_service_events():
    store = MessageStore(db_path=":memory:")
    await store.initialize()
    received: list[tuple[ServiceMessageEvent, str]] = []
    ingress = ServiceIngress(
        IngressConfig(enabled=True, host="127.0.0.1", port=0, token="secret", target_user_id="3535616589"),
        store,
        lambda event, event_id: _record(received, event, event_id),
    )
    await ingress.start()
    port = ingress._server.sockets[0].getsockname()[1]
    try:
        status, body = await _post(port, _event(), token="wrong")
        assert status == 401
        assert body["status"] == "failed"
        assert received == []

        status, body = await _post(port, _event())
        assert status == 202
        assert body["data"]["duplicate"] is False
        assert len(received) == 1
        event, event_id = received[0]
        assert event.source_kind == "service"
        assert event.user_id == "calendar"
        assert event_id == body["data"]["event_id"]

        stored = await store.get_event(event_id)
        assert stored is not None
        assert stored.actor_id == "service:calendar"
        assert stored.actor_kind == "service"
        assert stored.status == "received"
        actor = await store.get_actor("service:calendar")
        assert actor is not None
        assert actor.display_name == "日程服务"

        status, body = await _post(port, _event())
        assert status == 202
        assert body["data"]["duplicate"] is True
        # A retry is re-enqueued while the durable event is still received.
        # The scheduler deduplicates the event ID before execution.
        assert len(received) == 2

        await store.update_event_status(event_id, "finalized")
        status, body = await _post(port, _event())
        assert status == 202
        assert body["data"]["duplicate"] is True
        assert len(received) == 2
    finally:
        await ingress.close()
        await store.close()


@pytest.mark.asyncio
async def test_ingress_rejects_non_private_or_non_json_events():
    store = MessageStore(db_path=":memory:")
    await store.initialize()
    ingress = ServiceIngress(
        IngressConfig(enabled=True, host="127.0.0.1", port=0, token="secret", target_user_id="3535616589"),
        store,
        lambda event, event_id: asyncio.sleep(0),
    )
    await ingress.start()
    port = ingress._server.sockets[0].getsockname()[1]
    try:
        invalid = _event()
        invalid["message_type"] = "group"
        status, body = await _post(port, invalid)
        assert status == 400
        assert "private" in body["message"]

        status, body = await _post(port, _event(), content_type="text/plain")
        assert status == 415
        assert body["status"] == "failed"
    finally:
        await ingress.close()
        await store.close()


@pytest.mark.asyncio
async def test_ingress_concurrent_same_event_is_idempotent():
    store = MessageStore(db_path=":memory:")
    await store.initialize()
    received: list[str] = []

    async def enqueue(event: ServiceMessageEvent, event_id: str) -> None:
        await asyncio.sleep(0)
        received.append(event_id)

    ingress = ServiceIngress(
        IngressConfig(enabled=True, host="127.0.0.1", port=0, token="secret", target_user_id="3535616589"),
        store,
        enqueue,
    )
    await ingress.start()
    port = ingress._server.sockets[0].getsockname()[1]
    try:
        responses = await asyncio.gather(_post(port, _event()), _post(port, _event()))
        assert {status for status, _ in responses} == {202}
        assert sum(body["data"]["duplicate"] for _, body in responses) == 1
        assert len(await store.get_events(finalized_only=False)) == 1
    finally:
        await ingress.close()
        await store.close()


@pytest.mark.asyncio
async def test_ingress_rejects_local_files_and_unsupported_segments():
    store = MessageStore(db_path=":memory:")
    await store.initialize()
    ingress = ServiceIngress(
        IngressConfig(enabled=True, host="127.0.0.1", port=0, token="secret", target_user_id="3535616589"),
        store,
        lambda event, event_id: asyncio.sleep(0),
    )
    await ingress.start()
    port = ingress._server.sockets[0].getsockname()[1]
    try:
        local_image = _event("local-image")
        local_image["message"] = [{"type": "image", "data": {"file": "/etc/passwd"}}]
        status, body = await _post(port, local_image)
        assert status == 400
        assert "local file" in body["message"]

        unsupported = _event("unsupported")
        unsupported["message"] = [{"type": "record", "data": {"file": "audio.mp3"}}]
        status, body = await _post(port, unsupported)
        assert status == 400
        assert "unsupported" in body["message"]
    finally:
        await ingress.close()
        await store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "host,target,error",
    [
        ("0.0.0.0", "3535616589", "loopback"),
        ("127.0.0.1", "owner", "QQ number"),
    ],
)
async def test_ingress_rejects_unsafe_bind_or_target(host: str, target: str, error: str):
    store = MessageStore(db_path=":memory:")
    await store.initialize()
    ingress = ServiceIngress(
        IngressConfig(enabled=True, host=host, port=0, token="secret", target_user_id=target),
        store,
        lambda event, event_id: asyncio.sleep(0),
    )
    try:
        with pytest.raises(ValueError, match=error):
            await ingress.start()
    finally:
        await ingress.close()
        await store.close()


async def _record(received: list, event: ServiceMessageEvent, event_id: str) -> None:
    received.append((event, event_id))
