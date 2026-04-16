#!/usr/bin/env python3
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from mutsumi_sync.config import Config
from mutsumi_sync.tui.repl import REPL
from mutsumi_sync.tui.storage import ConversationStorage

def main():
    config = Config()
    storage = ConversationStorage()
    
    repl = REPL(
        config_loader=config,
        status_provider=None,
        stats_provider=None,
        reload_handler=None,
    )
    
    repl.run()

if __name__ == "__main__":
    main()