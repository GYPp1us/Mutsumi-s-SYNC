from src.mutsumi_sync.tui.commands.base import Command, CommandRegistry
from src.mutsumi_sync.tui.commands.config import ConfigCommand
from src.mutsumi_sync.tui.commands.logs import LogsCommand

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