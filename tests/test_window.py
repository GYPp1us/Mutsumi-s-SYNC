import pytest
from src.mutsumi_sync.memory.window import MessageWindow


class TestMessageWindow:
    def test_add_and_context(self):
        w = MessageWindow(max_size=5)
        w.add(user_id="user1", message="hello")
        w.add(user_id="user1", message="world", is_bot=True)
        ctx = w.get_context()
        assert len(ctx) == 2
        assert ctx[0] == {"role": "user", "content": "hello"}
        assert ctx[1] == {"role": "assistant", "content": "world"}

    def test_max_size_overflow(self):
        w = MessageWindow(max_size=2)
        w.add(user_id="u", message="a")
        w.add(user_id="u", message="b")
        w.add(user_id="u", message="c")
        assert len(w) == 2
        ctx = w.get_context()
        assert ctx[0]["content"] == "b"
        assert ctx[1]["content"] == "c"

    def test_clear(self):
        w = MessageWindow(max_size=10)
        w.add(user_id="u", message="test")
        assert len(w) == 1
        w.clear()
        assert len(w) == 0
        assert w.get_context() == []

    def test_empty_window(self):
        w = MessageWindow()
        assert w.get_context() == []
        assert len(w) == 0

    def test_role_label(self):
        w = MessageWindow()
        w.add(user_id="bot", message="reply", is_bot=True)
        ctx = w.get_context()
        assert ctx[0]["role"] == "assistant"
