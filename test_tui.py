import sys
sys.path.insert(0, ".")
from src.mutsumi_sync.tui.app import MutsumiTUI
from src.mutsumi_sync.tui.storage import ConversationStorage
s = ConversationStorage()
t = MutsumiTUI(s)
print("TUI created OK")
