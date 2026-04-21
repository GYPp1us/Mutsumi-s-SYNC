# TUI 重构实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 TUI 重构为基于 prompt_toolkit 的命令行交互界面，支持命令语法补全、状态栏、日志查看

**Architecture:** 采用分层架构 - REPL 核心负责输入/补全，命令分发器执行命令，日志缓冲区显示输出，状态栏定期刷新

**Tech Stack:** Python, prompt_toolkit>=3.0.0

---

## 文件结构

```
src/mutsumi_sync/tui/
├── __init__.py
├── app.py              # 主入口，保留供兼容
├── theme.py            # Solarized Dark 主题配置
├── repl.py             # REPL 核心 (含补全、历史)
├── commands/           # 命令模块
│   ├── __init__.py
│   ├── base.py         # 命令基类
│   ├── config.py       # config 命令
│   ├── logs.py         # logs 命令
│   ├── status.py       # status/stats 命令
│   ├── system.py       # reload 命令
│   └── conversations.py # conversations 命令
└── widgets/
    ├── status_bar.py   # 状态栏
    └── output.py       # 日志输出区域
```

---

## Task 1: 添加依赖

**Files:**
- Modify: `requirements.txt`

- [ ] **Step 1: 添加 prompt_toolkit 依赖**

```bash
echo "prompt_toolkit>=3.0.0" >> requirements.txt
```

Run: `cat requirements.txt`
Expected: 最后一行包含 `prompt_toolkit>=3.0.0`

- [ ] **Step 2: 安装依赖**

Run: `pip install prompt_toolkit>=3.0.0`
Expected: 安装成功

- [ ] **Step 3: 提交**

```bash
git add requirements.txt
git commit -m "deps: add prompt_toolkit for new TUI"
```

---

## Task 2: 创建主题配置

**Files:**
- Create: `src/mutsumi_sync/tui/theme.py`

- [ ] **Step 1: 创建 theme.py**

```python
from prompt_toolkit.styles import Style

SOLARIZED_DARK = {
    "bg": "#002b36",
    "fg": "#839496",
    "accent": "#2aa198",
    "warning": "#b58900",
    "error": "#dc322f",
    "success": "#859900",
    "status-bg": "#073642",
    "border": "#586e75",
}

STYLE = Style.from_dict({
    "output": f"bg:{SOLARIZED_DARK['bg']} fg:{SOLARIZED_DARK['fg']}",
    "status-bar": f"bg:{SOLARIZED_DARK['status-bg']} fg:{SOLARIZED_DARK['fg']}",
    "status-online": f"fg:{SOLARIZED_DARK['success']}",
    "status-offline": f"fg:{SOLARIZED_DARK['error']}",
    "prompt": f"fg:{SOLARIZED_DARK['accent']}",
    "input": f"fg:{SOLARIZED_DARK['fg']} bg:{SOLARIZED_DARK['bg']}",
    "completion-menu": f"bg:{SOLARIZED_DARK['status-bg']} fg:{SOLARIZED_DARK['fg']}",
    "completion-menu.selected": f"bg:{SOLARIZED_DARK['accent']} fg:{SOLARIZED_DARK['bg']}",
})
```

- [ ] **Step 2: 提交**

```bash
git add src/mutsumi_sync/tui/theme.py
git commit -m "feat: add Solarized Dark theme config"
```

---

## Task 3: 创建命令基类

**Files:**
- Create: `src/mutsumi_sync/tui/commands/base.py`
- Create: `src/mutsumi_sync/tui/commands/__init__.py`

- [ ] **Step 1: 创建 commands/__init__.py**

```python
from .base import Command, CommandRegistry
from .config import ConfigCommand
from .logs import LogsCommand
from .status import StatusCommand
from .system import SystemCommand
from .conversations import ConversationsCommand

__all__ = [
    "Command",
    "CommandRegistry",
    "ConfigCommand",
    "LogsCommand",
    "StatusCommand",
    "SystemCommand",
    "ConversationsCommand",
]
```

- [ ] **Step 2: 创建 base.py**

```python
from abc import ABC, abstractmethod
from typing import List, Optional

class Command(ABC):
    name: str = ""
    help: str = ""
    
    @abstractmethod
    def execute(self, args: List[str]) -> str:
        pass
    
    def complete(self, partial: str) -> List[str]:
        return []

class CommandRegistry:
    def __init__(self):
        self._commands: dict[str, Command] = {}
    
    def register(self, cmd: Command):
        self._commands[cmd.name] = cmd
    
    def get(self, name: str) -> Optional[Command]:
        return self._commands.get(name)
    
    def all(self) -> dict[str, Command]:
        return self._commands
    
    def complete(self, cmd_name: str, partial: str) -> List[str]:
        cmd = self._commands.get(cmd_name)
        if cmd:
            return cmd.complete(partial)
        return []
```

- [ ] **Step 3: 提交**

```bash
git add src/mutsumi_sync/tui/commands/base.py src/mutsumi_sync/tui/commands/__init__.py
git commit -m "feat: add command base classes"
```

---

## Task 4: 实现 config 命令

**Files:**
- Create: `src/mutsumi_sync/tui/commands/config.py`

- [ ] **Step 1: 创建 config.py**

```python
from .base import Command
from typing import List
import yaml

class ConfigCommand(Command):
    name = "config"
    help = "Configuration management"
    
    def __init__(self, config_loader=None):
        self._config_loader = config_loader
    
    def execute(self, args: List[str]) -> str:
        if not args:
            return "Usage: config <get|set|list|reload>"
        
        subcmd = args[0]
        
        if subcmd == "list":
            return self._list()
        elif subcmd == "get" and len(args) >= 2:
            return self._get(args[1])
        elif subcmd == "set" and len(args) >= 3:
            return self._set(args[1], args[2])
        elif subcmd == "reload":
            return self._reload()
        
        return f"Unknown subcommand: {subcmd}"
    
    def _list(self) -> str:
        if not self._config_loader:
            return "Config loader not available"
        try:
            config = self._config_loader.load()
            lines = []
            for key, value in config.items():
                lines.append(f"  {key}: {value}")
            return "\n".join(["Config:", *lines])
        except Exception as e:
            return f"Error: {e}"
    
    def _get(self, key: str) -> str:
        if not self._config_loader:
            return "Config loader not available"
        try:
            config = self._config_loader.load()
            value = config.get(key, f"Key not found: {key}")
            if key.endswith("api_key") or key.endswith("token"):
                value = "***hidden***"
            return f"{key} = {value}"
        except Exception as e:
            return f"Error: {e}"
    
    def _set(self, key: str, value: str) -> str:
        if not self._config_loader:
            return "Config loader not available"
        try:
            self._config_loader.set(key, value)
            return f"Set {key} = {value}"
        except Exception as e:
            return f"Error: {e}"
    
    def _reload(self) -> str:
        if not self._config_loader:
            return "Config loader not available"
        try:
            self._config_loader.reload()
            return "Config reloaded"
        except Exception as e:
            return f"Error: {e}"
    
    def complete(self, partial: str) -> List[str]:
        if not partial:
            return ["get", "set", "list", "reload"]
        if partial in ["get", "set", "list", "reload"]:
            return [partial]
        return []
```

- [ ] **Step 2: 提交**

```bash
git add src/mutsumi_sync/tui/commands/config.py
git commit -m "feat: implement config command"
```

---

## Task 5: 实现 logs 命令

**Files:**
- Create: `src/mutsumi_sync/tui/commands/logs.py`

- [ ] **Step 1: 创建 logs.py**

```python
from .base import Command
from typing import List
import os

class LogsCommand(Command):
    name = "logs"
    help = "Log operations"
    
    def __init__(self, log_path: str = "./logs/mutsumi.log"):
        self._log_path = log_path
    
    def execute(self, args: List[str]) -> str:
        if not args:
            return "Usage: logs <tail|grep|level>"
        
        subcmd = args[0]
        
        if subcmd == "tail":
            n = 20
            if len(args) >= 2 and args[1].startswith("-n"):
                try:
                    n = int(args[1][2:]) if args[1][2:] else int(args[2] if len(args) > 2 else 20)
                except ValueError:
                    return "Invalid -n value"
            return self._tail(n)
        elif subcmd == "grep" and len(args) >= 2:
            return self._grep(args[1])
        elif subcmd == "level" and len(args) >= 2:
            return self._level(args[1])
        
        return f"Unknown subcommand: {subcmd}"
    
    def _tail(self, n: int) -> str:
        if not os.path.exists(self._log_path):
            return f"Log file not found: {self._log_path}"
        try:
            with open(self._log_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            return "".join(lines[-n:])
        except Exception as e:
            return f"Error reading log: {e}"
    
    def _grep(self, pattern: str) -> str:
        if not os.path.exists(self._log_path):
            return f"Log file not found: {self._log_path}"
        try:
            with open(self._log_path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            matched = [l for l in lines if pattern in l]
            if not matched:
                return f"No matches found: {pattern}"
            return "".join(matched[-50:])
        except Exception as e:
            return f"Error searching log: {e}"
    
    def _level(self, level: str) -> str:
        valid = ["DEBUG", "INFO", "WARNING", "ERROR"]
        if level.upper() not in valid:
            return f"Invalid level. Use: {', '.join(valid)}"
        return f"Log level set to {level.upper()} (runtime change not implemented)"
    
    def complete(self, partial: str) -> List[str]:
        if not partial:
            return ["tail", "grep", "level"]
        if partial in ["tail", "grep", "level"]:
            return [partial]
        return []
```

- [ ] **Step 2: 提交**

```bash
git add src/mutsumi_sync/tui/commands/logs.py
git commit -m "feat: implement logs command"
```

---

## Task 6: 实现 status/stats 命令

**Files:**
- Create: `src/mutsumi_sync/tui/commands/status.py`

- [ ] **Step 1: 创建 status.py**

```python
from .base import Command
from typing import List

class StatusCommand(Command):
    name = "status"
    help = "Show system status"
    
    def __init__(self, status_provider=None):
        self._status_provider = status_provider
    
    def execute(self, args: List[str]) -> str:
        if not self._status_provider:
            return "Status not available"
        
        s = self._status_provider.get_status()
        return f"""AI: {'●' if s.get('ai_online') else '○'}
NapCat: {'●' if s.get('napcat_online') else '○'}
Messages: {s.get('message_count', 0)}
Uptime: {s.get('uptime', 'N/A')}"""

class StatsCommand(Command):
    name = "stats"
    help = "Show statistics"
    
    def __init__(self, stats_provider=None):
        self._stats_provider = stats_provider
    
    def execute(self, args: List[str]) -> str:
        if not self._stats_provider:
            return "Stats not available"
        
        stats = self._stats_provider.get_stats()
        lines = [f"Total Messages: {stats.get('total_messages', 0)}"]
        
        if "users" in stats:
            lines.append(f"Users: {len(stats['users'])}")
            for u in list(stats["users"].keys())[:5]:
                lines.append(f"  - {u}: {stats['users'][u]} messages")
        
        return "\n".join(lines)
```

- [ ] **Step 2: 提交**

```bash
git add src/mutsumi_sync/tui/commands/status.py
git commit -m "feat: implement status command"
```

---

## Task 7: 实现 system 命令

**Files:**
- Create: `src/mutsumi_sync/tui/commands/system.py`

- [ ] **Step 1: 创建 system.py**

```python
from .base import Command
from typing import List

class ReloadCommand(Command):
    name = "reload"
    help = "Reload connections"
    
    def __init__(self, reload_handler=None):
        self._reload_handler = reload_handler
    
    def execute(self, args: List[str]) -> str:
        if not self._reload_handler:
            return "Reload handler not available"
        
        try:
            self._reload_handler()
            return "Reload triggered"
        except Exception as e:
            return f"Error: {e}"

class ExitCommand(Command):
    name = "exit"
    help = "Exit the application"
    
    def execute(self, args: List[str]) -> str:
        return "__EXIT__"

class HelpCommand(Command):
    name = "help"
    help = "Show this help message"
    
    def __init__(self, registry):
        self._registry = registry
    
    def execute(self, args: List[str]) -> str:
        lines = ["Available commands:"]
        for name, cmd in self._registry.all().items():
            lines.append(f"  {name:15} - {cmd.help}")
        return "\n".join(lines)
```

- [ ] **Step 2: 提交**

```bash
git add src/mutsumi_sync/tui/commands/system.py
git commit -m "feat: implement system commands (reload/exit/help)"
```

---

## Task 8: 实现 conversations 命令

**Files:**
- Create: `src/mutsumi_sync/tui/commands/conversations.py`

- [ ] **Step 1: 创建 conversations.py**

```python
from .base import Command
from typing import List

class ConversationsCommand(Command):
    name = "conversations"
    help = "Enter conversation browser"
    
    def __init__(self, callback=None):
        self._callback = callback
    
    def execute(self, args: List[str]) -> str:
        if self._callback:
            self._callback()
            return "Entering conversation browser... (TBD)"
        return "Conversation browser not available"
```

- [ ] **Step 2: 提交**

```bash
git add src/mutsumi_sync/tui/commands/conversations.py
git commit -m "feat: implement conversations command"
```

---

## Task 9: 实现 REPL 核心

**Files:**
- Create: `src/mutsumi_sync/tui/repl.py`

- [ ] **Step 1: 创建 repl.py**

```python
from prompt_toolkit import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import VSplit, HSplit, Window
from prompt_toolkit.layout.controls import BufferControl
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import Style
from prompt_toolkit.history import FileHistory
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import Completer, Completion

import os
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
        
        self.registry = CommandRegistry()
        self._setup_commands(config_loader, status_provider, stats_provider, 
                           reload_handler, conversation_callback)
        
        self.output_lines = []
        self.status = {"ai_online": False, "napcat_online": False, 
                      "message_count": 0, "uptime": "00:00:00"}
        self._status_lock = threading.Lock()
        self._start_time = time.time()
        
        self.history = FileHistory(".mutsumi_history")
        self.buffer = Buffer(completer=CommandCompleter(self.registry),
                            history=self.history,
                            multiline=False)
        
        self._app = None
        self._build_app()
    
    def _setup_commands(self, config_loader, status_provider, stats_provider,
                       reload_handler, conversation_callback):
        self.registry.register(ConfigCommand(config_loader))
        self.registry.register(LogsCommand())
        self.registry.register(StatusCommand(status_provider))
        self.registry.register(StatsCommand(stats_provider))
        self.registry.register(ReloadCommand(reload_handler))
        self.registry.register(ExitCommand(lambda: self._exit()))
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
        if self._app:
            self._app._loop._invalidate()
    
    def update_status(self, **kwargs):
        with self._status_lock:
            self.status.update(kwargs)
    
    def run(self):
        self._app.run()
```

- [ ] **Step 2: 提交**

```bash
git add src/mutsumi_sync/tui/repl.py
git commit -m "feat: implement REPL core with prompt_toolkit"
```

---

## Task 10: 更新 start_tui.py

**Files:**
- Modify: `start_tui.py`

- [ ] **Step 1: 更新 start_tui.py**

```python
#!/usr/bin/env python3
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from mutsumi_sync.config import Config
from mutsumi_sync.tui.repl import REPL
from mutsumi_sync.tui.storage import ConversationStorage

def main():
    config = Config()
    storage = ConversationStorage()
    
    repl = REPL(
        config_loader=config,
        status_provider=None,
        stats_provider=None,
        reload_handler=None,
    )
    
    repl.run()

if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 测试运行**

Run: `python start_tui.py`

- [ ] **Step 3: 提交**

```bash
git add start_tui.py
git commit -m "refactor: update start_tui.py to use new REPL"
```

---

## Task 11: 添加测试

**Files:**
- Create: `tests/test_repl.py`

- [ ] **Step 1: 创建测试**

```python
import pytest
from mutsumi_sync.tui.commands.base import Command, CommandRegistry
from mutsumi_sync.tui.commands.config import ConfigCommand
from mutsumi_sync.tui.commands.logs import LogsCommand

class TestCommandRegistry:
    def test_register_and_get(self):
        registry = CommandRegistry()
        cmd = ConfigCommand()
        registry.register(cmd)
        assert registry.get("config") == cmd
    
    def test_all(self):
        registry = CommandRegistry()
        registry.register(ConfigCommand())
        assert "config" in registry.all()

class TestConfigCommand:
    def test_execute_no_args(self):
        cmd = ConfigCommand(config_loader=None)
        result = cmd.execute([])
        assert "Usage" in result
    
    def test_complete(self):
        cmd = ConfigCommand(config_loader=None)
        completions = cmd.complete("")
        assert "get" in completions

class TestLogsCommand:
    def test_execute_no_args(self):
        cmd = LogsCommand(log_path="/nonexistent.log")
        result = cmd.execute([])
        assert "Usage" in result
    
    def test_tail_nonexistent_file(self):
        cmd = LogsCommand(log_path="/nonexistent.log")
        result = cmd.execute(["tail"])
        assert "not found" in result.lower()
```

- [ ] **Step 2: 运行测试**

Run: `pytest tests/test_repl.py -v`

- [ ] **Step 3: 提交**

```bash
git add tests/test_repl.py
git commit -m "test: add REPL tests"
```

---

## 计划完成

总共 11 个 Task，预计完成时间约 30-45 分钟。