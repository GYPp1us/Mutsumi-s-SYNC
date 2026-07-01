from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ..config import Config
from ..lab.core import default_lab_prompt, load_lab_config
from ..memory.store import MessageStore
from ..memory.session import SessionState
from ..memory.window import MessageWindow
from ..message.classifier import MessageType
from ..message.sender import Peer
from ..pipeline import pipeline
from ..scheduler import PipelineDeps, PipelineScheduler
from ..tools.config_manager import CONFIG_MANAGER_SCHEMA, config_manager
from ..tools.http_api import HTTP_API_SCHEMA, http_api_call
from ..tools.memory import MEMORY_SAVE_SCHEMA, MEMORY_SEARCH_SCHEMA, memory_save, memory_search
from ..tools.registry import Tool, ToolRegistry
from ..tools.send import SEND_TOOL_SCHEMA, send_tool
from ..tools.self_note import SELF_NOTE_SCHEMA, self_note_tool

logger = logging.getLogger("mutsumi.lab")


@dataclass
class PromptLabState:
    config: Config
    store: MessageStore
    registry: ToolRegistry
    sender: Any
    scheduler: PipelineScheduler
    window: MessageWindow
    session: SessionState
    peer: Peer
    group_key: str


def build_lab_registry(config: Config, store: MessageStore) -> ToolRegistry:
    registry = ToolRegistry()

    async def _send(args: dict, **deps) -> str:
        return await send_tool(args, sender=deps.get("sender"), peer=deps.get("peer"))

    registry.register(Tool(
        name="send",
        description="发送消息到用户。支持 text/image/face/at/reply/forward 段类型。",
        parameters=SEND_TOOL_SCHEMA,
        handler=_send,
    ))

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

    return registry


async def create_prompt_lab(config: Config) -> PromptLabState:
    store = MessageStore()
    await store.initialize()
    registry = build_lab_registry(config, store)
    from .tester import _FakeSender
    sender = _FakeSender()
    scheduler = PipelineScheduler(config=config, registry=registry, sender=sender, store=store)
    startup = getattr(scheduler, "startup", None)
    if startup is not None:
        await startup()
    window = MessageWindow()
    session = SessionState()
    peer = Peer(chat_type=1, peer_uid="lab")
    return PromptLabState(
        config=config,
        store=store,
        registry=registry,
        sender=sender,
        scheduler=scheduler,
        window=window,
        session=session,
        peer=peer,
        group_key="private:lab",
    )


async def run_prompt_message(state: PromptLabState, text: str) -> None:
    deps = PipelineDeps(
        config=state.config,
        registry=state.registry,
        sender=state.sender,
        store=state.store,
        window=state.window,
        session=state.session,
        peer=state.peer,
        group_key=state.group_key,
        content_bridge=True,
    )
    await pipeline(text, MessageType.SHORT_TEXT, None, None, deps=deps)


def load_prompt_override(config: Config, path: str | None = None) -> str:
    if path is None:
        path = "lab_prompt.yaml"
    override = Path(path)
    if not override.exists():
        config.system_prompt = default_lab_prompt()
        return config.system_prompt
    data = yaml.safe_load(override.read_text(encoding="utf-8")) or {}
    prompt = str(data.get("system_prompt", "")).strip()
    config.system_prompt = prompt or default_lab_prompt()
    return config.system_prompt


async def run_lab(config_path: str = "config.yaml", prompt_path: str | None = None) -> None:
    config = load_lab_config(config_path)
    load_prompt_override(config, prompt_path)
    state = await create_prompt_lab(config)
    try:
        while True:
            line = await asyncio.to_thread(input, "lab> ")
            if not line.strip():
                continue
            if line.strip() in ("/quit", "/exit"):
                break
            if line.startswith("/prompt "):
                load_prompt_override(config, line.split(maxsplit=1)[1].strip())
                print("[OK] prompt reloaded")
                continue
            await run_prompt_message(state, line)
    finally:
        shutdown = getattr(state.scheduler, "shutdown", None)
        if shutdown is not None:
            await shutdown()
        await state.store.close()


def main() -> None:
    if len(sys.argv) < 2:
        print("用法: python -m src.mutsumi_sync.tui.lab [config.yaml] [prompt.yaml]")
        sys.exit(1)
    config_path = sys.argv[1]
    prompt_path = sys.argv[2] if len(sys.argv) > 2 else None
    try:
        asyncio.run(run_lab(config_path=config_path, prompt_path=prompt_path))
    except KeyboardInterrupt:
        print("\n中断")


if __name__ == "__main__":
    main()
