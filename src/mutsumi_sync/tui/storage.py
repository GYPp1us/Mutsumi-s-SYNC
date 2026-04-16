from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime

@dataclass
class ToolCall:
    name: str
    args: dict
    result: str = ""

@dataclass
class Round:
    timestamp: str
    user_message: str
    bot_message: str
    tool_calls: list[ToolCall] = field(default_factory=list)

@dataclass
class Conversation:
    peer_id: str
    peer_name: str = ""
    chat_type: int = 1
    rounds: list[Round] = field(default_factory=list)
    last_active: str = ""

class ConversationStorage:
    def __init__(self):
        self.conversations: dict[str, Conversation] = {}
        self.message_count: int = 0
    
    def add_message(self, peer_id: str, peer_name: str, chat_type: int, 
                    user_msg: str, bot_msg: str, tool_calls: list[ToolCall] = None):
        if peer_id not in self.conversations:
            self.conversations[peer_id] = Conversation(
                peer_id=peer_id,
                peer_name=peer_name,
                chat_type=chat_type
            )
        conv = self.conversations[peer_id]
        round = Round(
            timestamp=datetime.now().strftime("%H:%M:%S"),
            user_message=user_msg,
            bot_message=bot_msg,
            tool_calls=tool_calls or []
        )
        conv.rounds.append(round)
        conv.last_active = round.timestamp
        self.message_count += 1
    
    def get_conversations(self) -> list[Conversation]:
        return list(self.conversations.values())
    
    def get_conversation(self, peer_id: str) -> Optional[Conversation]:
        return self.conversations.get(peer_id)