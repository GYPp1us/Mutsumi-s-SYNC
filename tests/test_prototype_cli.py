from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from src.mutsumi_sync.config import Config
from src.mutsumi_sync.prototype.__main__ import (
    DemoModel,
    bind_database,
    build_parser,
    run_demo,
    validate_database,
)
from src.mutsumi_sync.prototype.adapters import ConsoleSender, build_worker_registry, load_prototype_prompts
from src.mutsumi_sync.prototype.runtime import PrototypeScheduler, RuntimeSettings
from src.mutsumi_sync.prototype.storage import PrototypeStore


def test_cli_defaults_to_offline_and_separate_database() -> None:
    args = build_parser().parse_args([])
    assert not args.live_model
    assert not args.interactive
    assert args.config == "config.example.yaml"
    assert args.db == "data/prototype/prototype.sqlite3"


def test_cli_refuses_nonprototype_database_without_changes(tmp_path: Path) -> None:
    database = tmp_path / "production.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE events (content TEXT)")
        connection.execute("INSERT INTO events VALUES ('existing user data')")
    before = database.read_bytes()
    with pytest.raises(ValueError, match="non-prototype"):
        validate_database(database, tmp_path, "offline")
    assert database.read_bytes() == before


def test_cli_binds_restore_to_project_root_and_model_mode(tmp_path: Path) -> None:
    database = tmp_path / "prototype.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE prototype_jobs (id TEXT)")
    bind_database(database, tmp_path, "offline")
    validate_database(database, tmp_path, "offline")
    with pytest.raises(ValueError, match="different --root or model mode"):
        validate_database(database, tmp_path / "another-project", "offline")
    with pytest.raises(ValueError, match="different --root or model mode"):
        validate_database(database, tmp_path, "live")


def test_cli_refuses_unbound_old_tasks(tmp_path: Path) -> None:
    database = tmp_path / "prototype.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE prototype_jobs (id TEXT)")
        connection.execute("INSERT INTO prototype_jobs VALUES ('old-task')")
    with pytest.raises(ValueError, match="older tasks"):
        validate_database(database, tmp_path, "offline")


@pytest.mark.asyncio
async def test_offline_demo_exercises_waiting_work_and_message_choices(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("Mutsumi is an asynchronous NapCat QQ chatbot.", encoding="utf-8")
    store = PrototypeStore(str(tmp_path / "demo.sqlite3"), media_dir=str(tmp_path / "media"))
    await store.initialize()
    sender = ConsoleSender(echo=False)
    model = DemoModel(hold_worker=True)
    scheduler = PrototypeScheduler(
        store, model, sender,
        worker_tools=build_worker_registry(tmp_path), prompts=load_prototype_prompts(Config()),
        settings=RuntimeSettings(wait_seconds=0.1, chat_timeout=3),
    )
    try:
        await scheduler.start()
        await run_demo(scheduler, model, sender)
        assert model.worker_release.is_set()
        assert model.slow_calls == 2
        assert len(sender.sent) == 8
    finally:
        await scheduler.close()
        await store.close()
