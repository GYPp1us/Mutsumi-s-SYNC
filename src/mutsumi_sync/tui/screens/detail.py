from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Static, Button
from textual.containers import Vertical, Horizontal
from ..storage import Conversation, Round

class DetailScreen(Screen):
    def __init__(self, conversation: Conversation, round_idx: int):
        super().__init__()
        self.conversation = conversation
        self.round_idx = round_idx
    
    def compose(self) -> ComposeResult:
        round = self.conversation.rounds[self.round_idx]
        
        yield Vertical(
            Static(f"对话详情 - 用户 {self.conversation.peer_id} 第 {self.round_idx + 1} 轮", id="detail_header"),
            Static(f"用户消息: {round.user_message}", id="user_msg"),
            Static(f"Bot 回复: {round.bot_message}", id="bot_msg"),
            id="detail_content"
        )
        
        if round.tool_calls:
            tool_info = "--- Tool Calls ---\n"
            for i, tc in enumerate(round.tool_calls, 1):
                tool_info += f"[{i}] {tc.name}\n"
                for k, v in tc.args.items():
                    tool_info += f"    {k}: {v}\n"
                if tc.result:
                    tool_info += f"    结果: {tc.result}\n"
            yield Static(tool_info, id="tool_calls")
        
        yield Horizontal(
            Button("返回", id="back_btn"),
            id="detail_footer"
        )
    
    def on_button_pressed(self, event: Button.Pressed):
        if event.button.id == "back_btn":
            self.app.pop_screen()