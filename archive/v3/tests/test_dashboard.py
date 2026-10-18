from __future__ import annotations

from types import SimpleNamespace
import asyncio
import logging

from prompt_toolkit.layout.controls import BufferControl
from prompt_toolkit.selection import SelectionType

from src.mutsumi_sync.tui.dashboard import (
    _AsyncQueueLogHandler,
    Dashboard,
    _accept_command,
    _ansi_to_fragments,
    _cmd_inject_helper,
    _copy_text_to_system_clipboard,
    _scroll_window_by_pages,
)
from src.mutsumi_sync.config import Config


def test_inject_private_preserves_spaces_in_message():
    event, label = _cmd_inject_helper("/inject private 123 hello dashboard world")

    assert label == "private:123"
    assert event is not None
    assert event.raw_message == "hello dashboard world"


def test_inject_group_preserves_spaces_in_message():
    event, label = _cmd_inject_helper("/inject group 456 123 hello dashboard world")

    assert label == "group:456:123"
    assert event is not None
    assert event.raw_message == "hello dashboard world"


def test_scroll_window_uses_rendered_height():
    window = SimpleNamespace(vertical_scroll=10, height=None, render_info=SimpleNamespace(window_height=4))

    _scroll_window_by_pages(window, -1)
    assert window.vertical_scroll == 6

    _scroll_window_by_pages(window, 1)
    assert window.vertical_scroll == 10


async def test_async_queue_log_handler_uses_running_loop():
    queue: asyncio.Queue[logging.LogRecord] = asyncio.Queue()
    handler = _AsyncQueueLogHandler(queue, asyncio.get_running_loop())

    record = logging.LogRecord("mutsumi.test", logging.INFO, __file__, 1, "hello", (), None)
    handler.emit(record)
    await asyncio.sleep(0)

    queued = queue.get_nowait()
    assert queued is record


def test_accept_command_clears_buffer_and_keeps_history():
    scheduled: list[str] = []
    buffer = SimpleNamespace(
        text="/help",
        reset=lambda: setattr(buffer, "text", ""),
        append_to_history=lambda: scheduled.append("history"),
    )

    accepted = _accept_command(buffer, lambda line: scheduled.append(line))

    assert accepted is True
    assert scheduled == ["/help", "history"]
    assert buffer.text == ""


def test_accept_command_records_dashboard_history():
    scheduled: list[str] = []
    remembered: list[str] = []
    buffer = SimpleNamespace(
        text="/inject private 123 hello",
        reset=lambda: setattr(buffer, "text", ""),
        append_to_history=lambda: scheduled.append("history"),
    )

    _accept_command(buffer, scheduled.append, remembered.append)

    assert scheduled == ["/inject private 123 hello", "history"]
    assert remembered == ["/inject private 123 hello"]
    assert buffer.text == ""


def test_dashboard_history_move_fills_command_buffer():
    dashboard = Dashboard(
        scheduler=SimpleNamespace(),
        store=SimpleNamespace(),
        config=Config(),
    )
    buffer = SimpleNamespace(text="", cursor_position=0)
    dashboard._cmd_buffer = buffer
    dashboard._remember_command("/help")
    dashboard._remember_command("/status")

    dashboard._history_move(-1)
    assert buffer.text == "/status"
    assert buffer.cursor_position == len("/status")

    dashboard._history_move(-1)
    assert buffer.text == "/help"

    dashboard._history_move(1)
    assert buffer.text == "/status"

    dashboard._history_move(1)
    assert buffer.text == ""


def test_layout_uses_selectable_log_buffer_but_focuses_command_input():
    dashboard = Dashboard(
        scheduler=SimpleNamespace(),
        store=SimpleNamespace(),
        config=Config(),
    )

    layout = dashboard._build_layout()

    assert isinstance(dashboard._log_window.content, BufferControl)
    assert dashboard._log_window.content.focus_on_click()
    assert dashboard._log_window.content.lexer is not None
    assert dashboard._log_window.wrap_lines()
    assert layout.current_buffer is dashboard._cmd_buffer


def test_command_input_can_be_refocused_with_mouse():
    dashboard = Dashboard(
        scheduler=SimpleNamespace(),
        store=SimpleNamespace(),
        config=Config(),
    )

    dashboard._build_layout()

    assert dashboard._cmd_control is not None
    assert dashboard._cmd_control.focus_on_click()


def test_focus_command_input_uses_application_layout():
    dashboard = Dashboard(
        scheduler=SimpleNamespace(),
        store=SimpleNamespace(),
        config=Config(),
    )
    buffer = SimpleNamespace()
    focused: list[object] = []
    dashboard._cmd_buffer = buffer
    dashboard.app = SimpleNamespace(layout=SimpleNamespace(focus=focused.append))

    dashboard._focus_command_input()

    assert focused == [buffer]


def test_copy_log_selection_puts_selected_text_on_clipboard():
    class Clipboard:
        data = None

        def set_data(self, data):
            self.data = data

    dashboard = Dashboard(
        scheduler=SimpleNamespace(),
        store=SimpleNamespace(),
        config=Config(),
    )
    dashboard._append_log_line("alpha beta gamma\n")
    dashboard._log_buffer.cursor_position = 0
    dashboard._log_buffer.start_selection(selection_type=SelectionType.CHARACTERS)
    dashboard._log_buffer.cursor_position = len("alpha beta")
    clipboard = Clipboard()
    dashboard.app = SimpleNamespace(clipboard=clipboard, invalidate=lambda: None)

    copied = dashboard._copy_log_selection()

    assert copied is True
    assert clipboard.data.text == "alpha beta"
    assert dashboard._log_buffer.selection_state is None


def test_copy_log_selection_also_updates_system_clipboard(monkeypatch):
    copied_text: list[str] = []
    monkeypatch.setattr(
        "src.mutsumi_sync.tui.dashboard._copy_text_to_system_clipboard",
        copied_text.append,
    )

    class Clipboard:
        def set_data(self, data):
            pass

    dashboard = Dashboard(
        scheduler=SimpleNamespace(),
        store=SimpleNamespace(),
        config=Config(),
    )
    dashboard._append_log_line("alpha beta gamma\n")
    dashboard._log_buffer.cursor_position = 0
    dashboard._log_buffer.start_selection(selection_type=SelectionType.CHARACTERS)
    dashboard._log_buffer.cursor_position = len("alpha beta")
    dashboard.app = SimpleNamespace(clipboard=Clipboard(), invalidate=lambda: None)

    copied = dashboard._copy_log_selection()

    assert copied is True
    assert copied_text == ["alpha beta"]


def test_system_clipboard_helper_reports_failure_for_failed_command(monkeypatch):
    def fake_run(*args, **kwargs):
        raise OSError("missing clipboard command")

    monkeypatch.setattr("subprocess.run", fake_run)

    assert _copy_text_to_system_clipboard("hello") is False


def test_log_scroll_follows_bottom_by_default():
    dashboard = Dashboard(
        scheduler=SimpleNamespace(),
        store=SimpleNamespace(),
        config=Config(),
    )

    dashboard._append_log_line("one\n")
    dashboard._append_log_line("two\n")

    assert dashboard._log_buffer.text == "one\ntwo\n"
    assert dashboard._log_buffer.cursor_position == len("one\ntwo\n")


def test_ansi_log_text_stays_selectable_plain_text():
    dashboard = Dashboard(
        scheduler=SimpleNamespace(),
        store=SimpleNamespace(),
        config=Config(),
    )

    dashboard._append_log_line("\033[31merror\033[0m plain\n")

    assert dashboard._log_buffer.text == "error plain\n"
    assert "\033[" not in dashboard._log_buffer.text


def test_log_lexer_renders_ansi_styles_over_plain_buffer_text():
    dashboard = Dashboard(
        scheduler=SimpleNamespace(),
        store=SimpleNamespace(),
        config=Config(),
    )
    dashboard._append_log_line("\033[31merror\033[0m plain\n")
    dashboard._build_layout()

    lexer = dashboard._log_window.content.lexer
    assert lexer is not None
    fragments = lexer.lex_document(dashboard._log_buffer.document)(0)

    assert fragments == [
        ("ansired", "error"),
        ("", " plain"),
    ]


async def test_command_output_preserves_ansi_styles_in_log_lexer():
    dashboard = Dashboard(
        scheduler=SimpleNamespace(),
        store=SimpleNamespace(),
        config=Config(),
    )

    await dashboard._exec_command("/help")
    dashboard._build_layout()

    assert "\033[" not in dashboard._log_buffer.text
    first_line = dashboard._log_window.content.lexer.lex_document(dashboard._log_buffer.document)(0)
    assert first_line == [
        ("ansicyan", "Dashboard Commands:"),
    ]


def test_ansi_fragments_preserve_color_styles():
    fragments = _ansi_to_fragments("\033[31merror\033[0m \033[1mcmd\033[0m")

    assert fragments == [
        ("ansired", "error"),
        ("", " "),
        ("bold", "cmd"),
    ]


def test_log_pageup_disables_follow_tail_and_pagedown_restores_at_bottom():
    dashboard = Dashboard(
        scheduler=SimpleNamespace(),
        store=SimpleNamespace(),
        config=Config(),
    )
    window = SimpleNamespace(render_info=SimpleNamespace(window_height=3))
    for line in ["one\n", "two\n", "three\n", "four\n", "five\n"]:
        dashboard._append_log_line(line)
    dashboard._log_window = window

    window.vertical_scroll = 2
    dashboard._scroll_log_by_pages(-1)
    assert dashboard._log_follow_tail is False
    assert window.vertical_scroll == 0
    assert dashboard._log_buffer.document.cursor_position_row == 0

    window.vertical_scroll = 0
    dashboard._scroll_log_by_pages(1)
    assert dashboard._log_follow_tail is True
    assert dashboard._log_buffer.cursor_position == len(dashboard._log_buffer.text)


def test_log_append_preserves_manual_scroll_position():
    dashboard = Dashboard(
        scheduler=SimpleNamespace(),
        store=SimpleNamespace(),
        config=Config(),
    )
    window = SimpleNamespace(render_info=SimpleNamespace(window_height=3))
    dashboard._log_window = window
    for line in ["one\n", "two\n", "three\n", "four\n", "five\n"]:
        dashboard._append_log_line(line)

    dashboard._scroll_log_by_pages(-1)
    scroll = window.vertical_scroll
    dashboard._append_log_line("six\n")

    assert dashboard._log_follow_tail is False
    assert window.vertical_scroll == scroll
    assert dashboard._log_buffer.text.endswith("six\n")
