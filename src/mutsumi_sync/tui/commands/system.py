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