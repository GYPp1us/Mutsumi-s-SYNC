from __future__ import annotations

import asyncio
import logging
import sys

from .config import Config
from .memory.store import MessageStore
from .message.receiver import MessageReceiver
from .message.sender import MessageSender
from .scheduler import PipelineScheduler
from .tools.registry import Tool, ToolRegistry
from .tools.http_api import http_api_call, HTTP_API_SCHEMA
from .tools.config_manager import config_manager, CONFIG_MANAGER_SCHEMA
from .tools.memory import memory_search, memory_save, MEMORY_SEARCH_SCHEMA, MEMORY_SAVE_SCHEMA
from .tools.self_note import self_note_tool, SELF_NOTE_SCHEMA
from .tools.send import send_tool, SEND_TOOL_SCHEMA

logger = logging.getLogger("mutsumi.main")


def setup_logging(level: int = logging.INFO) -> None:
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)-5s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger("mutsumi")
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(handler)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)


def build_registry(config: Config, store: MessageStore) -> ToolRegistry:
    registry = ToolRegistry()

    registry.register(Tool(
        name="http_api_call",
        description="发送 HTTP 请求到任意 URL",
        parameters=HTTP_API_SCHEMA,
        handler=http_api_call,
    ))

    async def _config_manager(args: dict) -> str:
        return await config_manager(args, config=config)

    registry.register(Tool(
        name="config_manager",
        description="读取、修改、热重载配置",
        parameters=CONFIG_MANAGER_SCHEMA,
        handler=_config_manager,
    ))

    async def _memory_search(args: dict, **deps) -> str:
        return await memory_search(args, store=store, group_key=deps.get("group_key", ""))

    registry.register(Tool(
        name="memory_search",
        description="搜索长期记忆，用关键词查找过去保存的信息",
        parameters=MEMORY_SEARCH_SCHEMA,
        handler=_memory_search,
    ))

    async def _memory_save(args: dict, **deps) -> str:
        return await memory_save(args, store=store, group_key=deps.get("group_key", ""))

    registry.register(Tool(
        name="memory_save",
        description="保存一条信息到长期记忆",
        parameters=MEMORY_SAVE_SCHEMA,
        handler=_memory_save,
    ))

    async def _self_note(args: dict, **deps) -> str:
        return await self_note_tool(args, store=store, group_key=deps.get("group_key", ""))

    registry.register(Tool(
        name="self_note",
        description="管理对用户的私人印象。add:追加, replace:覆盖",
        parameters=SELF_NOTE_SCHEMA,
        handler=_self_note,
    ))

    async def _send(args: dict, **deps) -> str:
        return await send_tool(args, sender=deps.get("sender"), peer=deps.get("peer"))

    registry.register(Tool(
        name="send",
        description="发送消息到用户。支持 text/image/face/at/reply/forward 段类型。",
        parameters=SEND_TOOL_SCHEMA,
        handler=_send,
    ))

    return registry


async def run(config_path: str = "config.yaml") -> None:
    config = Config.load(config_path)
    logger.info("Config loaded from %s", config_path)

    store = MessageStore()
    await store.initialize()

    registry = build_registry(config, store)
    sender = MessageSender(config.napcat.http_url, config.napcat.access_token)
    scheduler = PipelineScheduler(config=config, registry=registry, sender=sender, store=store)

    receiver = MessageReceiver(config.napcat.ws_url, config.napcat.access_token)
    receiver.on_message(scheduler.dispatch)

    await scheduler.startup()
    logger.info("Starting receiver on %s", config.napcat.ws_url)
    try:
        await receiver.run()
    finally:
        await scheduler.shutdown()


def main() -> None:
    setup_logging()
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    try:
        asyncio.run(run(config_path))
    except KeyboardInterrupt:
        logger.info("Shutting down")


if __name__ == "__main__":
    main()
