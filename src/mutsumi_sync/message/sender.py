from __future__ import annotations

import logging
from typing import Iterable

import httpx
from pydantic import BaseModel

logger = logging.getLogger("mutsumi.sender")


class Peer(BaseModel):
    chat_type: int  # 1: private, 2: group
    peer_uid: str


class MessageSender:
    def __init__(self, http_url: str, access_token: str = ""):
        self.http_url = http_url.rstrip("/")
        self.access_token = access_token

    def _token_url(self, path: str) -> str:
        url = f"{self.http_url}{path}"
        if self.access_token:
            url = f"{url}?access_token={self.access_token}"
        return url

    async def send(self, peer: Peer, message: str | list[dict]) -> dict:
        if isinstance(message, str):
            segments = [{"type": "text", "data": {"text": message}}]
        else:
            segments = message

        if peer.chat_type == 1:
            url = self._token_url("/send_private_msg")
            body = {"user_id": peer.peer_uid, "message": segments}
        else:
            url = self._token_url("/send_group_msg")
            body = {"group_id": peer.peer_uid, "message": segments}

        label = "private" if peer.chat_type == 1 else "group"
        preview = _preview(segments)
        logger.info("[SEND] %s: %s", label, preview)

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(url, json=body, timeout=10)
                result = resp.json()
                if resp.status_code == 200 and result.get("status") == "ok":
                    logger.info("[SEND OK] message_id:%s",
                                result.get("data", {}).get("message_id", "?"))
                else:
                    logger.error("[SEND FAIL] status:%s result:%s",
                                 resp.status_code, result)
                return result
        except Exception:
            logger.exception("[SEND ERROR]")
            return {"status": "error"}

    async def send_poke(self, peer: Peer) -> dict:
        label = "private" if peer.chat_type == 1 else "group"
        logger.info("[POKE] %s:%s", label, peer.peer_uid)

        if peer.chat_type == 1:
            url = self._token_url("/friend_poke")
            body = {"user_id": peer.peer_uid}
        else:
            url = self._token_url("/group_poke")
            body = {"group_id": peer.peer_uid, "user_id": peer.peer_uid}

        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(url, json=body, timeout=10)
                result = resp.json()
                if resp.status_code != 200 or result.get("status") != "ok":
                    logger.warning("[POKE FAIL] %s", result)
                return result
        except Exception:
            logger.exception("[POKE ERROR]")
            return {"status": "error"}


def _preview(segments: Iterable[dict]) -> str:
    for seg in segments:
        if seg.get("type") == "text":
            text = seg.get("data", {}).get("text", "")
            return text[:50]
    return "[non-text]"
