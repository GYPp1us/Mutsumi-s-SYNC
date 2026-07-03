from __future__ import annotations

import asyncio
import logging
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any

import httpx
from prompt_toolkit.application import Application
from prompt_toolkit.application.current import get_app
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.document import Document
from prompt_toolkit.history import InMemoryHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout, HSplit, VSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl, BufferControl
from prompt_toolkit.lexers import Lexer
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.mouse_events import MouseEventType
from prompt_toolkit.styles import Style

from ..config import Config
from ..memory.store import MessageStore
from ..message.classifier import MessageType
from ..message.receiver import MessageEvent, MessageReceiver
from ..message.sender import MessageSender
from ..scheduler import PipelineScheduler
from ..tools.registry import Tool, ToolRegistry
from ..tools.http_api import http_api_call, HTTP_API_SCHEMA
from ..tools.config_manager import config_manager, CONFIG_MANAGER_SCHEMA
from ..tools.memory import memory_search, memory_save, MEMORY_SEARCH_SCHEMA, MEMORY_SAVE_SCHEMA
from ..tools.self_note import self_note_tool, SELF_NOTE_SCHEMA
from ..tools.send import send_tool, SEND_TOOL_SCHEMA

from .tester import _FakeSender

logger = logging.getLogger("mutsumi.dashboard")

_ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub('', text)


_ANSI_STYLE_MAP = {
    1: "bold",
    2: "#888888",
    31: "ansired",
    32: "ansigreen",
    33: "ansiyellow",
    36: "ansicyan",
}


def _ansi_to_fragments(text: str) -> list[tuple[str, str]]:
    fragments: list[tuple[str, str]] = []
    style_parts: list[str] = []
    pos = 0

    for match in _ANSI_RE.finditer(text):
        if match.start() > pos:
            fragments.append((" ".join(style_parts), text[pos:match.start()]))

        codes = match.group()[2:-1]
        parsed_codes = [0] if not codes else [int(c) if c else 0 for c in codes.split(";")]
        for code in parsed_codes:
            if code == 0:
                style_parts.clear()
            elif code in _ANSI_STYLE_MAP:
                style = _ANSI_STYLE_MAP[code]
                if style not in style_parts:
                    style_parts.append(style)
        pos = match.end()

    if pos < len(text):
        fragments.append((" ".join(style_parts), text[pos:]))

    return [(style, part) for style, part in fragments if part]


def _ansi_fragments_to_text(fragments: list[tuple[str, str]]) -> str:
    return "".join(text for _, text in fragments)


def _split_fragments_by_line(fragments: list[tuple[str, str]]) -> list[list[tuple[str, str]]]:
    lines: list[list[tuple[str, str]]] = [[]]
    for style, text in fragments:
        parts = text.splitlines(keepends=True)
        if not parts:
            continue
        for part in parts:
            visible = part[:-1] if part.endswith("\n") else part
            if visible:
                lines[-1].append((style, visible))
            if part.endswith("\n"):
                lines.append([])
    if lines and not lines[-1]:
        lines.pop()
    return lines or [[("", "")]]


def _copy_text_to_system_clipboard(text: str) -> bool:
    if not text:
        return False

    try:
        if sys.platform == "win32":
            subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command", "$input | Set-Clipboard"],
                input=text,
                text=True,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True

        if sys.platform == "darwin" and shutil.which("pbcopy"):
            subprocess.run(
                ["pbcopy"],
                input=text,
                text=True,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True

        for command in ("wl-copy", "xclip", "xsel"):
            path = shutil.which(command)
            if not path:
                continue
            args = [path]
            if command == "xclip":
                args.extend(["-selection", "clipboard"])
            elif command == "xsel":
                args.append("--clipboard")
            subprocess.run(
                args,
                input=text,
                text=True,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True
    except (OSError, subprocess.CalledProcessError):
        return False

    return False


ESTIMATE_CHARS_PER_TOKEN = 4

_DIM = "\033[2m"
_RESET = "\033[0m"
_BOLD = "\033[1m"
_RED = "\033[31m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_CYAN = "\033[36m"

DASHBOARD_STYLE = Style.from_dict({
    "bar-title": "bold",
    "bar-dim": "#888888",
    "bar-ok": "ansigreen",
    "bar-warn": "ansiyellow",
    "bar-err": "ansired",
    "bar-instance": "bold ansicyan",
    "bar-pipe-active": "ansigreen",
    "bar-pipe-idle": "#888888",
    "bar-pipe-err": "ansired",
    "bar-label": "#aaaaaa",
    "bar-fill-note": "ansigreen",
    "bar-fill-summary": "ansiyellow",
    "bar-fill-window": "ansicyan",
    "bar-empty": "#555555",
    "log-debug": "#888888",
    "log-info": "",
    "log-warning": "ansiyellow",
    "log-error": "ansired",
    "bar-input": "ansigreen",
    "bar-output": "ansiyellow",
    "bar-cache": "ansimagenta",
    "cmd-prompt": "bold ansicyan",
    "cmd-separator": "#444444",
})


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // ESTIMATE_CHARS_PER_TOKEN)


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def _fmt_uptime(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h > 0:
        return f"{h}h{m:02d}m"
    if m > 0:
        return f"{m}m{s:02d}s"
    return f"{s}s"


def _render_bar(current: float, maximum: float, blocks: int = 10) -> tuple[int, int]:
    """Returns (filled_count, empty_count)."""
    if maximum <= 0:
        return 0, blocks
    ratio = min(1.0, current / maximum)
    filled = int(ratio * blocks)
    return filled, blocks - filled


def _render_split_bar(current: float, min_val: float, max_val: float,
                      left_blocks: int = 10, right_blocks: int = 5) -> tuple[int, int, int, int]:
    """Returns (left_filled, left_empty, right_filled, right_empty)."""
    if min_val <= 0:
        left_filled, left_empty = 0, left_blocks
    else:
        left_ratio = min(1.0, current / min_val)
        left_filled = int(left_ratio * left_blocks)
        left_empty = left_blocks - left_filled

    if current <= min_val or max_val <= min_val:
        right_filled, right_empty = 0, right_blocks
    else:
        right_ratio = min(1.0, (current - min_val) / (max_val - min_val))
        right_filled = int(right_ratio * right_blocks)
        right_empty = right_blocks - right_filled

    return left_filled, left_empty, right_filled, right_empty


def _build_registry(config: Config, store: MessageStore) -> ToolRegistry:
    registry = ToolRegistry()

    registry.register(Tool(
        name="http_api_call",
        description="发送 HTTP 请求到任意 URL",
        parameters=HTTP_API_SCHEMA,
        handler=http_api_call,
    ))

    async def _cfg_mgr(args: dict) -> str:
        return await config_manager(args, config=config)
    registry.register(Tool(
        name="config_manager",
        description="读取、修改、热重载配置",
        parameters=CONFIG_MANAGER_SCHEMA,
        handler=_cfg_mgr,
    ))

    async def _mem_search(args: dict, **deps) -> str:
        return await memory_search(args, store=store, group_key=deps.get("group_key", ""))
    registry.register(Tool(
        name="memory_search",
        description="搜索长期记忆，用关键词查找过去保存的信息",
        parameters=MEMORY_SEARCH_SCHEMA,
        handler=_mem_search,
    ))

    async def _mem_save(args: dict, **deps) -> str:
        return await memory_save(args, store=store, group_key=deps.get("group_key", ""))
    registry.register(Tool(
        name="memory_save",
        description="保存一条信息到长期记忆",
        parameters=MEMORY_SAVE_SCHEMA,
        handler=_mem_save,
    ))

    async def _sn(args: dict, **deps) -> str:
        return await self_note_tool(args, store=store, group_key=deps.get("group_key", ""))
    registry.register(Tool(
        name="self_note",
        description="管理对用户的私人印象。add:追加, replace:覆盖",
        parameters=SELF_NOTE_SCHEMA,
        handler=_sn,
    ))

    async def _snd(args: dict, **deps) -> str:
        return await send_tool(args, sender=deps.get("sender"), peer=deps.get("peer"))
    registry.register(Tool(
        name="send",
        description="发送消息到用户。支持 text/image/face/at/reply/forward 段类型。",
        parameters=SEND_TOOL_SCHEMA,
        handler=_snd,
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
        sender={"user_id": user_id, "nickname": "inject"},
        time=int(time.time()),
        self_id=0,
    )


@dataclass
class MemorySnapshot:
    self_note_tokens: float = 0.0
    summaries_count: float = 0.0
    window_tokens: float = 0.0


class _AsyncQueueLogHandler(logging.Handler):
    def __init__(self, queue: asyncio.Queue[logging.LogRecord], loop: asyncio.AbstractEventLoop):
        super().__init__()
        self.queue = queue
        self.loop = loop
        self.setFormatter(logging.Formatter(
            fmt="%(asctime)s [%(levelname)-5s] %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        ))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.format(record)
            self.loop.call_soon_threadsafe(self._enqueue, record)
        except Exception:
            self.handleError(record)

    def _enqueue(self, record: logging.LogRecord) -> None:
        try:
            self.queue.put_nowait(record)
        except asyncio.QueueFull:
            pass


class _LogBufferControl(BufferControl):
    def mouse_handler(self, mouse_event: Any) -> Any:
        app = get_app()
        if (
            self.focus_on_click()
            and app.layout.current_control != self
            and mouse_event.event_type == MouseEventType.MOUSE_DOWN
        ):
            app.layout.current_control = self
        return super().mouse_handler(mouse_event)


class _LogLexer(Lexer):
    def __init__(self, dashboard: "Dashboard"):
        self.dashboard = dashboard

    def lex_document(self, document: Document) -> Any:
        def _get_line(lineno: int) -> list[tuple[str, str]]:
            if 0 <= lineno < len(self.dashboard._log_fragments):
                return self.dashboard._log_fragments[lineno]
            return [("", "")]

        return _get_line

    def invalidation_hash(self) -> int:
        return self.dashboard._log_style_version


class Dashboard:
    def __init__(self, scheduler: PipelineScheduler, store: MessageStore,
                 config: Config, receiver: MessageReceiver | None = None):
        self.scheduler = scheduler
        self.store = store
        self.config = config
        self.receiver = receiver
        self.start_time = time.time()

        self._log_lines: list[str] = []
        self._log_fragments: list[list[tuple[str, str]]] = []
        self._log_style_version = 0
        self._log_window: Window | None = None
        self._log_buffer = Buffer(read_only=True, multiline=True)
        self.log_queue: asyncio.Queue[logging.LogRecord] = asyncio.Queue(maxsize=500)

        self.current_instance: str | None = None
        self.auto_track = True
        self._memory_cache: dict[str, MemorySnapshot] = {}
        self.http_healthy: bool = False

        self._running = False
        self._ws_task: asyncio.Task[None] | None = None
        self.app: Application[None] | None = None
        self._cmd_buffer: Buffer | None = None
        self._cmd_control: BufferControl | None = None
        self._cmd_history: list[str] = []
        self._cmd_history_index: int | None = None
        self._log_follow_tail = True

    def _setup_logging(self) -> None:
        root = logging.getLogger("mutsumi")
        root.setLevel(logging.DEBUG)
        root.handlers.clear()
        root.addHandler(_AsyncQueueLogHandler(self.log_queue, asyncio.get_running_loop()))
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("websockets").setLevel(logging.WARNING)
        logging.getLogger("asyncio").setLevel(logging.WARNING)
        logging.getLogger("prompt_toolkit").setLevel(logging.WARNING)

    def _active_instance(self) -> str | None:
        if not self.auto_track and self.current_instance:
            return self.current_instance
        return self.scheduler._last_active_key

    def _build_line1(self) -> FormattedText:
        app = self.app
        if app is None:
            return FormattedText([("", "[starting...]")])
        width = app.output.get_size().columns

        uptime = _fmt_uptime(time.time() - self.start_time)
        tu = self.scheduler.token_usage

        ws_ok = self.receiver.is_connected if self.receiver else False
        ws = "●" if ws_ok else "○"
        ws_s = "bar-ok" if ws_ok else "bar-err"

        http_s = "bar-ok" if self.http_healthy else "bar-warn"
        http_c = "●" if self.http_healthy else "◐"

        llm_ok = self.scheduler.llm_healthy
        llm_c = "●" if llm_ok else "○"
        llm_s = "bar-ok" if llm_ok else "bar-err"

        cache_total = tu["cache_hit"] + tu["cache_miss"]
        if cache_total > 0:
            cache_pct = tu["cache_hit"] / cache_total * 100
            cache_str = f"{cache_pct:.1f}%"
        else:
            cache_str = "N/A"

        left = [("class:bar-title", f"[Mutsumi's SYNC v3]")]
        right = [
            ("class:bar-dim", f"[uptime:{uptime}]"),
            ("class:bar-dim", "[CONNECT:"),
            (f"class:{ws_s}", ws),
            (f"class:{http_s}", http_c),
            (f"class:{llm_s}", llm_c),
            ("class:bar-dim", "]"),
            ("class:bar-input", f"[↑{_fmt_tokens(tu['input'])}]"),
            ("class:bar-output", f"[↓{_fmt_tokens(tu['output'])}]"),
            ("class:bar-cache", f"[▣{cache_str}]"),
        ]

        return _render_filled_line([left, right], width)

    def _build_line2(self) -> FormattedText:
        app = self.app
        if app is None:
            return FormattedText([])
        width = app.output.get_size().columns

        instance = self._active_instance()
        if not instance:
            left = [("class:bar-instance", "[[no active instance]]")]
            return _render_filled_line([left], width)

        key_short = instance
        if len(key_short) > 30:
            key_short = "..." + key_short[-27:]

        state = self.scheduler._pipeline_states.get(instance, "Idle")

        if state in ("CANCELLED", "ERROR"):
            pipe_style = "class:bar-pipe-err"
        elif state in ("Idle", "DONE"):
            pipe_style = "class:bar-pipe-idle"
        else:
            pipe_style = "class:bar-pipe-active"

        left = [("class:bar-instance", f"[[{key_short}]]")]
        right = [(pipe_style, f"[pipeline:{state}]")]

        return _render_filled_line([left, right], width)

    def _build_line3(self) -> FormattedText:
        app = self.app
        if app is None:
            return FormattedText([])
        width = app.output.get_size().columns

        instance = self._active_instance()
        if not instance:
            left = [("class:bar-dim", "[no instance selected]")]
            return _render_filled_line([left], width)

        snap = self._memory_cache.get(instance, MemorySnapshot())
        cfg_mem = self.config.memory
        cfg_ctx = self.config.context

        sn_filled, sn_empty = _render_bar(snap.self_note_tokens, cfg_mem.self_note_target_tokens)

        s_lf, s_le, s_rf, s_re = _render_split_bar(
            snap.summaries_count,
            float(cfg_ctx.summaries_min_count),
            float(cfg_ctx.summaries_max_count),
        )

        w_lf, w_le, w_rf, w_re = _render_split_bar(
            snap.window_tokens,
            float(cfg_ctx.window_min_tokens),
            float(cfg_ctx.window_max_tokens),
        )

        self_note_anchor: list[tuple[str, str]] = [
            ("class:bar-label", "[self_note:"),
            ("class:bar-fill-note", "■" * sn_filled),
            ("class:bar-empty", "□" * sn_empty),
            ("class:bar-label", "]"),
        ]

        summaries_anchor: list[tuple[str, str]] = [
            ("class:bar-label", "[summaries:"),
            ("class:bar-fill-summary", "■" * s_lf),
            ("class:bar-empty", "□" * s_le),
            ("class:bar-label", "|"),
            ("class:bar-fill-summary", "■" * s_rf),
            ("class:bar-empty", "□" * s_re),
            ("class:bar-label", "]"),
        ]

        window_anchor: list[tuple[str, str]] = [
            ("class:bar-label", "[window:"),
            ("class:bar-fill-window", "■" * w_lf),
            ("class:bar-empty", "□" * w_le),
            ("class:bar-label", "|"),
            ("class:bar-fill-window", "■" * w_rf),
            ("class:bar-empty", "□" * w_re),
            ("class:bar-label", "]"),
        ]

        return _render_filled_line([self_note_anchor, summaries_anchor, window_anchor], width)

    def _scroll_log_to_bottom(self) -> None:
        self._log_follow_tail = True
        if self._log_window:
            self._log_window.vertical_scroll = max(0, len(self._log_fragments) - 1)
        self._sync_log_buffer(cursor_position=len(self._log_buffer.text))

    def _scroll_log_by_pages(self, pages: int) -> None:
        if not self._log_window:
            return

        render_info = getattr(self._log_window, "render_info", None)
        page_height = getattr(render_info, "window_height", None)
        if not isinstance(page_height, int) or page_height <= 0:
            page_height = 10

        current_scroll = int(getattr(self._log_window, "vertical_scroll", 0))
        if pages < 0:
            self._log_follow_tail = False
            next_scroll = max(0, current_scroll - abs(pages) * page_height)
        elif pages > 0:
            max_scroll = max(0, len(self._log_fragments) - page_height)
            next_scroll = min(max_scroll, current_scroll + pages * page_height)
            self._log_follow_tail = next_scroll >= max_scroll
        else:
            return

        self._log_window.vertical_scroll = next_scroll

        if self._log_follow_tail:
            self._scroll_log_to_bottom()
        else:
            cursor_row = min(next_scroll, max(0, len(self._log_buffer.document.lines) - 1))
            cursor_position = self._log_buffer.document.translate_row_col_to_index(cursor_row, 0)
            self._sync_log_buffer(cursor_position=cursor_position)
        if self.app:
            self.app.invalidate()

    def _sync_log_buffer(self, cursor_position: int | None = None) -> None:
        text = "".join(self._log_lines)
        if cursor_position is None:
            cursor_position = self._log_buffer.cursor_position
        cursor_position = max(0, min(cursor_position, len(text)))
        self._log_buffer.set_document(
            Document(text, cursor_position=cursor_position),
            bypass_readonly=True,
        )

    def _clear_log(self) -> None:
        self._log_lines.clear()
        self._log_fragments.clear()
        self._log_style_version += 1
        self._log_follow_tail = True
        self._sync_log_buffer(cursor_position=0)

    def _append_log_line(self, line: str) -> None:
        fragments = _ansi_to_fragments(line)
        line = _ansi_fragments_to_text(fragments)
        was_at_tail = self._log_buffer.cursor_position >= len(self._log_buffer.text)
        cursor_position = self._log_buffer.cursor_position
        self._log_lines.append(line)
        self._log_fragments.extend(_split_fragments_by_line(fragments))
        if len(self._log_lines) > 1000:
            removed = self._log_lines[:200]
            del self._log_lines[:200]
            del self._log_fragments[:200]
            cursor_position = max(0, cursor_position - len("".join(removed)))
        self._log_style_version += 1
        if self._log_follow_tail and was_at_tail:
            self._sync_log_buffer(cursor_position=len("".join(self._log_lines)))
        else:
            self._log_follow_tail = False
            self._sync_log_buffer(cursor_position=cursor_position)

    def _build_status_section(self) -> FormattedText:
        l1 = self._build_line1()
        l2 = self._build_line2()
        l3 = self._build_line3()
        return FormattedText(list(l1) + [("", "\n")] + list(l2) + [("", "\n")] + list(l3))

    def _build_layout(self) -> Layout:
        status_window = Window(
            content=FormattedTextControl(self._build_status_section),
            height=3,
            style="",
        )

        separator = Window(height=1, style="class:cmd-separator",
                           content=FormattedTextControl(
                               lambda: [("class:cmd-separator", "─" * (self.app.output.get_size().columns if self.app else 80))]
                           ))

        self._log_window = Window(
            content=_LogBufferControl(
                buffer=self._log_buffer,
                lexer=_LogLexer(self),
                focusable=True,
                focus_on_click=True,
            ),
            wrap_lines=True,
            style="",
        )

        cmd_buffer = Buffer(multiline=False, history=InMemoryHistory())
        cmd_buffer.accept_handler = self._on_command
        self._cmd_buffer = cmd_buffer
        self._cmd_control = BufferControl(
            buffer=cmd_buffer,
            focus_on_click=True,
        )
        cmd_bar = VSplit([
            Window(content=FormattedTextControl([("class:cmd-prompt", "> ")]),
                   width=2, height=1, dont_extend_width=True),
            Window(content=self._cmd_control, height=1),
        ])

        return Layout(HSplit([
            status_window,
            separator,
            self._log_window,
            cmd_bar,
        ]), focused_element=cmd_buffer)

    def _key_bindings(self) -> KeyBindings:
        kb = KeyBindings()

        @kb.add("c-c")
        def _quit(event):
            if not self._copy_log_selection():
                asyncio.ensure_future(self._quit_handler())

        @kb.add("c-l")
        def _clear(event):
            self._clear_log()
            self.app.invalidate() if self.app else None

        @kb.add("pageup")
        def _pgup(event):
            self._scroll_log_by_pages(-1)

        @kb.add("pagedown")
        def _pgdn(event):
            self._scroll_log_by_pages(1)

        @kb.add("escape")
        def _focus_cmd(event):
            self._focus_command_input()

        @kb.add("up")
        def _history_up(event):
            self._history_move(-1)

        @kb.add("down")
        def _history_down(event):
            self._history_move(1)

        return kb

    def _on_command(self, buffer: Buffer) -> bool:
        accepted = _accept_command(
            buffer,
            lambda line: asyncio.ensure_future(self._exec_command(line)),
            self._remember_command,
        )
        self._focus_command_input()
        return accepted

    def _remember_command(self, line: str) -> None:
        if not self._cmd_history or self._cmd_history[-1] != line:
            self._cmd_history.append(line)
        self._cmd_history_index = None

    def _history_move(self, delta: int) -> None:
        if not self._cmd_buffer or not self._cmd_history:
            return

        if self._cmd_history_index is None:
            self._cmd_history_index = len(self._cmd_history)

        self._cmd_history_index = max(0, min(len(self._cmd_history), self._cmd_history_index + delta))
        if self._cmd_history_index == len(self._cmd_history):
            self._cmd_buffer.text = ""
        else:
            self._cmd_buffer.text = self._cmd_history[self._cmd_history_index]
        self._cmd_buffer.cursor_position = len(self._cmd_buffer.text)

    def _focus_command_input(self) -> None:
        if self.app and self._cmd_buffer:
            self.app.layout.focus(self._cmd_buffer)

    def _copy_log_selection(self) -> bool:
        if not self.app or self._log_buffer.selection_state is None:
            return False
        data = self._log_buffer.copy_selection()
        if not data.text:
            return False
        self.app.clipboard.set_data(data)
        _copy_text_to_system_clipboard(data.text)
        self.app.invalidate()
        return True

    async def _exec_command(self, line: str) -> None:
        try:
            result = await self._dispatch_command(line)
        except Exception as e:
            result = f"[Error: {e}]"
        if result:
            self._append_log_line(result + "\n")
            if self.app:
                self.app.invalidate()

    async def _dispatch_command(self, line: str) -> str:
        cmd = line.split()[0].lower() if line.strip() else ""
        parts = line.split(maxsplit=3)

        if cmd == "/help":
            return self._cmd_help()
        elif cmd == "/quit":
            asyncio.ensure_future(self._quit_handler())
            return ""
        elif cmd == "/clear":
            self._clear_log()
            return ""
        elif cmd == "/list":
            return self._cmd_list()
        elif cmd == "/status":
            return await self._cmd_status()
        elif cmd == "/watch":
            return self._cmd_watch(parts)
        elif cmd == "/auto":
            self.auto_track = True
            self.current_instance = None
            await self._refresh_current_memory()
            return "Auto-tracking latest active instance"
        elif cmd == "/tools":
            return self._cmd_tools()
        elif cmd == "/config":
            return self._cmd_config(parts)
        elif cmd == "/inject":
            return await self._cmd_inject(line)
        elif cmd == "/break":
            return await self._cmd_break(parts)
        elif cmd == "/connect":
            return await self._cmd_connect()
        elif cmd == "/memory":
            return await self._cmd_memory(parts)
        else:
            return f"Unknown command: {cmd}  Type /help for help"

    def _cmd_help(self) -> str:
        return _cmd_help_static()

    def _cmd_list(self) -> str:
        keys = self.scheduler.active_keys()
        current = self._active_instance()
        lines = [f"{_CYAN}Active tasks ({len(keys)}):{_RESET}"]
        for k in keys:
            marker = f" {_GREEN}← current{_RESET}" if k == current else ""
            state = self.scheduler._pipeline_states.get(k, "?")
            lines.append(f"  {k}  [{state}]{marker}")
        return "\n".join(lines) if len(lines) > 1 else lines[0]

    async def _cmd_status(self) -> str:
        st = self.scheduler.status()
        lines = [f"{_CYAN}Scheduler status:{_RESET}"]
        for k, v in st.items():
            lines.append(f"  {k}: {v}")
        instance = self._active_instance()
        if instance:
            snap = self._memory_cache.get(instance)
            if snap:
                lines.append(f"  current instance: {instance}")
                lines.append(f"    self_note: ~{snap.self_note_tokens:.0f}tokens")
                lines.append(f"    summaries: {snap.summaries_count:.0f}")
                lines.append(f"    window: ~{snap.window_tokens:.0f}tokens")
        return "\n".join(lines)

    def _cmd_tools(self) -> str:
        tools = self.scheduler.registry.to_openai_schema()
        lines = [f"{_CYAN}Registered tools ({len(tools)}):{_RESET}"]
        for t in tools:
            name = t["function"]["name"]
            desc = t["function"]["description"][:60]
            lines.append(f"  {_BOLD}{name}{_RESET} — {_DIM}{desc}{_RESET}")
        return "\n".join(lines)

    def _cmd_config(self, parts: list[str]) -> str:
        if len(parts) < 2:
            return "Usage: /config <key> [value]"
        key = parts[1]
        if len(parts) >= 3:
            value = " ".join(parts[2:])
            result = self.config.set(key, value)
            if str(result).startswith("[OK]"):
                self.config.save_key(key)
            return str(result)
        else:
            value = self.config.get(key)
            return f"{key} = {value}"

    def _cmd_watch(self, parts: list[str]) -> str:
        if len(parts) < 2:
            current = self._active_instance()
            if current:
                return f"Current instance: {current}  (auto_track={self.auto_track})"
            return "No instance selected. Use /watch <key> or /auto"
        key = parts[1]
        self.current_instance = key
        self.auto_track = False
        asyncio.ensure_future(self._refresh_current_memory())
        return f"Now watching: {key}"

    async def _cmd_break(self, parts: list[str]) -> str:
        key, label_or_err = _cmd_break_static(parts)
        if key is None:
            return label_or_err or "Invalid /break usage"
        await self.scheduler.cancel_user(key)
        return f"Cancelled: {label_or_err}"

    async def _cmd_inject(self, line: str) -> str:
        event, label_or_err = _cmd_inject_helper(line)
        if event is None:
            return label_or_err
        await self.scheduler.dispatch(event)
        return f"Injected {label_or_err}: {event.raw_message[:50]}"

    async def _cmd_connect(self) -> str:
        if self.receiver and self.receiver._running:
            return "Already connected"
        if self.receiver:
            try:
                await self.receiver.close()
            except Exception:
                pass
        real_sender = MessageSender(self.config.napcat.http_url, self.config.napcat.access_token)
        self.scheduler.sender = real_sender
        self.receiver = MessageReceiver(self.config.napcat.ws_url, self.config.napcat.access_token)
        self.receiver.on_message(self.scheduler.dispatch)
        self._ws_task = asyncio.create_task(self.receiver.run())
        return f"Connecting to NapCat: {self.config.napcat.ws_url}"

    async def _cmd_memory(self, parts: list[str]) -> str:
        key = parts[1] if len(parts) >= 2 else self._active_instance()
        if not key:
            return "No instance specified. Use /memory <key> or /watch first"
        await self._refresh_memory_snapshot(key)
        snap = self._memory_cache.get(key)
        if not snap:
            return f"No memory snapshot for {key}"
        note = await self.store.get_current_self_note(key)
        note_preview = ""
        if note and note.get("content"):
            note_preview = note["content"][:200].replace("\n", " ")
        summaries = await self.store.get_summaries(key, limit=5)
        lines = [f"{_CYAN}Memory for {key}:{_RESET}"]
        lines.append(f"  self_note: ~{snap.self_note_tokens:.0f} tokens")
        if note_preview:
            lines.append(f"    preview: {_DIM}{note_preview}{_RESET}")
        lines.append(f"  summaries: {snap.summaries_count:.0f} (max {self.config.context.summaries_max_count})")
        if summaries:
            for s in summaries[:3]:
                lines.append(f"    {_DIM}[{s['source']}] {s['summary'][:80]}{_RESET}")
        lines.append(f"  window: ~{snap.window_tokens:.0f} tokens "
                     f"(min {self.config.context.window_min_tokens}, max {self.config.context.window_max_tokens})")
        return "\n".join(lines)

    async def _log_consumer(self) -> None:
        while self._running:
            try:
                record = await asyncio.wait_for(self.log_queue.get(), timeout=0.2)
            except asyncio.TimeoutError:
                continue

            self._append_log_line(
                f"[{record.asctime}] [{record.levelname:<5}] {record.name}: {record.getMessage()}\n"
            )
            if self.app:
                self.app.invalidate()

    async def _health_check_loop(self) -> None:
        while self._running:
            try:
                url = self.config.napcat.http_url.rstrip("/")
                async with httpx.AsyncClient(timeout=5) as client:
                    resp = await client.get(f"{url}/get_login_info",
                                            headers={"Authorization": f"Bearer {self.config.napcat.access_token}"})
                    self.http_healthy = resp.status_code == 200
            except Exception:
                self.http_healthy = False
            await asyncio.sleep(30)

    async def _refresh_memory_snapshot(self, key: str) -> None:
        try:
            note = await self.store.get_current_self_note(key)
            note_tokens = _estimate_tokens(note["content"]) if note and note.get("content") else 0

            summaries = await self.store.get_summaries(key, limit=self.config.context.summaries_max_count)
            summaries_count = len(summaries)

            window = self.scheduler._windows.get(key)
            window_tokens = 0
            if window:
                window_tokens = sum(
                    _estimate_tokens(str(m.get("content", "")))
                    for m in window.get_context()
                )

            self._memory_cache[key] = MemorySnapshot(
                self_note_tokens=float(note_tokens),
                summaries_count=float(summaries_count),
                window_tokens=float(window_tokens),
            )
        except Exception:
            logger.exception("Memory snapshot refresh failed for %s", key)

    async def _refresh_current_memory(self) -> None:
        instance = self._active_instance()
        if instance:
            await self._refresh_memory_snapshot(instance)

    async def _memory_refresh_loop(self) -> None:
        while self._running:
            await self._refresh_current_memory()
            await asyncio.sleep(3)

    async def _tick_loop(self) -> None:
        while self._running:
            await asyncio.sleep(1)
            if self.app:
                self.app.invalidate()

    async def _quit_handler(self) -> None:
        logger.info("[DASH] Shutting down...")
        self._append_log_line("[DASH] Shutting down...\n")

        if self._ws_task:
            self._ws_task.cancel()
            self._ws_task = None
        if self.receiver:
            try:
                await self.receiver.close()
            except Exception:
                pass

        self.scheduler.on_state_change = None
        await self.scheduler.shutdown()

        self._running = False
        await asyncio.sleep(0.1)
        while not self.log_queue.empty():
            try:
                record = self.log_queue.get_nowait()
                self._append_log_line(
                    f"[{record.asctime}] [{record.levelname:<5}] {record.name}: {record.getMessage()}\n"
                )
            except asyncio.QueueEmpty:
                break

        self._append_log_line("[DASH] Shutdown complete — exiting\n")
        self._scroll_log_to_bottom()
        if self.app:
            self.app.invalidate()
            await asyncio.sleep(0.5)
            self.app.exit()

    async def run(self) -> None:
        self._running = True
        self._setup_logging()
        logger.info("Dashboard starting — config loaded")

        m = self.config.model
        if m.api_key:
            logger.info("API: %s @ %s model=%s", m.provider, m.base_url, m.model)
        else:
            logger.info("API: not configured — pipeline will use local stub")

        if self.receiver:
            logger.info("Starting NapCat receiver on %s", self.config.napcat.ws_url)

        self.app = Application(
            layout=self._build_layout(),
            full_screen=True,
            key_bindings=self._key_bindings(),
            style=DASHBOARD_STYLE,
            mouse_support=True,
        )

        log_task = asyncio.create_task(self._log_consumer())
        health_task = asyncio.create_task(self._health_check_loop())
        mem_task = asyncio.create_task(self._memory_refresh_loop())
        tick_task = asyncio.create_task(self._tick_loop())
        if self.receiver:
            self._ws_task = asyncio.create_task(self.receiver.run())

        def _on_change():
            if self.app:
                self.app.invalidate()
            asyncio.ensure_future(self._refresh_current_memory())

        self.scheduler.on_state_change = _on_change

        try:
            await self.app.run_async()
        finally:
            self._running = False
            self.scheduler.on_state_change = None
            for t in [log_task, health_task, mem_task, tick_task]:
                t.cancel()
            if self._ws_task:
                self._ws_task.cancel()
            if self.receiver:
                await self.receiver.close()


def _render_filled_line(anchors: list[list[tuple[str, str]]], terminal_width: int,
                        filler_style: str = "class:bar-dim") -> FormattedText:
    total_text = sum(sum(len(t) for _, t in a) for a in anchors)
    spacer_count = len(anchors) - 1

    if spacer_count <= 0:
        result = list(anchors[0]) if anchors else []
        remaining = terminal_width - total_text
        if remaining > 0:
            result.append((filler_style, "=" * remaining))
        return FormattedText(result)

    available = max(1, terminal_width - total_text)
    each = available // spacer_count
    remainder = available - each * spacer_count

    result: list[tuple[str, str]] = []
    for i, anchor in enumerate(anchors):
        result.extend(anchor)
        if i < spacer_count:
            extra = 1 if i < remainder else 0
            result.append((filler_style, "=" * (each + extra)))
        else:
            used = total_text + each * spacer_count + remainder
            remaining = terminal_width - used
            if remaining > 0:
                result.append((filler_style, "=" * remaining))

    return FormattedText(result)


def _accept_command(buffer: Any, schedule: Any, remember: Any | None = None) -> bool:
    line = str(getattr(buffer, "text", ""))
    if line.strip():
        schedule(line)
        if remember is not None:
            remember(line)
        buffer.append_to_history()
    buffer.reset()
    return True


def _cmd_help_static() -> str:
    return (
        f"{_CYAN}Dashboard Commands:{_RESET}\n"
        f"  {_BOLD}/watch{_RESET} [key]              switch monitored instance\n"
        f"  {_BOLD}/auto{_RESET}                     auto-track latest active instance\n"
        f"  {_BOLD}/list{_RESET}                     list active tasks\n"
        f"  {_BOLD}/status{_RESET}                   full scheduler status\n"
        f"  {_BOLD}/tools{_RESET}                    list registered tools\n"
        f"  {_BOLD}/config{_RESET} [key] [value]     get/set config\n"
        f"  {_BOLD}/memory{_RESET} [key]             show memory summary for instance\n"
        f"  {_BOLD}/inject{_RESET} private <uid> <msg>    inject private message\n"
        f"  {_BOLD}/inject{_RESET} group <gid> <uid> <msg> inject group message\n"
        f"  {_BOLD}/break{_RESET} ...                    cancel pipeline\n"
        f"  {_BOLD}/connect{_RESET}                   connect to NapCat WebSocket\n"
        f"  {_BOLD}/clear{_RESET}                     clear log area\n"
        f"  {_BOLD}/quit{_RESET}                      exit\n"
        f"  {_BOLD}Ctrl+L{_RESET}                     clear log area\n"
        f"  {_BOLD}Ctrl+C{_RESET}                     exit"
    )


def _cmd_break_static(parts: list[str]) -> tuple[str | None, str | None]:
    """Parse /break command, returns (key, label) or (None, error)."""
    if len(parts) < 2:
        return None, f"Usage: /break private <user_id>  or  /break group <group_id> <user_id>"
    if parts[1] == "private" and len(parts) >= 3:
        return f"private:{parts[2]}", f"private:{parts[2]}"
    elif parts[1] == "group" and len(parts) >= 4:
        return f"group:{parts[2]}:{parts[3]}", f"group:{parts[2]}:{parts[3]}"
    else:
        return f"private:{parts[1]}", f"private:{parts[1]}"


def _cmd_inject_helper(line: str) -> tuple[MessageEvent | None, str]:
    """Parse /inject command, returns (event, label) or (None, error)."""
    parts = line.split(maxsplit=2)
    if len(parts) < 2:
        return None, (
            f"{_CYAN}Usage:{_RESET} /inject [private <user_id> | group <group_id> <user_id>] <message>\n"
            f"  Examples: /inject private 123456 Hello\n"
            f"            /inject group 789000 123456 Hello"
        )
    if parts[1] == "private":
        private_parts = line.split(maxsplit=3)
        if len(private_parts) < 4:
            return None, "Invalid /inject private format"
        uid = int(private_parts[2])
        msg = private_parts[3]
        return _fake_event(uid, None, msg), f"private:{uid}"
    elif parts[1] == "group":
        group_parts = line.split(maxsplit=4)
        if len(group_parts) < 5:
            return None, "Invalid /inject group format"
        gid = int(group_parts[2])
        uid = int(group_parts[3])
        msg = group_parts[4]
        return _fake_event(uid, gid, msg), f"group:{gid}:{uid}"
    return None, "Invalid /inject format"


def _scroll_window_by_pages(window: Any, pages: int) -> None:
    """Scroll by rendered page height; prompt_toolkit Window.height is a dimension hint."""
    render_info = getattr(window, "render_info", None)
    page_height = getattr(render_info, "window_height", None)
    if not isinstance(page_height, int) or page_height <= 0:
        page_height = 10

    next_scroll = int(getattr(window, "vertical_scroll", 0)) + pages * page_height
    window.vertical_scroll = max(0, next_scroll)


def main() -> None:
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"

    async def _run():
        config = Config.load(config_path)
        store = MessageStore()
        await store.initialize()

        registry = _build_registry(config, store)
        sender = _FakeSender()
        scheduler = PipelineScheduler(config=config, registry=registry, sender=sender, store=store)

        dashboard = Dashboard(scheduler=scheduler, store=store, config=config, receiver=None)
        await scheduler.startup()
        await dashboard.run()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        print(f"\n{_YELLOW}Interrupted{_RESET}")


if __name__ == "__main__":
    main()
