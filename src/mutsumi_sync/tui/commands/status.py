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