from textual.widget import Widget
from textual.widgets import Static
from ..storage import Conversation, Round


class ConversationView(Widget):
    def __init__(self, conversation: Conversation = None, round_idx: int = 0):
        super().__init__()
        self.conversation = conversation
        self.round_idx = round_idx

    def compose(self):
        if not self.conversation:
            yield Static("暂无对话")
            return

        conv = self.conversation
        if not conv.rounds:
            yield Static(f"用户 {conv.peer_id} 暂无对话记录")
            return

        if self.round_idx >= len(conv.rounds):
            self.round_idx = 0

        round = conv.rounds[self.round_idx]
        content = f"""第 {self.round_idx + 1}/{len(conv.rounds)} 轮对话

{round.timestamp} 用户: {round.user_message}

{round.timestamp} Bot: {round.bot_message}"""

        yield Static(content, id="conversation")

    def update(self, conversation: Conversation, round_idx: int):
        self.conversation = conversation
        self.round_idx = round_idx
        self.query_one("#conversation", Static).update(self._build_content())

    def _build_content(self) -> str:
        if not self.conversation or not self.conversation.rounds:
            return "暂无对话"

        round = self.conversation.rounds[self.round_idx]
        return f"""第 {self.round_idx + 1}/{len(self.conversation.rounds)} 轮对话

{round.timestamp} 用户: {round.user_message}

{round.timestamp} Bot: {round.bot_message}"""