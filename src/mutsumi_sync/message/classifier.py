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
    media_kind: str = "image"


def classify_message(message: list[dict], raw_message: str) -> ClassifiedMessage:
    text_parts: list[str] = []
    image_file: str | None = None
    image_url: str | None = None
    media_kind = "image"
    has_image = False
    has_media = False

    for seg in message:
        seg_type = seg.get("type", "")

        if seg_type == "text":
            text_parts.append(seg.get("data", {}).get("text", ""))

        elif seg_type == "image":
            data = seg.get("data", {})
            has_image = True
            image_file = image_file or data.get("file")
            image_url = image_url or data.get("url")
            summary = str(data.get("summary") or data.get("sub_type") or "").lower()
            if "sticker" in summary or "face" in summary or "表情" in summary:
                media_kind = "sticker"

        elif seg_type in ("record", "video", "forward"):
            has_media = True

    combined = "".join(text_parts)
    if has_media:
        return ClassifiedMessage(raw=raw_message, msg_type=MessageType.MEDIA, content=combined or None)
    if has_image:
        return ClassifiedMessage(
            raw=raw_message,
            msg_type=MessageType.IMAGE,
            content=combined,
            image_file=image_file,
            image_url=image_url,
            media_kind=media_kind,
        )
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
