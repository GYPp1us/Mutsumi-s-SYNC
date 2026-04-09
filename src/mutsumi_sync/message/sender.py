import httpx
from typing import Union, Optional
from pydantic import BaseModel


class Peer(BaseModel):
    chat_type: int  # 1: private, 2: group
    peer_uid: str


class MessageSender:
    def __init__(self, http_url: str):
        self.http_url = http_url

    async def send(self, peer: Peer, message: Union[str, list]) -> dict:
        """发送消息"""
        if isinstance(message, str):
            message = [{"type": "text", "data": {"text": message}}]
        
        params = {
            "message_type": "private" if peer.chat_type == 1 else "group",
            "message": message
        }
        if peer.chat_type == 1:
            params["user_id"] = peer.peer_uid
        else:
            params["group_id"] = peer.peer_uid
        
        async with httpx.AsyncClient() as client:
            resp = await client.post(f"{self.http_url}/send_msg", json=params)
            return resp.json()
