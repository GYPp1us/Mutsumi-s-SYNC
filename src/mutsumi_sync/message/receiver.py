from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable
from urllib.parse import parse_qs, urlparse, urlunparse

from pydantic import BaseModel, ConfigDict
from websockets import connect
from websockets.asyncio.client import ClientConnection
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger("mutsumi.receiver")


class MessageEvent(BaseModel):
    """NapCat WebSocket 消息事件的忠实映射。"""

    post_type: str
    message_type: str
    user_id: int
    group_id: int | None = None
    message: list[dict]
    raw_message: str
    message_id: int
    sender: dict
    time: int = 0
    self_id: int = 0
    sub_type: str = ""
    message_seq: int | None = None

    model_config = ConfigDict(extra="ignore")


class MessageReceiver:
    def __init__(self, ws_url: str, access_token: str = ""):
        self.ws_url = ws_url
        self.access_token = access_token
        self._ws: ClientConnection | None = None
        self._handler: Callable[[MessageEvent], Awaitable[None]] | None = None
        self._running = False

    @property
    def is_connected(self) -> bool:
        return self._ws is not None and self._running

    def on_message(self, handler: Callable[[MessageEvent], Awaitable[None]]) -> None:
        self._handler = handler

    async def _build_url(self) -> str:
        parsed = urlparse(self.ws_url)
        query_params = parse_qs(parsed.query)

        if self.access_token:
            query_params["accessToken"] = [self.access_token]

        new_query = "&".join(f"{k}={v[0]}" for k, v in query_params.items())
        return urlunparse((
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            new_query or None,
            parsed.fragment,
        ))

    async def connect(self) -> None:
        url = await self._build_url()
        logger.info("Connecting to %s", url)
        self._ws = await connect(url)
        logger.info("Connected to WebSocket")

    async def _drop_connection(self) -> None:
        ws = self._ws
        self._ws = None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                logger.debug("Ignoring error while closing stale WebSocket", exc_info=True)

    async def run(self) -> None:
        self._running = True
        reconnect_delay = 1

        while self._running:
            try:
                if not self._ws:
                    await self.connect()
                    reconnect_delay = 1

                async for raw in self._ws:
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        logger.error("JSON decode error for: %s", raw[:200])
                        continue

                    post_type = data.get("post_type", "")

                    if post_type == "message":
                        try:
                            event = MessageEvent(**data)
                        except Exception:
                            logger.exception("Failed to parse MessageEvent")
                            continue
                        logger.info("[MSG] From:%s type:%s msg:%s",
                                     event.user_id, event.message_type,
                                     event.raw_message[:50])
                        if self._handler:
                            asyncio.create_task(_safe_handler(self._handler, event))
                    elif post_type == "meta_event":
                        logger.debug("Meta event: %s", data.get("meta_event_type"))
                    elif post_type == "notice":
                        logger.debug("Notice event: %s", data.get("notice_type"))
                    elif data.get("status") == "failed":
                        logger.warning("[WS ERR] retcode:%s echo:%s",
                                       data.get("retcode"), data.get("echo", "N/A"))

                logger.warning("Connection ended, reconnecting...")
                await self._drop_connection()
            except ConnectionClosed as e:
                logger.warning("Connection closed, reconnecting: %s", e)
                await self._drop_connection()
            except Exception:
                logger.exception("Connection error")
                await self._drop_connection()

            if self._running:
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 30)
                logger.info("Reconnecting in %ds...", reconnect_delay)

    async def close(self) -> None:
        self._running = False
        await self._drop_connection()
        logger.info("Connection closed")


async def _safe_handler(handler, event: MessageEvent) -> None:
    try:
        await handler(event)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Unhandled error in message handler")
