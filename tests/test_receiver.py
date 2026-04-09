import pytest
from src.mutsumi_sync.message.receiver import MessageReceiver, MessageEvent


def test_message_event_parsing():
    data = {
        "post_type": "message",
        "message_type": "group",
        "user_id": 123456,
        "group_id": 789,
        "message": [{"type": "text", "data": {"text": "hello"}}],
        "raw_message": "hello",
        "message_id": 111,
        "sender": {"user_id": 123456, "nickname": "test"}
    }
    event = MessageEvent(**data)
    assert event.user_id == 123456
    assert event.group_id == 789
    assert event.message_type == "group"
