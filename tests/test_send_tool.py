import pytest
from src.mutsumi_sync.tools.send import send_tool, SEND_TOOL_SCHEMA
from src.mutsumi_sync.message.sender import Peer


class FakeSender:
    def __init__(self):
        self.last_peer = None
        self.last_segments = None

    async def send(self, peer, segments):
        self.last_peer = peer
        self.last_segments = segments
        return {"status": "ok", "data": {"message_id": 12345}}


class TestSendTool:
    async def test_send_text(self):
        sender = FakeSender()
        peer = Peer(chat_type=1, peer_uid="12345")
        result = await send_tool({"text": "hello"}, sender=sender, peer=peer)
        assert "ok" in result.lower() or "12345" in result
        assert sender.last_segments[0]["type"] == "text"
        assert sender.last_segments[0]["data"]["text"] == "hello"

    async def test_send_no_content_error(self):
        sender = FakeSender()
        peer = Peer(chat_type=1, peer_uid="12345")
        result = await send_tool({}, sender=sender, peer=peer)
        assert result.startswith("[Error:")

    async def test_send_with_image(self):
        sender = FakeSender()
        peer = Peer(chat_type=1, peer_uid="12345")
        result = await send_tool(
            {"text": "see this", "image": "test.png"},
            sender=sender, peer=peer,
        )
        assert "ok" in result.lower()
        assert len(sender.last_segments) == 2

    async def test_send_with_face(self):
        sender = FakeSender()
        peer = Peer(chat_type=1, peer_uid="12345")
        result = await send_tool(
            {"text": "smile", "face": 1},
            sender=sender, peer=peer,
        )
        assert len(sender.last_segments) == 2
        assert sender.last_segments[1]["type"] == "face"

    def test_schema_valid(self):
        assert SEND_TOOL_SCHEMA["type"] == "object"
        assert "text" in SEND_TOOL_SCHEMA["properties"]
