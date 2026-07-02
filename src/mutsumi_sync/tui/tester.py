from __future__ import annotations

import asyncio
import logging
import sys
import threading

from ..config import Config
from ..memory.store import MessageStore
from ..message.classifier import MessageType
from ..message.receiver import MessageEvent
from ..message.sender import MessageSender
from ..scheduler import PipelineScheduler
from ..tools.registry import Tool, ToolRegistry
from ..tools.http_api import http_api_call, HTTP_API_SCHEMA
from ..tools.config_manager import config_manager, CONFIG_MANAGER_SCHEMA
from ..tools.memory import memory_search, memory_save, MEMORY_SEARCH_SCHEMA, MEMORY_SAVE_SCHEMA
from ..tools.self_note import self_note_tool, SELF_NOTE_SCHEMA
from ..tools.send import send_tool, SEND_TOOL_SCHEMA

logger = logging.getLogger("mutsumi.tester")

_BOLD = "\033[1m"
_DIM = "\033[2m"
_RED = "\033[31m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_CYAN = "\033[36m"
_RESET = "\033[0m"


class _QueueHandler(logging.Handler):
    def __init__(self, queue: asyncio.Queue[logging.LogRecord]):
        super().__init__()
        self.queue = queue
        self.setFormatter(logging.Formatter(
            fmt="%(asctime)s [%(levelname)-5s] %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        ))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.format(record)
            self.queue.put_nowait(record)
        except asyncio.QueueFull:
            pass


def _level_color(record: logging.LogRecord) -> str:
    if record.levelno >= logging.ERROR:
        return _RED
    if record.levelno >= logging.WARNING:
        return _YELLOW
    if record.levelno >= logging.INFO:
        return _RESET
    return _DIM


def _format_log(record: logging.LogRecord) -> str:
    color = _level_color(record)
    msg = _DIM + record.name + _RESET + " " + color + record.getMessage() + _RESET
    return f"{color}[{record.asctime}]{_RESET} {msg}"


def setup_test_logging(queue: asyncio.Queue[logging.LogRecord]) -> None:
    root = logging.getLogger("mutsumi")
    root.setLevel(logging.DEBUG)
    root.handlers.clear()
    root.addHandler(_QueueHandler(queue))

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)


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
        return await send_tool(args, sender=deps.get("sender"), peer=deps.get("peer"), config=config)

    registry.register(Tool(
        name="send",
        description="发送消息到用户。支持 text/image/markdown_image/face/at/reply/forward 段类型。",
        parameters=SEND_TOOL_SCHEMA,
        handler=_send,
    ))

    return registry


def _fake_event(user_id: int, group_id: int | None, text: str) -> MessageEvent:
    msg_type = "group" if group_id else "private"
    return MessageEvent(
        post_type="message",
        message_type=msg_type,
        user_id=user_id,
        group_id=group_id,
        message=[{"type": "text", "data": {"text": text}}],
        raw_message=text,
        message_id=0,
        sender={"user_id": user_id, "nickname": "test"},
        time=int(asyncio.get_event_loop().time()),
        self_id=0,
    )


def _inject_help() -> str:
    return (
        f"{_CYAN}用法:{_RESET} /inject [private <user_id> | group <group_id> <user_id>] <message>\n"
        f"  示例: {_DIM}/inject private 123456 你好世界{_RESET}\n"
        f"         {_DIM}/inject group 789000 123456 群聊测试{_RESET}"
    )


def _cmd_help() -> str:
    return f"""{_CYAN}命令:{_RESET}
  {_BOLD}/inject{_RESET} private <user_id> <msg>      注入私聊消息
  {_BOLD}/inject{_RESET} group <group_id> <user_id> <msg>  注入群消息
  {_BOLD}/break{_RESET} <user_id>                    取消该用户的 pipeline
  {_BOLD}/break{_RESET} private <user_id>            取消私聊 pipeline
  {_BOLD}/break{_RESET} group <group_id> <user_id>   取消群聊 pipeline
  {_BOLD}/list{_RESET}                               列出活跃 task
  {_BOLD}/status{_RESET}                             显示 scheduler 状态
  {_BOLD}/connect{_RESET}                            连接 NapCat WebSocket
  {_BOLD}/quit{_RESET}                               退出"""


class _FakeSender:
    async def send(self, peer, message) -> dict:
        segments = _to_segments(message)
        label = "private" if peer.chat_type == 1 else "group"
        lines = ["", f"=========[SEND][{label}][{peer.peer_uid}]========="]
        for seg in segments:
            lines.append(f"  {_DIM}{_render_segment(seg)}{_RESET}")
        lines.append(f"=========[{len(segments)} segment(s)]=========")
        logger.info("\n".join(lines))
        return {"status": "ok"}

    async def send_poke(self, peer) -> dict:
        label = "private" if peer.chat_type == 1 else "group"
        logger.info(f"[POKE] {label}:{peer.peer_uid}")
        return {"status": "ok"}


def _render_segment(seg: dict) -> str:
    seg_type = seg.get("type", "unknown")
    data = seg.get("data", {})
    if seg_type == "text":
        return f"[text] {data.get('text', '')}"
    if seg_type == "image":
        return f"[image] file={data.get('file','')}"
    if seg_type == "face":
        return f"[face] id={data.get('id','')}"
    if seg_type == "at":
        return f"[at] @{data.get('qq','')}"
    if seg_type == "reply":
        return f"[reply] id={data.get('id','')}"
    if seg_type in ("record", "video"):
        return f"[{seg_type}] file={data.get('file','')}"
    if seg_type == "forward":
        return f"[forward] id={data.get('id','')}"
    return f"[{seg_type}] {data}" if data else f"[{seg_type}]"


def _to_segments(message: str | list) -> list[dict]:
    if isinstance(message, str):
        return [{"type": "text", "data": {"text": message}}]
    return message


async def run_tester(config_path: str = "config.yaml") -> None:
    config = Config.load(config_path)
    store = MessageStore()
    await store.initialize()
    registry = build_registry(config, store)
    sender = _FakeSender()
    scheduler = PipelineScheduler(config=config, registry=registry, sender=sender, store=store)

    log_queue: asyncio.Queue[logging.LogRecord] = asyncio.Queue(maxsize=500)
    setup_test_logging(log_queue)
    logger.info("测试器启动 - 配置已加载")
    logger.info("使用 FakeSender，输入 /connect 连接真实 NapCat")

    m = config.model
    if m.api_key:
        logger.info("API 状态: %s @ %s model=%s temp=%s", m.provider, m.base_url, m.model, m.temperature)
    else:
        logger.info("API 状态: 未配置 — pipeline 将使用本地 stub")

    receiver = None

    print(f"{_GREEN}{_BOLD}Mutsumi's SYNC - 交互式测试器{_RESET}")
    print(f"{_DIM}输入 /help 查看命令{_RESET}")
    print()

    cmd_queue: asyncio.Queue[str] = asyncio.Queue()
    running = True

    def stdin_reader() -> None:
        nonlocal running
        while running:
            try:
                line = sys.stdin.readline()
                if not line:
                    break
                line = line.strip()
                if line:
                    try:
                        cmd_queue.put_nowait(line)
                    except asyncio.QueueFull:
                        pass
            except (EOFError, ValueError, OSError):
                break

    stdin_thread = threading.Thread(target=stdin_reader, daemon=True)
    stdin_thread.start()
    print(f"{_DIM}> {_RESET}", end="", flush=True)

    async def print_logs() -> None:
        while True:
            try:
                record = await log_queue.get()
                print(f"\r{_format_log(record)}")
                print(f"{_DIM}> {_RESET}", end="", flush=True)
            except Exception:
                try:
                    print(f"\r{_RED}print_logs crashed{_RESET}", file=sys.__stdout__)
                finally:
                    pass

    log_task = asyncio.create_task(print_logs())

    try:
        while running:
            try:
                line = await asyncio.wait_for(cmd_queue.get(), timeout=0.1)
            except asyncio.TimeoutError:
                continue

            parts = line.split(maxsplit=3)
            cmd = parts[0].lower() if parts else ""

            if cmd == "/quit":
                running = False

            elif cmd == "/help":
                print(f"\r{_cmd_help()}")

            elif cmd == "/list":
                keys = scheduler.active_keys()
                print(f"\r{_CYAN}活跃 tasks ({len(keys)}):{_RESET}")
                for k in keys:
                    print(f"  {k}")

            elif cmd == "/status":
                st = scheduler.status()
                print(f"\r{_CYAN}Scheduler 状态:{_RESET}")
                for k, v in st.items():
                    print(f"  {k}: {v}")

            elif cmd == "/break":
                if len(parts) < 2:
                    print(f"\r{_RED}用法:{_RESET} /break private <user_id>  或  /break group <group_id> <user_id>")
                elif parts[1] == "private" and len(parts) >= 3:
                    key = f"private:{parts[2]}"
                    print(f"\r{_YELLOW}取消: {key}{_RESET}")
                    await scheduler.cancel_user(key)
                elif parts[1] == "group" and len(parts) >= 4:
                    if len(parts) < 4:
                        print(f"\r{_RED}用法:{_RESET} /break group <group_id> <user_id>")
                    else:
                        key = f"group:{parts[2]}:{parts[3]}"
                        print(f"\r{_YELLOW}取消: {key}{_RESET}")
                        await scheduler.cancel_user(key)
                else:
                    key = f"private:{parts[1]}"
                    print(f"\r{_YELLOW}取消: {key}{_RESET}")
                    await scheduler.cancel_user(key)

            elif cmd == "/inject":
                if len(parts) < 2:
                    print(f"\r{_inject_help()}")
                elif parts[1] == "private" and len(parts) >= 4:
                    uid = int(parts[2])
                    msg = parts[3] if len(parts) > 3 else " ".join(parts[3:]) if len(parts) > 3 else ""
                    event = _fake_event(uid, None, parts[3])
                    print(f"\r{_CYAN}注入私聊: user={uid} msg={parts[3][:30]}{_RESET}")
                    await scheduler.dispatch(event)
                elif parts[1] == "group" and len(parts) >= 5:
                    gid = int(parts[2])
                    uid = int(parts[3])
                    msg = parts[4] if len(parts) > 4 else " "
                    event = _fake_event(uid, gid, parts[4])
                    print(f"\r{_CYAN}注入群消息: group={gid} user={uid} msg={parts[4][:30]}{_RESET}")
                    await scheduler.dispatch(event)
                else:
                    print(f"\r{_inject_help()}")

            elif cmd == "/connect":
                if receiver is not None:
                    print(f"\r{_YELLOW}已连接{_RESET}")
                else:
                    real_sender = MessageSender(config.napcat.http_url, config.napcat.access_token)
                    scheduler.sender = real_sender
                    from ..message.receiver import MessageReceiver
                    receiver = MessageReceiver(config.napcat.ws_url, config.napcat.access_token)
                    receiver.on_message(scheduler.dispatch)
                    asyncio.create_task(receiver.run())
                    logger.info("切换到真实 NapCat 连接: %s", config.napcat.ws_url)
                    print(f"\r{_GREEN}正在连接 NapCat: {config.napcat.ws_url}{_RESET}")

            else:
                print(f"\r{_RED}未知命令: {cmd}{_RESET}  输入 /help 查看帮助")

            print(f"{_DIM}> {_RESET}", end="", flush=True)

    finally:
        running = False
        log_task.cancel()
        try:
            await log_task
        except asyncio.CancelledError:
            pass
        if receiver:
            await receiver.close()
        logger.info("测试器退出")


def main() -> None:
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    try:
        asyncio.run(run_tester(config_path))
    except KeyboardInterrupt:
        print(f"\n{_YELLOW}中断{_RESET}")


if __name__ == "__main__":
    main()
