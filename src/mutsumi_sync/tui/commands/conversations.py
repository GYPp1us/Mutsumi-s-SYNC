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