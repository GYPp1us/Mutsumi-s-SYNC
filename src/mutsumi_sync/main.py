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


def build_registry(config: Config) -> ToolRegistry:
    registry = ToolRegistry()

    registry.register(Tool(
        name="http_api_call",
        description="发�� HTTP 请求到任意 URL",
        parameters=HTTP_API_SCHEMA,
        handler=http_api_call,
    ))

    async def _config_manager(args: dict) -> str:
        return config_manager(args, config=config)

    registry.register(Tool(
        name="config_manager",
        description="读取、修改、热重载配置",
        parameters=CONFIG_MANAGER_SCHEMA,
        handler=_config_manager,
    ))

    return registry


async def run(config_path: str = "config.yaml") -> None:
    config = Config.load(config_path)
    logger.info("Config loaded from %s", config_path)

    store = MessageStore()
    await store.initialize()

    registry = build_registry(config)
    sender = MessageSender(config.napcat.http_url, config.napcat.access_token)
    scheduler = PipelineScheduler(config=config, registry=registry, sender=sender, store=store)

    receiver = MessageReceiver(config.napcat.ws_url, config.napcat.access_token)
    receiver.on_message(scheduler.dispatch)

    logger.info("Starting receiver on %s", config.napcat.ws_url)
    await receiver.run()


def main() -> None:
    setup_logging()
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    try:
        asyncio.run(run(config_path))
    except KeyboardInterrupt:
        logger.info("Shutting down")


if __name__ == "__main__":
    main()
