import asyncio
import logging
import httpx
from typing import Union, Optional
from urllib.parse import urljoin
from pydantic import BaseModel

logger = logging.getLogger("mutsumi.sender")


class Peer(BaseModel):
    chat_type: int
    peer_uid: str


class MessageSender:
    def __init__(self, http_url: str, access_token: str = None):
        self.http_url = http_url.rstrip('/')
        self.access_token = access_token

    async def send(self, peer: Peer, message: Union[str, list]) -> dict:
        if isinstance(message, str):
            message = [{"type": "text", "data": {"text": message}}]
        
        url = f"{self.http_url}/send_private_msg" if peer.chat_type == 1 else f"{self.http_url}/send_group_msg"
        
        params = {}
        if peer.chat_type == 1:
            params["user_id"] = peer.peer_uid
        else:
            params["group_id"] = peer.peer_uid
        params["message"] = message
        
        if self.access_token:
            url = f"{url}?access_token={self.access_token}"
            logger.info(f"\033[33m[TOKEN]\033[0m HTTP URL with token: {url}")
        
        msg_type = "private" if peer.chat_type == 1 else "group"
        logger.info(f"[SEND] Sending to {msg_type}: {message[0]['data']['text'][:30]}...")
        
        try:
            async with httpx.AsyncClient() as client:
                resp = await client.post(url, json=params, timeout=10)
                result = resp.json()
                
                if resp.status_code == 200 and result.get("status") == "ok":
                    logger.info(f"\033[32m[SEND OK]\033[0m message_id: {result.get('data', {}).get('message_id', 'unknown')}")
                else:
                    logger.error(f"\033[31m[SEND FAIL]\033[0m status: {resp.status_code}, result: {result}")
                
                return result
        except Exception as e:
            logger.error(f"\033[31m[SEND ERROR]\033[0m {e}")
            return {"status": "error", "message": str(e)}
