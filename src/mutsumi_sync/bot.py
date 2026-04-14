import asyncio
import logging
from .config import load_config
from .message.receiver import MessageReceiver, MessageEvent
from .message.classifier import classify_message, MessageType
from .message.sender import MessageSender, Peer
from .processor.pipeline import ModelPipeline
from .processor.vector import VectorMatcher
from .processor.dedup import Deduplicator
from .processor.auth import AuthManager
from .processor.tools import config_manager, http_api_call
from .cache.meme import MemeCache
from .memory.window import MessageWindow

logger = logging.getLogger("mutsumi")


class MutsumiSync:
    def __init__(self, config, log_callback=None):
        self.config = config
        self.log_callback = log_callback
        self.receiver = MessageReceiver(config.napcat.ws_url, config.napcat.access_token)
        self.sender = MessageSender(config.napcat.http_url, config.napcat.access_token)
        
        tools = [config_manager, http_api_call]
        self.pipeline = ModelPipeline(
            provider=config.model.provider,
            model=config.model.model,
            temperature=config.model.temperature,
            api_key=config.model.api_key,
            base_url=config.model.base_url,
            tools=tools
        )
        self.matcher = VectorMatcher(config.memory.vector_dim)
        self.dedup = Deduplicator(config.deduplication.wait_time)
        self.auth = AuthManager()
        self.meme_cache = MemeCache(config.cache.meme_desc)
        self.window = MessageWindow(config.context.window_size)

    def log(self, msg, level="info", color=""):
        full_msg = f"{color}{msg}\033[0m" if color else msg
        if self.log_callback:
            self.log_callback(full_msg)
        getattr(logger, level)(msg)

    async def handle_message(self, event: MessageEvent):
        self.log(f"\033[32m[MSG IN]\033[0m From user:{event.user_id} msg:{event.raw_message[:30]}...", "info", "\033[32m")
        
        self.log("[CLASSIFY] Classifying message...", "info", "\033[33m")
        classified = classify_message(event.message, event.raw_message)
        self.log(f"[CLASSIFY] Type: {classified.msg_type.value}", "info", "\033[33m")
        
        self.log("[DEDUP] Checking deduplication...", "info", "\033[33m")
        should_reply = await self.dedup.should_reply(event.user_id, event.group_id)
        
        if not should_reply:
            self.log("[DEDUP] Skipped - dedup block", "warning", "\033[31m")
            return
        
        self.log(f"[VECTOR] Matcher empty: {self.matcher.is_empty()}", "info", "\033[33m")
        
        if classified.msg_type in (MessageType.SHORT_TEXT, MessageType.LONG_TEXT):
            self.log(f"[MODEL] Calling model: {self.config.model.model}", "info", "\033[32m")
            
            context = self.window.get_context()
            self.log(f"[MODEL] Context length: {len(context)}", "info", "\033[33m")
            
            response = await self.pipeline.chat(
                classified.content or "",
                system_prompt=self.config.system_prompt,
                context=context
            )
            
            self.log(f"[MODEL] Response: {response[:50]}...", "info", "\033[32m")
            
            peer = Peer(
                chat_type=2 if event.group_id else 1,
                peer_uid=str(event.group_id or event.user_id)
            )
            
            self.log(f"[SEND] Sending to chat_type:{peer.chat_type} peer:{peer.peer_uid}", "info", "\033[33m")
            send_result = await self.sender.send(peer, response)
            
            if send_result.get("status") == "ok":
                self.log("[SEND] Message sent successfully", "info", "\033[32m")
            else:
                self.log(f"[SEND] Failed: {send_result}", "error", "\033[31m")
            
            self.window.add(event.user_id, event.raw_message)
            self.window.add(event.user_id, response, is_bot=True)

    async def run(self):
        self.receiver.on_message(self.handle_message)
        await self.receiver.run()