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