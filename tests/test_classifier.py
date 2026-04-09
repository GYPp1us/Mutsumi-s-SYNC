from src.mutsumi_sync.message.classifier import classify_message, MessageType


def test_short_text_classification():
    message = [{"type": "text", "data": {"text": "hello"}}]
    result = classify_message(message, "hello")
    assert result.msg_type == MessageType.SHORT_TEXT
    assert result.content == "hello"


def test_long_text_classification():
    message = [{"type": "text", "data": {"text": "a" * 100}}]
    result = classify_message(message, "a" * 100)
    assert result.msg_type == MessageType.LONG_TEXT


def test_image_classification():
    message = [{"type": "image", "data": {"file": "abc.jpg"}}]
    result = classify_message(message, "[图片]")
    assert result.msg_type == MessageType.IMAGE
