from .base import Command
from typing import List

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