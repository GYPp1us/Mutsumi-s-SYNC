from textual.widget import Widget
from textual.widgets import Static


class StatusBar(Widget):
    def __init__(self, ai_online: bool = False, napcat_online: bool = False, message_count: int = 0):
        super().__init__()
        self.ai_online = ai_online
        self.napcat_online = napcat_online
        self.message_count = message_count

    def compose(self):
        yield Static(self._get_status_text(), id="status")

    def _get_status_text(self) -> str:
        ai = "●" if self.ai_online else "○"
        nc = "●" if self.napcat_online else "○"
        return f"[AI: {ai}] [NapCat: {nc}] [消息: {self.message_count}]  [←→切换用户 ↑↓切换轮次 Enter详情]"

    def update(self, ai_online: bool, napcat_online: bool, message_count: int):
        self.ai_online = ai_online
        self.napcat_online = napcat_online
        self.message_count = message_count
        self.query_one("#status", Static).update(self._get_status_text())