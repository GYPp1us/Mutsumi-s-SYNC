from textual.app import App, ComposeResult
from textual.widgets import Static, Footer
from textual.containers import Container, Horizontal, Vertical
from textual.binding import Binding
from .storage import ConversationStorage
from .widgets.status_bar import StatusBar
from .widgets.conversation import ConversationView

class MutsumiTUI(App):
    CSS = """
    Screen { height: 100%; }
    # header { height: 3; background: $primary; }
    # content { height: 1fr; }
    # status { height: 3; background: $surface; }
    """
    
    BINDINGS = [
        Binding("left", "prev_user", "上一用户"),
        Binding("right", "next_user", "下一用户"),
        Binding("up", "prev_round", "上一轮"),
        Binding("down", "next_round", "下一轮"),
        Binding("enter", "show_detail", "详情"),
        Binding("escape", "back", "返回"),
    ]
    
    def __init__(self, storage: ConversationStorage):
        super().__init__()
        self.storage = storage
        self.current_user_idx = 0
        self.current_round_idx = 0
    
    def compose(self) -> ComposeResult:
        yield Static("Mutsumi's SYNC", id="header")
        with Vertical(id="content"):
            yield ConversationView(id="conversation")
        yield StatusBar(id="status")
    
    def action_next_user(self):
        convs = self.storage.get_conversations()
        if convs:
            self.current_user_idx = (self.current_user_idx + 1) % len(convs)
            self.current_round_idx = 0
            self.refresh_content()
    
    def action_prev_user(self):
        convs = self.storage.get_conversations()
        if convs:
            self.current_user_idx = (self.current_user_idx - 1) % len(convs)
            self.current_round_idx = 0
            self.refresh_content()
    
    def action_next_round(self):
        convs = self.storage.get_conversations()
        if convs and convs[self.current_user_idx].rounds:
            max_idx = len(convs[self.current_user_idx].rounds) - 1
            self.current_round_idx = min(self.current_round_idx + 1, max_idx)
            self.refresh_content()
    
    def action_prev_round(self):
        self.current_round_idx = max(self.current_round_idx - 1, 0)
        self.refresh_content()
    
    def refresh_content(self):
        convs = self.storage.get_conversations()
        if convs:
            conv = convs[self.current_user_idx]
            self.query_one("#conversation", ConversationView).update(conv, self.current_round_idx)
    
    def update_status(self, ai_online: bool, napcat_online: bool, msg_count: int):
        self.query_one("#status", StatusBar).update(ai_online, napcat_online, msg_count)
    
    def action_show_detail(self):
        pass
    
    def action_back(self):
        pass