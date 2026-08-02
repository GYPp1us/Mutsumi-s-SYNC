from __future__ import annotations

import asyncio
import logging
import sys

from .config import Config
from .ingress import ServiceIngress
from .logging import start_stream_log_store, stop_stream_log_store
from .memory.store import MessageStore
from .message.receiver import MessageReceiver
from .message.sender import MessageSender
from .scheduler import PipelineScheduler
from .tools.registry import Tool, ToolRegistry
from .tools.http_api import http_api_call, HTTP_API_SCHEMA
from .tools.config_manager import config_manager, CONFIG_MANAGER_SCHEMA
from .tools.memory import (
    memory_search,
    memory_save,
    MEMORY_SEARCH_DESCRIPTION,
    MEMORY_SEARCH_SCHEMA,
    MEMORY_SAVE_DESCRIPTION,
    MEMORY_SAVE_SCHEMA,
)
from .tools.self_note import self_note_tool, SELF_NOTE_DESCRIPTION, SELF_NOTE_SCHEMA
from .tools.actor_profile import actor_profile_tool, ACTOR_PROFILE_SCHEMA
from .tools.send import send_tool, SEND_TOOL_SCHEMA
from .tools.no_reply import no_reply_tool, NO_REPLY_SCHEMA
from .tools.status_update import status_update_tool, STATUS_UPDATE_SCHEMA
from .tools.scheduler import scheduler_tool, SCHEDULER_SCHEMA
from .tools.media import media_search, MEDIA_SEARCH_SCHEMA, sticker_manage, STICKER_MANAGE_SCHEMA
from .tools.file_edit import file_edit_tool, FILE_EDIT_SCHEMA
from .tools.delegate import delegate_task_tool, DELEGATE_TASK_SCHEMA

logger = logging.getLogger("mutsumi.main")


def setup_logging(level: int = logging.INFO, config: Config | None = None) -> None:
    stop_stream_log_store()
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
    if config is not None:
        stream_handler = start_stream_log_store(config)
        if stream_handler is not None:
            root.addHandler(stream_handler)

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)


def build_registry(config: Config, store: MessageStore) -> ToolRegistry:
    registry = ToolRegistry()

    if config.file_tools.enabled:
        async def _file_edit(args: dict, **deps) -> str:
            return await file_edit_tool(args, config=config)

        registry.register(Tool(
            name="file_edit",
            description=(
                "在受控的 skills 文件根目录中列出、读取、完整写入或精确替换文本。"
                "只能操作工具结果中显示的相对路径；不能执行命令、访问配置或数据库。"
            ),
            parameters=FILE_EDIT_SCHEMA,
            handler=_file_edit,
        ))

    registry.register(Tool(
        name="http_api_call",
        description="发送 HTTP 请求到任意 URL",
        parameters=HTTP_API_SCHEMA,
        handler=http_api_call,
        latency_class="long",
        status_hint="我先请求一下外部接口，可能需要一点时间。",
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
        description=MEMORY_SEARCH_DESCRIPTION,
        parameters=MEMORY_SEARCH_SCHEMA,
        handler=_memory_search,
    ))

    async def _memory_save(args: dict, **deps) -> str:
        return await memory_save(args, store=store, group_key=deps.get("group_key", ""))

    registry.register(Tool(
        name="memory_save",
        description=MEMORY_SAVE_DESCRIPTION,
        parameters=MEMORY_SAVE_SCHEMA,
        handler=_memory_save,
    ))

    async def _media_search(args: dict, **deps) -> str:
        deps.pop("store", None)
        return await media_search(args, store=store, **deps)

    registry.register(Tool(
        name="sticker_search",
        description=(
            "搜索全局 Media Ledger。query 省略时列出所有可复用媒体；需要复用结果时，"
            "将返回的 media_id 传给 send(media_id=...)，不要猜测文件路径。"
        ),
        parameters=MEDIA_SEARCH_SCHEMA,
        handler=_media_search,
    ))
    registry.register(Tool(
        name="media_search",
        description=(
            "搜索全局 Media Ledger。query 省略时列出所有可复用媒体；需要复用结果时，"
            "将返回的 media_id 传给 send(media_id=...)，不要猜测文件路径。"
        ),
        parameters=MEDIA_SEARCH_SCHEMA,
        handler=_media_search,
    ))

    async def _sticker_manage(args: dict, **deps) -> str:
        deps.pop("store", None)
        return await sticker_manage(args, store=store, **deps)

    registry.register(Tool(
        name="sticker_manage",
        description="Describe, archive, or restore a sticker in the global media ledger.",
        parameters=STICKER_MANAGE_SCHEMA,
        handler=_sticker_manage,
    ))

    async def _self_note(args: dict, **deps) -> str:
        return await self_note_tool(args, store=store, group_key=deps.get("group_key", ""))

    registry.register(Tool(
        name="self_note",
        description=SELF_NOTE_DESCRIPTION,
        parameters=SELF_NOTE_SCHEMA,
        handler=_self_note,
    ))

    async def _actor_profile(args: dict, **deps) -> str:
        return await actor_profile_tool(args, store=store, **deps)

    registry.register(Tool(
        name="actor_profile",
        description=(
            "维护全局参与者档案。可读取、列出或维护稳定 actor_id 对应的私下称呼和关系标签。"
            "不要猜测 actor_id，只使用上下文或工具结果中的稳定 ID。"
        ),
        parameters=ACTOR_PROFILE_SCHEMA,
        handler=_actor_profile,
    ))

    async def _send(args: dict, **deps) -> str:
        return await send_tool(
            args,
            sender=deps.get("sender"),
            peer=deps.get("peer"),
            config=config,
            store=store,
        )

    registry.register(Tool(
        name="send",
        description=(
            "特殊发送工具。普通文字回复请写入最终 assistant content 的 TO_USER 区块；仅在需要发送图片、"
            "media_id、markdown_image、QQ 表情、@、reply 或 forward 等特殊消息段时使用。"
            "跨会话互动可以使用 target+text 向不同目标发送文字；当前会话的普通文字仍必须使用 TO_USER。"
            "target 必填，只能使用 private:<numeric_id> 或 group:<numeric_id>，不要使用昵称。"
        ),
        parameters=SEND_TOOL_SCHEMA,
        handler=_send,
    ))

    async def _status_update(args: dict, **deps) -> str:
        return await status_update_tool(
            args,
            sender=deps.get("sender"),
            peer=deps.get("peer"),
        )

    registry.register(Tool(
        name="status_update",
        description=(
            "在预计需要等待的工具调用前，向用户发送一条简短的进度通知。"
            "只说明准备做什么，不要写详细思考过程；它不会结束 pipeline，也不是最终回复。"
        ),
        parameters=STATUS_UPDATE_SCHEMA,
        handler=_status_update,
        latency_class="fast",
    ))

    registry.register(Tool(
        name="no_reply",
        description=(
            "当本轮不应该向用户发送任何消息时调用。调用后 pipeline 会结束本轮并保持静默。"
            "只用于确实没有可见内容的静默维护或自主检查；普通问题、未知事实、拒答、隐私边界说明和工具完成结果都必须写入 TO_USER，不能用 no_reply 逃避回答。"
        ),
        parameters=NO_REPLY_SCHEMA,
        handler=no_reply_tool,
    ))

    return registry


def register_scheduler_tool(registry: ToolRegistry, scheduler: PipelineScheduler) -> None:
    async def _scheduler_tool(args: dict, **deps) -> str:
        return await scheduler_tool(
            args,
            scheduler=scheduler,
            group_key=deps.get("group_key", ""),
            conversation_id=deps.get("conversation_id", ""),
            peer=deps.get("peer"),
        )

    registry.register(Tool(
        name="scheduler",
        description=(
            "Manage durable one-shot, fixed-interval, or five-field cron wake-ups. The task is bound to the current chat; "
            "store a natural-language prompt, an optional opaque natural-language context, and a time rule. "
            "The scheduler does not interpret context as workflow data. Use operation=list/pause/resume/cancel for lifecycle changes. "
            "A due task waits behind an active user pipeline and does not cancel it."
        ),
        parameters=SCHEDULER_SCHEMA,
        handler=_scheduler_tool,
    ))

    if getattr(scheduler, "_opencode_runner", None) is not None:
        async def _delegate_task(args: dict, **deps) -> str:
            return await delegate_task_tool(
                args,
                scheduler=scheduler,
                conversation_id=deps.get("conversation_id", ""),
                group_key=deps.get("group_key", ""),
            )

        registry.register(Tool(
            name="delegate_task",
            description=(
                "将一个需要较长执行的自然语言任务异步交给 OpenCode。"
                "可以在 prompt 中给出 Skill 文件路径和交付要求；任务完成时必须调用 report_task 回报。"
                "不要用它发送普通聊天文字，也不要猜测未回报的任务已经完成。"
            ),
            parameters=DELEGATE_TASK_SCHEMA,
            handler=_delegate_task,
            latency_class="long",
            status_hint="我先把这项工作交给外部任务 Agent 处理。",
        ))


async def run(config_path: str = "config.yaml") -> None:
    config = Config.load(config_path)
    config.validate_startup()
    setup_logging(config=config)
    logger.info("Config loaded from %s", config_path)

    store = MessageStore()
    await store.initialize()

    registry = build_registry(config, store)
    sender = MessageSender(config.napcat.http_url, config.napcat.access_token)
    scheduler = PipelineScheduler(config=config, registry=registry, sender=sender, store=store)
    register_scheduler_tool(registry, scheduler)
    ingress = ServiceIngress(config.ingress, store, scheduler.dispatch_service_event)

    receiver = MessageReceiver(config.napcat.ws_url, config.napcat.access_token)
    receiver.on_message(scheduler.dispatch)

    await scheduler.startup()
    logger.info("Starting receiver on %s", config.napcat.ws_url)
    try:
        await ingress.start()
        await receiver.run()
    finally:
        await ingress.close()
        await scheduler.shutdown()


def main() -> None:
    setup_logging()
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    try:
        asyncio.run(run(config_path))
    except KeyboardInterrupt:
        logger.info("Shutting down")
    finally:
        stop_stream_log_store()


if __name__ == "__main__":
    main()
