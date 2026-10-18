import asyncio
import json
import logging
from typing import Callable, Optional, Awaitable
from urllib.parse import urlparse, parse_qs, urlunparse
from websockets import connect, ClientProtocol
from websockets.exceptions import ConnectionClosed
from pydantic import BaseModel

logger = logging.getLogger("mutsumi.receiver")


class MessageEvent(BaseModel):
    post_type: str
    message_type: str
    user_id: int
    group_id: Optional[int] = None
    message: list
    raw_message: str
    message_id: int
    sender: dict


class MessageReceiver:
    def __init__(self, ws_url: str, access_token: str = None):
        self.ws_url = ws_url
        self.access_token = access_token
        self._ws: Optional[ClientProtocol] = None
        self._handler: Optional[Callable[..., Awaitable[None]]] = None
        self._running = False

    def on_message(self, handler: Callable[..., Awaitable[None]]):
        self._handler = handler

    async def connect(self):
        logger.info(f"Connecting to {self.ws_url}...")
        logger.info(f"access_token value: '{self.access_token}'")
        
        parsed = urlparse(self.ws_url)
        query_params = parse_qs(parsed.query)
        
        if self.access_token and self.access_token.strip():
            query_params['accessToken'] = [self.access_token]
            logger.info(f"\033[33m[TOKEN]\033[0m Adding accessToken to URL")
        else:
            logger.warning(f"\033[31m[TOKEN]\033[0m No access_token!")
        
        new_query = '&'.join([f"{k}={v[0]}" for k, v in query_params.items()])
        new_url = urlunparse((
            parsed.scheme, parsed.netloc, parsed.path, parsed.params, 
            new_query if new_query else None, parsed.fragment
        ))
        
        logger.info(f"Connecting to: {new_url}")
        
        self._ws = await connect(new_url)
        logger.info("Connected to WebSocket")

    async def run(self):
        self._running = True
        reconnect_delay = 1
        
        while self._running:
            try:
                if not self._ws:
                    await self.connect()
                
                async for raw in self._ws:
                    try:
                        data = json.loads(raw)
                        post_type = data.get("post_type")
                        
                        if post_type == "message":
                            event = MessageEvent(**data)
                            logger.info(f"\033[32m[MSG]\033[0m From:{event.user_id} msg:{event.raw_message[:30]}...")
                            if self._handler:
                                asyncio.create_task(self._handler(event))
                        elif post_type == "meta_event":
                            logger.debug(f"Meta event: {data.get('meta_event_type', 'unknown')}")
                        elif post_type == "notice":
                            logger.debug(f"Notice event: {data.get('notice_type', 'unknown')}")
                        elif post_type == "request":
                            logger.debug(f"Request event: {data.get('request_type', 'unknown')}")
                        elif data.get("status") == "failed":
                            retcode = data.get("retcode")
                            logger.warning(f"\033[31m[WS ERR]\033[0m retcode:{retcode} echo:{data.get('echo', 'N/A')}")
                        else:
                            logger.debug(f"Unknown event: {post_type}")
                    except json.JSONDecodeError as e:
                        logger.error(f"JSON decode error: {e}")
                    except Exception as e:
                        logger.error(f"Error processing message: {e}")
                        
            except ConnectionClosed:
                logger.warning("Connection closed, reconnecting...")
            except Exception as e:
                logger.error(f"Connection error: {e}")
            
            if self._running and reconnect_delay <= 30:
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, 30)
                logger.info(f"Reconnecting in {reconnect_delay}s...")

    async def close(self):
        self._running = False
        if self._ws:
            await self._ws.close()
            logger.info("Connection closed")