from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any

import yaml

from ..config import Config
from ..message.classifier import MessageType
from ..message.sender import Peer
from ..memory.window import MessageWindow
from ..memory.session import SessionState
from ..memory.store import MessageStore
from ..pipeline import pipeline
from ..scheduler import PipelineDeps
from ..tools.registry import Tool, ToolRegistry
from ..tools.http_api import http_api_call, HTTP_API_SCHEMA
from ..tools.config_manager import config_manager, CONFIG_MANAGER_SCHEMA
from ..tools.send import send_tool, SEND_TOOL_SCHEMA
from ..tools.memory import memory_search, memory_save, MEMORY_SEARCH_SCHEMA, MEMORY_SAVE_SCHEMA
from ..tools.self_note import self_note_tool, SELF_NOTE_SCHEMA

_DIM = "\033[2m"
_RESET = "\033[0m"
_BOLD = "\033[1m"
_CYAN = "\033[36m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_RED = "\033[31m"

logger = logging.getLogger("mutsumi.playground")


def setup_logging() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)-5s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger("mutsumi")
    root.setLevel(logging.INFO)
    root.handlers.clear()
    root.addHandler(handler)
    logging.getLogger("httpx").setLevel(logging.WARNING)


class CaptureSender:
    """记录 pipeline 发送的所有消息。"""

    def __init__(self):
        self.sent: list[dict[str, Any]] = []
        self.sends: list[str] = []

    async def send(self, peer, message) -> dict:
        preview = str(message)[:200]
        self.sent.append({"type": "send", "message": message, "peer": peer})
        self.sends.append(preview)
        return {"status": "ok"}

    async def send_poke(self, peer) -> dict:
        self.sent.append({"type": "poke", "peer": peer})
        return {"status": "ok"}


def build_registry(config: Config, store: MessageStore) -> ToolRegistry:
    registry = ToolRegistry()

    async def _send(args: dict, **deps) -> str:
        return await send_tool(args, sender=deps.get("sender"), peer=deps.get("peer"))

    registry.register(Tool(
        name="send",
        description="向用户发送消息（支持文本/图片/表情/ @ 人/引用回复/转发）",
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


def load_scenario(path: str) -> dict:
    path = Path(path)
    if not path.exists():
        alt = Path("scenarios") / path.name
        if alt.exists():
            path = alt
    if not path.exists():
        print(f"{_RED}场景文件不存在: {path}{_RESET}")
        sys.exit(1)
    with open(path, encoding="utf-8") as f:
        scenario = yaml.safe_load(f)
    if not scenario or "steps" not in scenario:
        print(f"{_RED}场景格式错误: 需要 steps 字段{_RESET}")
        sys.exit(1)
    return scenario


async def run_scenario(scenario_path: str) -> None:
    setup_logging()

    scenario = load_scenario(scenario_path)
    scenario_name = scenario.get("name", Path(scenario_path).stem)
    print(f"\n{_BOLD}{_CYAN}=========[SCENARIO] {scenario_name}========={_RESET}\n")

    config = Config.load("config.yaml")
    original_prompt = config.prompts.persona
    if scenario.get("prompt"):
        config.prompts.persona = scenario["prompt"]

    store = MessageStore()
    await store.initialize()

    registry = build_registry(config, store)
    sender = CaptureSender()
    window = MessageWindow()
    session = SessionState()
    peer = Peer(chat_type=1, peer_uid="playground")
    group_key = "private:playground"

    steps = scenario["steps"]
    for i, step in enumerate(steps):
        text = step.get("text", "")
        if not text:
            continue

        print(f"{_BOLD}{_YELLOW}--- Step {i+1}/{len(steps)}: {text[:60]} ---{_RESET}\n")

        deps = PipelineDeps(
            config=config,
            registry=registry,
            sender=sender,
            store=store,
            window=window,
            session=session,
            peer=peer,
            group_key=group_key,
        )

        try:
            await pipeline(text, MessageType.SHORT_TEXT, None, None, deps=deps)
        except Exception:
            logger.exception("Step %d failed", i + 1)

    config.prompts.persona = original_prompt

    print()
    print(f"{_BOLD}{_CYAN}=========[SCENARIO COMPLETE] {scenario_name}========={_RESET}")
    print(f"{_DIM}Steps:  {len(steps)}{_RESET}")
    print(f"{_DIM}Sends:  {len(sender.sends)}{_RESET}")
    for s in sender.sends:
        print(f"  {_DIM}→ {s[:120]}{_RESET}")

    await store.close()


def main() -> None:
    if len(sys.argv) < 2:
        print(f"用法: python -m src.mutsumi_sync.tui.playground <scenario.yaml>")
        sys.exit(1)
    try:
        asyncio.run(run_scenario(sys.argv[1]))
    except KeyboardInterrupt:
        print(f"\n{_YELLOW}中断{_RESET}")


if __name__ == "__main__":
    main()
