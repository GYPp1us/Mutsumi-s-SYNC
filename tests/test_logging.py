from __future__ import annotations

from types import SimpleNamespace

from src.mutsumi_sync.logging import log_context


def test_log_context_does_not_truncate_message_content(caplog):
    long_content = "x" * 180 + "TAIL"
    deps = SimpleNamespace(
        config=SimpleNamespace(
            model=SimpleNamespace(provider="test-provider", model="test-model")
        )
    )

    with caplog.at_level("INFO", logger="mutsumi.logging"):
        log_context([{"role": "user", "content": long_content}], deps)

    logged = "\n".join(record.message for record in caplog.records)
    assert long_content in logged
    assert "..." not in logged
