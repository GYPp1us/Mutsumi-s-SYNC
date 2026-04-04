import asyncio
from .config import load_config
from .message.receiver import MessageReceiver, MessageEvent
from .message.classifier import classify_message, MessageType
from .message.sender import MessageSender, Peer
from .processor.pipeline import ModelPipeline
from .processor.vector import VectorMatcher
from .processor.dedup import Deduplicator
from .processor.auth import AuthManager
from .cache.meme import MemeCache
from .memory.window import MessageWindow


class MutsumiSync:
    def __init__(self, config):
        self.config = config
        self.receiver = MessageReceiver(config.napcat.ws_url)
        self.sender = MessageSender(config.napcat.http_url)
        self.pipeline = ModelPipeline(config.model.model, config.model.temperature)
        self.matcher = VectorMatcher(config.memory.vector_dim)
        self.dedup = Deduplicator(config.deduplication.wait_time)
        self.auth = AuthManager()
        self.meme_cache = MemeCache(config.cache.meme_desc)
        self.window = MessageWindow(config.context.window_size)

    async def handle_message(self, event: MessageEvent):
        classified = classify_message(event.message, event.raw_message)
        
        should_reply = await self.dedup.should_reply(event.user_id, event.group_id)
        if not should_reply:
            return
        
        if classified.msg_type == MessageType.SHORT_TEXT:
            if not self.matcher.is_empty():
                pass
        
        if classified.msg_type in (MessageType.SHORT_TEXT, MessageType.LONG_TEXT):
            context = self.window.get_context()
            response = await self.pipeline.chat(classified.content or "", context=context)
            
            peer = Peer(
                chat_type=2 if event.group_id else 1,
                peer_uid=str(event.group_id or event.user_id)
            )
            await self.sender.send(peer, response)
            
            self.window.add(event.user_id, event.raw_message)
            self.window.add(event.user_id, response, is_bot=True)

    async def run(self):
        self.receiver.on_message(self.handle_message)
        await self.receiver.run()


async def main():
    config = load_config()
    print("Mutsumi's SYNC started")
    bot = MutsumiSync(config)
    await bot.run()


if __name__ == "__main__":
    asyncio.run(main())
