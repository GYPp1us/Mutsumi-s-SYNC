#!/usr/bin/env python3
"""Mutsumi's SYNC - 窗口化 TUI"""
import asyncio
import logging
import sys
import threading
from pathlib import Path
from datetime import datetime
import re

sys.path.insert(0, str(Path(__file__).parent))

from src.mutsumi_sync.config import load_config
from src.mutsumi_sync.bot import MutsumiSync
from src.mutsumi_sync.tui.app import MutsumiTUI
from src.mutsumi_sync.tui.storage import ConversationStorage, ToolCall


Path("./logs").mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler('./logs/mutsumi.log'),
        logging.NullHandler()
    ]
)


class BotWithTUI:
    def __init__(self, config, tui_app: MutsumiTUI):
        self.config = config
        self.tui = tui_app
        self.bot = MutsumiSync(config, log_callback=self.on_bot_message)
        self._running = False
    
    async def run(self):
        self._running = True
        
        bot_thread = threading.Thread(target=lambda: asyncio.run(self.bot.run()), daemon=True)
        bot_thread.start()
        
        self.tui.run()
        self._running = False
    
    def on_bot_message(self, msg: str):
        self._parse_and_update(msg)
    
    def _parse_and_update(self, msg: str):
        try:
            peer_id = None
            user_msg = None
            bot_msg = None
            
            msg_type_match = re.search(r'\[MSG IN\]', msg)
            send_match = re.search(r'\[SEND\].*?chat_type:(\d+) peer:(\S+)', msg)
            
            if msg_type_match:
                user_match = re.search(r'From user:(\S+) msg:(.+)', msg)
                if user_match:
                    peer_id = user_match.group(1)
                    user_msg = user_match.group(2).strip()
            
            if send_match:
                if peer_id is None:
                    peer_id = send_match.group(2)
                bot_match = re.search(r'Response: (.+)', msg)
                if bot_match:
                    bot_msg = bot_match.group(1).strip()
            
            if peer_id and user_msg:
                self.tui.storage.add_message(
                    peer_id=peer_id,
                    peer_name=peer_id,
                    chat_type=1,
                    user_msg=user_msg,
                    bot_msg=bot_msg or "",
                    tool_calls=None
                )
                if self.tui.is_running:
                    self.tui.call_later(self.tui.refresh_content)
        except Exception as e:
            logging.error(f"Failed to parse bot message: {e}")


async def main():
    config = load_config()
    storage = ConversationStorage()
    tui_app = MutsumiTUI(storage)
    
    bot_with_tui = BotWithTUI(config, tui_app)
    await bot_with_tui.run()


if __name__ == "__main__":
    asyncio.run(main())