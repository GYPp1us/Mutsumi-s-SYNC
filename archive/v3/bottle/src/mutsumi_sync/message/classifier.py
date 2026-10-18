from enum import Enum
from typing import Optional
from pydantic import BaseModel


class MessageType(Enum):
    SHORT_TEXT = "short_text"
    LONG_TEXT = "long_text"
    IMAGE = "image"
    MEME = "meme"


class ClassifiedMessage(BaseModel):
    raw: str
    msg_type: MessageType
    content: Optional[str] = None
    image_md5: Optional[str] = None


def classify_message(message: list, raw_message: str) -> ClassifiedMessage:
    """分类消息类型"""
    # 检查图片
    for seg in message:
        if seg.get("type") == "image":
            return ClassifiedMessage(
                raw=raw_message,
                msg_type=MessageType.IMAGE,
                image_md5=seg.get("data", {}).get("file", "").split(".")[-1]
            )
    
    # 检查文字长度
    text_len = len(raw_message)
    if text_len < 50:
        return ClassifiedMessage(
            raw=raw_message,
            msg_type=MessageType.SHORT_TEXT,
            content=raw_message
        )
    else:
        return ClassifiedMessage(
            raw=raw_message,
            msg_type=MessageType.LONG_TEXT,
            content=raw_message
        )
