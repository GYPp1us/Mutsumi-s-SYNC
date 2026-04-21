from prompt_toolkit import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import HSplit, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.styles import Style
from prompt_toolkit.history import FileHistory
from prompt_toolkit.completion import Completer, Completion

import threading
import time
from .theme import SOLARIZED_DARK, STYLE
from .commands import (
    CommandRegistry, ConfigCommand, LogsCommand,
    StatusCommand, StatsCommand, ReloadCommand,
    ExitCommand, HelpCommand, ConversationsCommand
)


class CommandCompleter(Completer):
    def __init__(self, registry: CommandRegistry):
        self.registry = registry

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        parts = text.split()

        if not parts:
            for name in self.registry.all():
                yield Completion(name, start_position=0)
            return

        if len(parts) == 1:
            for name in self.registry.all():
                if name.startswith(parts[0]):
                    yield Completion(name, start_position=-len(parts[0]))
        else:
            cmd_name = parts[0]
            partial = parts[-1]
            for opt in self.registry.complete(cmd_name, partial):
                yield Completion(opt, start_position=-len(partial))


class REPL:
    def __init__(self, config_loader=None, status_provider=None,
                 stats_provider=None, reload_handler=None,
                 conversation_callback=None, log_path="./logs/mutsumi.log"):

        self._log_path = log_path
        self.registry = CommandRegistry()
        self._setup_commands(config_loader, status_provider, stats_provider,
                           reload_handler, conversation_callback)

        self.output_lines = []
        self.status = {"ai_online": False, "napcat_online": False,
                      "message_count": 0, "uptime": "00:00:00"}
        self._status_lock = threading.Lock()
        self._start_time = time.time()
        self._log_path = log_path
        
        self.history = FileHistory(".mutsumi_history")
        self.buffer = Buffer(completer=CommandCompleter(self.registry),
                            history=self.history,
                            multiline=False)

        self._app = None
        self._build_app()

    def _setup_commands(self, config_loader, status_provider, stats_provider,
                       reload_handler, conversation_callback):
        self.registry.register(ConfigCommand(config_loader))
        self.registry.register(LogsCommand(self._log_path))
        self.registry.register(StatusCommand(status_provider))
        self.registry.register(StatsCommand(stats_provider))
        self.registry.register(ReloadCommand(reload_handler))
        self.registry.register(ExitCommand())
        self.registry.register(HelpCommand(self.registry))
        self.registry.register(ConversationsCommand(conversation_callback))

    def _exit(self):
        if self._app:
            self._app.exit()

    def _build_app(self):
        kb = KeyBindings()

        @kb.add("c-c", eager=True)
        @kb.add("c-d", eager=True)
        def _(event):
            event.app.exit()

        @kb.add("enter")
        def _(event):
            self._execute()
            self.buffer.text = ""

        status_text = FormattedTextControl(
            text=self._get_status_text(),
            style="class:status-bar"
        )

        output_control = FormattedTextControl(
            text=self._get_output_text(),
            style="class:output"
        )

        input_control = BufferControl(buffer=self.buffer)

        layout = Layout(
            HSplit([
                Window(status_text, height=1),
                Window(output_control, style="class:output"),
                Window(input_control, height=1),
            ])
        )

        self._app = Application(
            layout=layout,
            style=STYLE,
            key_bindings=kb,
            full_screen=True,
        )

    def _get_status_text(self):
        ai = "●" if self.status.get("ai_online") else "○"
        nc = "●" if self.status.get("napcat_online") else "○"
        msg = self.status.get("message_count", 0)

        elapsed = int(time.time() - self._start_time)
        mins, secs = divmod(elapsed, 60)
        hours, mins = divmod(mins, 60)
        uptime = f"{hours:02d}:{mins:02d}:{secs:02d}"

        with self._status_lock:
            self.status["uptime"] = uptime

        return f"{ai} AI | {nc} NapCat | MSG: {msg} | Uptime: {uptime}  "

    def _get_output_text(self):
        return "\n".join(self.output_lines[-20:])

    def _execute(self):
        text = self.buffer.text.strip()
        if not text:
            return

        self.output_lines.append(f"❯ {text}")

        parts = text.split()
        cmd = self.registry.get(parts[0])

        if cmd:
            result = cmd.execute(parts[1:])
            if result == "__EXIT__":
                self._exit()
                return
            if result:
                self.output_lines.append(result)
        else:
            self.output_lines.append(f"Unknown command: {parts[0]}")

        self._refresh_output()

    def _refresh_output(self):
        if self._app and hasattr(self._app, 'invalidate'):
            self._app.invalidate()

    def update_status(self, **kwargs):
        with self._status_lock:
            self.status.update(kwargs)

    def run(self):
        self._app.run()