from __future__ import annotations

from enum import Enum

from pydantic import BaseModel


class MessageType(Enum):
    SHORT_TEXT = "short_text"
    LONG_TEXT = "long_text"
    IMAGE = "image"
    MEDIA = "media"


class ClassifiedMessage(BaseModel):
    raw: str
    msg_type: MessageType
    content: str | None = None
    image_file: str | None = None
    image_url: str | None = None


def classify_message(message: list[dict], raw_message: str) -> ClassifiedMessage:
    text_parts: list[str] = []

    for seg in message:
        seg_type = seg.get("type", "")

        if seg_type == "text":
            text_parts.append(seg.get("data", {}).get("text", ""))

        elif seg_type == "image":
            data = seg.get("data", {})
            return ClassifiedMessage(
                raw=raw_message,
                msg_type=MessageType.IMAGE,
                image_file=data.get("file"),
                image_url=data.get("url"),
            )

        elif seg_type in ("record", "video", "forward"):
            return ClassifiedMessage(
                raw=raw_message,
                msg_type=MessageType.MEDIA,
            )

    combined = "".join(text_parts)
    if len(combined) < 50:
        return ClassifiedMessage(
            raw=raw_message,
            msg_type=MessageType.SHORT_TEXT,
            content=combined,
        )
    else:
        return ClassifiedMessage(
            raw=raw_message,
            msg_type=MessageType.LONG_TEXT,
            content=combined,
        )
