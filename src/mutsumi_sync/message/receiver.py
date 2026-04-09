import asyncio
import json
from typing import Callable, Optional
from websockets import connect, ClientProtocol
from pydantic import BaseModel


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
    def __init__(self, ws_url: str):
        self.ws_url = ws_url
        self._ws: Optional[ClientProtocol] = None
        self._handler: Optional[Callable] = None

    def on_message(self, handler: Callable[[MessageEvent], None]):
        self._handler = handler

    async def connect(self):
        self._ws = await connect(self.ws_url)

    async def run(self):
        if not self._ws:
            await self.connect()
        async for raw in self._ws:
            try:
                data = json.loads(raw)
                if data.get("post_type") == "message":
                    event = MessageEvent(**data)
                    if self._handler:
                        self._handler(event)
            except Exception as e:
                print(f"Error parsing message: {e}")

    async def close(self):
        if self._ws:
            await self._ws.close()
