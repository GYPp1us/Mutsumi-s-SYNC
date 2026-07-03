from __future__ import annotations

from src.mutsumi_sync.pipeline import _is_placeholder_summary


def test_shutdown_archive_marker_is_placeholder_summary():
    assert _is_placeholder_summary("[会话结束] 4 messages archived on shutdown")
    assert _is_placeholder_summary("[會話結束] 2 messages archived on shutdown")


def test_real_summary_is_not_placeholder_summary():
    assert not _is_placeholder_summary("用户最近在调试 dashboard 日志窗口。")
    assert not _is_placeholder_summary("4 messages about dashboard were summarized.")
