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