import json
import logging
from types import SimpleNamespace

from src.mutsumi_sync.config import Config
from src.mutsumi_sync.logging import log_context, stop_stream_log_store
from src.mutsumi_sync.main import setup_logging


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


def test_stream_log_store_writes_records_as_ndjson(tmp_path):
    config = Config()
    config.logging.stream_store.enabled = True
    config.logging.stream_store.path = str(tmp_path / "mutsumi.ndjson")

    setup_logging(config=config)
    logging.getLogger("mutsumi.test").info("hello\n\033[31mred\033[0m")
    stop_stream_log_store()

    lines = (tmp_path / "mutsumi.ndjson").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1

    row = json.loads(lines[0])
    assert row["level"] == "INFO"
    assert row["logger"] == "mutsumi.test"
    assert row["message"] == "hello\n\033[31mred\033[0m"
    assert row["ansi"] is True
    assert row["schema"] == "mutsumi.log.v1"


def test_stream_log_store_can_strip_ansi(tmp_path):
    config = Config()
    config.logging.stream_store.enabled = True
    config.logging.stream_store.path = str(tmp_path / "mutsumi.ndjson")
    config.logging.stream_store.keep_ansi = False

    setup_logging(config=config)
    logging.getLogger("mutsumi.test").warning("\033[33mplain\033[0m")
    stop_stream_log_store()

    row = json.loads((tmp_path / "mutsumi.ndjson").read_text(encoding="utf-8"))
    assert row["message"] == "plain"
    assert row["ansi"] is False


def test_stream_log_store_disabled_does_not_create_file(tmp_path):
    config = Config()
    config.logging.stream_store.enabled = False
    config.logging.stream_store.path = str(tmp_path / "mutsumi.ndjson")

    setup_logging(config=config)
    logging.getLogger("mutsumi.test").info("not stored")
    stop_stream_log_store()

    assert not (tmp_path / "mutsumi.ndjson").exists()
