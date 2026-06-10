import time
import pytest
from src.mutsumi_sync.memory.session import SessionState


class TestSessionState:
    def test_defaults(self):
        s = SessionState()
        assert s.is_pending is False
        assert isinstance(s.last_active, float)

    def test_touch(self):
        s = SessionState(last_active=0.0)
        s.touch()
        assert s.last_active > 0.0

    def test_is_cold_true(self):
        s = SessionState(last_active=0.0)
        assert s.is_cold(5.0) is True

    def test_is_cold_false(self):
        s = SessionState()
        assert s.is_cold(99999) is False

    def test_pending(self):
        s = SessionState()
        s.mark_pending()
        assert s.is_pending is True
        s.clear_pending()
        assert s.is_pending is False
