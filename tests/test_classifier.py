import pytest
from src.mutsumi_sync.message.classifier import (
    classify_message,
    MessageType,
    ClassifiedMessage,
)


class TestClassifier:
    def test_short_text(self):
        result = classify_message(
            [{"type": "text", "data": {"text": "hi"}}],
            "hi",
        )
        assert result.msg_type == MessageType.SHORT_TEXT
        assert result.content == "hi"

    def test_long_text(self):
        long_text = "x" * 60
        result = classify_message(
            [{"type": "text", "data": {"text": long_text}}],
            long_text,
        )
        assert result.msg_type == MessageType.LONG_TEXT
        assert result.content == long_text

    def test_image(self):
        result = classify_message(
            [{"type": "image", "data": {"file": "abc.jpg", "url": "https://example.com/img"}}],
            "[image]",
        )
        assert result.msg_type == MessageType.IMAGE
        assert result.image_file == "abc.jpg"
        assert result.image_url == "https://example.com/img"

    def test_image_overrides_text(self):
        result = classify_message(
            [
                {"type": "text", "data": {"text": "look at this"}},
                {"type": "image", "data": {"file": "photo.png"}},
            ],
            "look at this [image]",
        )
        assert result.msg_type == MessageType.IMAGE
        assert result.content == "look at this"

    def test_image_preserves_text_segments_after_image(self):
        result = classify_message(
            [
                {"type": "text", "data": {"text": "before "}},
                {"type": "image", "data": {"url": "https://example.com/photo.png"}},
                {"type": "text", "data": {"text": "after"}},
            ],
            "before [image] after",
        )

        assert result.msg_type == MessageType.IMAGE
        assert result.content == "before after"
        assert result.image_url == "https://example.com/photo.png"

    def test_record_is_media(self):
        result = classify_message(
            [{"type": "record", "data": {"file": "audio.amr"}}],
            "[voice]",
        )
        assert result.msg_type == MessageType.MEDIA

    def test_video_is_media(self):
        result = classify_message(
            [{"type": "video", "data": {"file": "movie.mp4"}}],
            "[video]",
        )
        assert result.msg_type == MessageType.MEDIA

    def test_forward_is_media(self):
        result = classify_message(
            [{"type": "forward", "data": {"id": "123"}}],
            "[forward]",
        )
        assert result.msg_type == MessageType.MEDIA

    def test_multiple_text_segments(self):
        result = classify_message(
            [
                {"type": "text", "data": {"text": "hello "}},
                {"type": "text", "data": {"text": "world"}},
            ],
            "hello world",
        )
        assert result.msg_type == MessageType.SHORT_TEXT
        assert result.content == "hello world"

    def test_empty_message(self):
        result = classify_message([], "")
        assert result.msg_type == MessageType.SHORT_TEXT
        assert result.content == ""

    def test_unknown_segment_ignored(self):
        result = classify_message(
            [{"type": "text", "data": {"text": "ok"}}, {"type": "unknown", "data": {}}],
            "ok",
        )
        assert result.msg_type == MessageType.SHORT_TEXT
        assert result.content == "ok"
