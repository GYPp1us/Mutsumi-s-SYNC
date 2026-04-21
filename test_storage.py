#!/usr/bin/env python3
import sys
import curses
sys.path.insert(0, ".")

from src.mutsumi_sync.tui.storage import ConversationStorage, ToolCall

s = ConversationStorage()
s.add_message("123", "user1", 1, "hello", "hi there", None)
s.add_message("123", "user1", 1, "how are you?", "fine", None)
s.add_message("456", "user2", 1, "hi", "hello!", None)

print("Storage created with", len(s.get_conversations()), "conversations")
print("Messages:", s.message_count)

for conv in s.get_conversations():
    print(f"User: {conv.peer_id}, Rounds: {len(conv.rounds)}")
    for r in conv.rounds:
        print(f"  {r.timestamp}: {r.user_message} -> {r.bot_message}")
