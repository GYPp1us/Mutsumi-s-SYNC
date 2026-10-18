"""Run a local-only prototype, with a deterministic offline demo by default."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
import sqlite3
import sys
import uuid

from prompt_toolkit import PromptSession

from ..config import Config
from ..logging import start_stream_log_store, stop_stream_log_store
from ..message.sender import Peer
from ..pipeline import LLMResult
from .adapters import ConsoleSender, LiveModel, build_worker_registry, load_prototype_prompts
from .runtime import PrototypeScheduler, RuntimeSettings
from .storage import PrototypeStore


def _reply(*messages: str) -> LLMResult:
    body = json.dumps({"messages": list(messages)}, ensure_ascii=False)
    return LLMResult(content=f"[TO_SELF][/TO_SELF][TO_USER]{body}[/TO_USER]")


class DemoModel:
    """Scripted behavior to inspect scheduling, never a model-quality simulation."""

    def __init__(self, *, hold_worker: bool = False) -> None:
        self.worker_started = asyncio.Event()
        self.worker_release = asyncio.Event()
        self.hold_worker = hold_worker
        self.slow_calls = 0

    async def __call__(self, messages: list[dict], tools: list[dict], *, lane: str) -> LLMResult:
        if lane == "worker":
            if not any(message.get("role") == "tool" for message in messages):
                return LLMResult(content="后台内部工具说明，不应发送。", tool_calls=[{
                    "id": "demo-read", "name": "read_file", "arguments": {"path": "README.md"},
                }])
            self.worker_started.set()
            if self.hold_worker:
                await self.worker_release.wait()
            else:
                await asyncio.sleep(2)
            result = next(message["content"] for message in reversed(messages) if message.get("role") == "tool")
            if str(result).startswith("[Error:"):
                return LLMResult(content="未能读取 README.md，无法完成这次资料检查。")
            return LLMResult(content="已读取 README.md。它介绍了基于 NapCat 的异步聊天机器人；这个演示仅完成文件读取，没有修改文件或发送 QQ。依据：README.md。")

        if messages and messages[-1].get("role") == "tool":
            result = str(messages[-1].get("content", ""))
            if result.startswith("[Error:"):
                return _reply("这次没能办成，我还不能确认。")
            previous = next((message for message in reversed(messages) if message.get("tool_calls")), {})
            function = previous.get("tool_calls", [{}])[0].get("function", {})
            arguments = function.get("arguments", "{}")
            arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
            action = arguments.get("action", "start")
            if action == "cancel":
                return _reply("好，这件事先停下。")
            if action == "update":
                return _reply("记下了，按这个要求继续。")
            if action == "status":
                return _reply("任务状态已经查到了。", result[:450])
            return _reply("嗯，我仔细看看。")

        current = str(messages[-1].get("content", "")) if messages else ""
        if "/slow" in current:
            self.slow_calls += 1
            if self.slow_calls == 1:
                await asyncio.Event().wait()
            return _reply("接上了，刚才让你等了一下。")
        if "/silent" in current:
            return _reply()
        if "/multi" in current:
            return _reply("先说第一件事。", "再补充第二件事。正文中的 a | b 保持原样。")
        actions = {"/work": "start", "/update": "update", "/cancel": "cancel", "/status": "status"}
        for command, action in actions.items():
            if command in current and tools:
                request = "阅读 README.md，简述项目用途并注明依据。" if action == "start" else current.split(command, 1)[-1].strip()
                arguments: dict = {"action": action}
                if request and action in {"start", "update"}:
                    arguments["request"] = request
                return LLMResult(content="解释一次任务管理调用，不应发送。", tool_calls=[{
                    "id": "demo-task", "name": "background_task", "arguments": arguments,
                }])
        if "后台" in current and ("报告" in current or "结果" in current):
            if "未能读取" in current or '"status": "failed"' in current:
                return _reply("这次没有读到项目说明，我还不能确认。")
            return _reply("看完了，README 里写的是一个异步 QQ 聊天机器人。", "这次只读了项目说明，没修改文件。")
        return _reply("我在听。那件事处理着，我们也可以接着聊。")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="聊天与后台工作原型：默认离线演示，所有回复只输出到本地终端。")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--demo", action="store_true", help="运行可重复的离线行为演示，默认模式")
    mode.add_argument("--interactive", action="store_true", help="本地交互聊天；默认使用脚本假模型")
    parser.add_argument("--live-model", action="store_true", help="显式使用配置的真实付费模型；仍只在终端显示，不连接 QQ")
    parser.add_argument("--config", default="config.example.yaml", help="配置文件，需选中含 prototype_* 字段的提示词文件")
    parser.add_argument("--db", default="data/prototype/prototype.sqlite3", help="原型专用 SQLite 数据库")
    parser.add_argument("--root", default=".", help="后台只读文件工具允许访问的项目目录")
    parser.add_argument("--wait-seconds", type=float, default=3.0, help="首次等待提示阈值，默认 3 秒")
    parser.add_argument("--chat-timeout", type=float, default=8.0, help="一次聊天处理的总时限，默认 8 秒")
    return parser


def validate_database(database: Path, root: Path, mode: str) -> None:
    """Refuse production databases and silent changes to restored job inputs."""
    if not database.exists() or database.stat().st_size == 0:
        return
    expected = {"root": os.path.normcase(str(root.resolve())), "model_mode": mode}
    try:
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            tables = {name for name in tables if not name.startswith("sqlite_")}
            if tables and "prototype_jobs" not in tables:
                raise ValueError("--db points to an existing non-prototype database; select a new prototype database")
            if "prototype_cli_settings" in tables:
                actual = dict(connection.execute("SELECT key, value FROM prototype_cli_settings"))
                if any(actual.get(key) != value for key, value in expected.items()):
                    raise ValueError("--db was created with a different --root or model mode; use its original settings or a new database")
            elif "prototype_jobs" in tables and connection.execute("SELECT 1 FROM prototype_jobs LIMIT 1").fetchone():
                raise ValueError("--db contains older tasks without saved root/model settings; use a new database")
    except sqlite3.DatabaseError:
        raise ValueError("--db is not a readable prototype SQLite database") from None


def bind_database(database: Path, root: Path, mode: str) -> None:
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS prototype_cli_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.executemany("INSERT OR IGNORE INTO prototype_cli_settings(key, value) VALUES (?, ?)", [
            ("root", os.path.normcase(str(root.resolve()))), ("model_mode", mode),
        ])


async def run_demo(scheduler: PrototypeScheduler, model: DemoModel, sender: ConsoleSender) -> None:
    peer = Peer(chat_type=1, peer_uid=f"demo-{uuid.uuid4().hex[:8]}")
    conversation = f"private:{peer.peer_uid}"

    async def submit(text: str) -> None:
        print(f"\n用户：{text}", flush=True)
        await scheduler.submit(text, conversation_id=conversation, actor_id="local:demo", peer=peer, actor_name="演示用户")
        await scheduler.wait_chat_idle(conversation)

    print("离线脚本演示：检查程序行为，不代表真实模型速度或角色表现。", flush=True)
    await submit("/work 帮我看看项目说明。")
    await asyncio.wait_for(model.worker_started.wait(), timeout=5)
    await submit("你今天怎么样？")
    assert not model.worker_release.is_set(), "background job should still be waiting"
    before_silence = len(sender.sent)
    await submit("/silent 这一条不用回复。")
    assert len(sender.sent) == before_silence, "a valid empty list must not send"
    print("验证：后台等待期间闲聊能回复；空消息列表没有发送。", flush=True)
    before_multi = len(sender.sent)
    await submit("/multi 分成两条说。")
    assert len(sender.sent) == before_multi + 2
    assert "a | b" in sender.sent[-1]["message"]
    before_slow = len(sender.sent)
    await submit("/slow 模拟聊天模型卡住。")
    assert len(sender.sent) == before_slow + 2
    assert sender.sent[before_slow]["message"] == scheduler.prompts.wait
    assert sender.sent[before_slow + 1]["message"] == "接上了，刚才让你等了一下。"
    print("验证：独立计时触发了等待发送；工具调用轮文字没有发送。", flush=True)
    model.worker_release.set()
    await asyncio.wait_for(scheduler.wait_idle(), timeout=10)
    assert not any("不应发送" in str(item["message"]) for item in sender.sent)
    print("\n演示完成：后台报告已回到聊天过程，所有可见消息只在终端显示。", flush=True)


async def run_interactive(scheduler: PrototypeScheduler) -> None:
    peer = Peer(chat_type=1, peer_uid="local-interactive")
    print("本地交互。/quit 退出；/silent、/multi、/slow、/work、/update、/status、/cancel 可演示假模型行为。")
    session: PromptSession[str] = PromptSession()
    while True:
        try:
            text = await session.prompt_async("你：")
        except (EOFError, KeyboardInterrupt):
            break
        if text.strip() == "/quit":
            break
        if text.strip():
            await scheduler.submit(text, conversation_id="private:local-interactive", actor_id="local:owner", peer=peer, actor_name="本地用户")


async def run(args: argparse.Namespace) -> None:
    config_path = Path(args.config)
    if not config_path.is_file():
        raise ValueError(f"Configuration does not exist: {config_path}")
    config = Config.load(str(config_path))
    prompts = load_prototype_prompts(config)
    database = Path(args.db).resolve()
    selected_root = Path(args.root).resolve()
    mode = "live" if args.live_model else "offline"
    await asyncio.to_thread(validate_database, database, selected_root, mode)
    worker_tools = build_worker_registry(selected_root)
    model = LiveModel(config) if args.live_model else DemoModel(hold_worker=not args.interactive)
    sender = ConsoleSender()
    settings = RuntimeSettings(wait_seconds=args.wait_seconds, chat_timeout=args.chat_timeout)
    log_config = config.model_copy(deep=True)
    log_config.logging.stream_store.path = str(database.parent / "logs" / "prototype.ndjson")
    log_config.logging.text_file.path = str(database.parent / "logs" / "prototype.log")
    root_logger = logging.getLogger("mutsumi")
    root_logger.setLevel(logging.INFO)
    handler = start_stream_log_store(log_config)
    if handler is not None:
        root_logger.addHandler(handler)
    store = PrototypeStore(str(database), media_dir=str(database.parent / "media"))
    scheduler = PrototypeScheduler(
        store, model, sender, worker_tools=worker_tools, prompts=prompts, settings=settings,
        bot_id=config.identity.bot_user_id or "local:bot", bot_name=config.identity.bot_nickname or "Mutsumi",
    )
    try:
        await store.initialize()
        await asyncio.to_thread(bind_database, database, selected_root, mode)
        await scheduler.start()
        print(f"原型数据库：{database}\n模式：{'真实模型，本地终端' if args.live_model else '离线假模型，本地终端'}", flush=True)
        if args.interactive or args.live_model:
            await run_interactive(scheduler)
        else:
            assert isinstance(model, DemoModel)
            await run_demo(scheduler, model, sender)
    finally:
        await scheduler.close()
        await store.close()
        if handler is not None:
            root_logger.removeHandler(handler)
        stop_stream_log_store()


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    parser = build_parser()
    args = parser.parse_args()
    if args.demo and args.live_model:
        parser.error("--demo is offline; use --live-model for interactive model calls")
    if args.wait_seconds <= 0 or args.chat_timeout <= args.wait_seconds:
        parser.error("require 0 < --wait-seconds < --chat-timeout")
    try:
        asyncio.run(run(args))
    except (ValueError, OSError) as exc:
        print(f"原型启动失败：{exc}", file=sys.stderr)
        raise SystemExit(2) from None
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
