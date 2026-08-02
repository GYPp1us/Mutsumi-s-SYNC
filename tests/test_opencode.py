from __future__ import annotations

import asyncio
import json

from src.mutsumi_sync.config import Config
from src.mutsumi_sync.memory.store import MessageStore
from src.mutsumi_sync.opencode import (
    OpenCodeRunner,
    REPORT_PLUGIN_SOURCE,
    _render_report_plugin,
)


def test_report_plugin_is_atomic_and_exported_for_opencode() -> None:
    assert "@opencode-ai/plugin" in REPORT_PLUGIN_SOURCE
    assert "await access(path)" in REPORT_PLUGIN_SOURCE
    assert "await link(temporary, path)" in REPORT_PLUGIN_SOURCE
    assert "randomUUID" in REPORT_PLUGIN_SOURCE
    assert "${path}.tmp" not in REPORT_PLUGIN_SOURCE
    assert "export default MutsumiReportPlugin" in REPORT_PLUGIN_SOURCE
    rendered = _render_report_plugin("file:///opt/opencode/plugin/index.js")
    assert 'from "file:///opt/opencode/plugin/index.js"' in rendered
    assert '@opencode-ai/plugin"' not in rendered.splitlines()[0]


def test_runner_builds_provider_config_for_isolated_run(tmp_path) -> None:
    config = Config()
    config.model.provider = "deepseek"
    config.model.model = "deepseek-chat"
    config.model.base_url = "https://example.test/v1"
    config.model.api_key = "secret-key"
    runner = OpenCodeRunner(config, None, lambda *_: _noop())

    payload = json.loads(runner._config_content(tmp_path / "report.mjs"))

    assert payload["model"] == "deepseek/deepseek-chat"
    assert payload["plugin"] == [str(tmp_path / "report.mjs")]
    provider = payload["provider"]["deepseek"]
    assert provider["options"] == {
        "baseURL": "https://example.test/v1",
        "apiKey": "secret-key",
    }
    assert provider["npm"] == "@ai-sdk/openai-compatible"
    assert provider["models"]["deepseek-chat"]["name"] == "deepseek-chat"


async def test_delegated_report_is_token_bound_and_idempotent(tmp_path) -> None:
    store = MessageStore(str(tmp_path / "tasks.db"), str(tmp_path / "media"))
    await store.initialize()
    try:
        await store.create_delegated_task(
            run_id="oc_test",
            conversation_id="private:alice",
            prompt="do work",
            workspace=str(tmp_path),
            report_token="token",
        )
        assert await store.accept_delegated_report("oc_test", "wrong", "bad") is False
        assert await store.accept_delegated_report("oc_test", "token", "done") is True
        assert await store.accept_delegated_report("oc_test", "token", "duplicate") is False
        record = await store.get_delegated_task("oc_test")
        assert record is not None
        assert record.status == "reported"
        assert record.report == "done"
    finally:
        await store.close()


async def test_empty_delegated_report_is_not_accepted(tmp_path) -> None:
    store = MessageStore(str(tmp_path / "empty-report.db"), str(tmp_path / "media"))
    await store.initialize()
    try:
        await store.create_delegated_task(
            run_id="oc_empty",
            conversation_id="private:alice",
            prompt="do work",
            workspace=str(tmp_path),
            report_token="token",
        )
        assert await store.accept_delegated_report("oc_empty", "token", "  ") is False
        record = await store.get_delegated_task("oc_empty")
        assert record is not None and record.status == "pending" and record.report == ""
    finally:
        await store.close()


async def test_runner_uses_json_events_but_requires_explicit_report(monkeypatch, tmp_path) -> None:
    store = MessageStore(str(tmp_path / "tasks.db"), str(tmp_path / "media"))
    await store.initialize()
    config = Config()
    config.opencode.enabled = True
    config.opencode.executable = "python"
    config.opencode.workspace = str(tmp_path)
    config.opencode.run_root = str(tmp_path / "runs")
    reports: list[tuple[str, str]] = []
    commands: list[list[str]] = []
    environments: list[dict[str, str]] = []

    class Stream:
        async def readline(self):
            if getattr(self, "sent", False):
                return b""
            self.sent = True
            return b'{"type":"step"}\n'

    class Process:
        returncode = 0
        stdout = Stream()
        stderr = Stream()

        async def wait(self):
            return 0

        def terminate(self):
            self.returncode = -15

        def kill(self):
            self.returncode = -9

    async def fake_exec(*command, **kwargs):
        commands.append(list(command))
        env = kwargs["env"]
        environments.append(env)
        with open(env["MUTSUMI_REPORT_PATH"], "w", encoding="utf-8") as handle:
            json.dump({"token": env["MUTSUMI_REPORT_TOKEN"], "report": "finished"}, handle)
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    runner = OpenCodeRunner(config, store, lambda run_id, report: _collect(reports, run_id, report))
    try:
        run_id = await runner.start(prompt="read the skill", conversation_id="private:alice")
        while runner._tasks:
            await asyncio.sleep(0.05)
        assert reports == [(run_id, "finished")]
        assert commands[0][1:4] == ["run", "--format", "json"]
        assert "必须调用 report_task" in commands[0][-1]
        assert environments[0]["MUTSUMI_REPORT_TOKEN"]
        assert environments[0]["MUTSUMI_REPORT_PATH"]
        assert environments[0]["OPENCODE_CONFIG_CONTENT"]
        assert environments[0]["OPENCODE_DB"].endswith("opencode.db")
        assert environments[0]["XDG_DATA_HOME"].endswith("xdg-data")
        assert environments[0]["XDG_CONFIG_HOME"].endswith("xdg-config")
        assert environments[0]["OPENCODE_DISABLE_PROJECT_CONFIG"] == "1"
        record = await store.get_delegated_task(run_id)
        assert record is not None
        assert record.status == "reported"
    finally:
        await runner.shutdown()
        await store.close()


async def test_runner_stops_worker_after_explicit_report(monkeypatch, tmp_path) -> None:
    store = MessageStore(str(tmp_path / "stop-after-report.db"), str(tmp_path / "media"))
    await store.initialize()
    config = Config()
    config.opencode.enabled = True
    config.opencode.executable = "python"
    config.opencode.workspace = str(tmp_path)
    config.opencode.run_root = str(tmp_path / "runs")
    processes = []

    class Stream:
        async def readline(self):
            return b""

    class Process:
        def __init__(self):
            self.returncode = None
            self.terminated = False
            self.stdout = Stream()
            self.stderr = Stream()

        async def wait(self):
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = 0

        def kill(self):
            self.returncode = -9

    async def fake_exec(*command, **kwargs):
        process = Process()
        processes.append(process)
        with open(kwargs["env"]["MUTSUMI_REPORT_PATH"], "w", encoding="utf-8") as handle:
            json.dump({
                "token": kwargs["env"]["MUTSUMI_REPORT_TOKEN"],
                "report": "finished",
            }, handle)
        assert kwargs["stdin"] is asyncio.subprocess.DEVNULL
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    runner = OpenCodeRunner(config, store, lambda *_: _noop())
    try:
        run_id = await runner.start(prompt="report and stop", conversation_id="private:alice")
        while runner._tasks:
            await asyncio.sleep(0.05)
        assert processes and processes[0].terminated is True
        record = await store.get_delegated_task(run_id)
        assert record is not None and record.status == "reported"
    finally:
        await runner.shutdown()
        await store.close()


async def test_runner_marks_process_without_report_unreported(monkeypatch, tmp_path) -> None:
    store = MessageStore(str(tmp_path / "tasks.db"), str(tmp_path / "media"))
    await store.initialize()
    config = Config()
    config.opencode.enabled = True
    config.opencode.executable = "python"
    config.opencode.workspace = str(tmp_path)
    config.opencode.run_root = str(tmp_path / "runs")

    class Stream:
        async def readline(self):
            return b""

    class Process:
        returncode = 0
        stdout = Stream()
        stderr = Stream()

        async def wait(self):
            return 0

        def terminate(self):
            self.returncode = -15

        def kill(self):
            self.returncode = -9

    async def fake_exec(*command, **kwargs):
        return Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    runner = OpenCodeRunner(config, store, lambda *_: _noop())
    try:
        run_id = await runner.start(prompt="do not report", conversation_id="private:alice")
        while runner._tasks:
            await asyncio.sleep(0.05)
        record = await store.get_delegated_task(run_id)
        assert record is not None
        assert record.status == "unreported"
        assert not (tmp_path / "runs" / run_id).exists()
    finally:
        await runner.shutdown()
        await store.close()


async def test_runner_shutdown_marks_active_worker_unreported(monkeypatch, tmp_path) -> None:
    store = MessageStore(str(tmp_path / "shutdown.db"), str(tmp_path / "media"))
    await store.initialize()
    config = Config()
    config.opencode.enabled = True
    config.opencode.executable = "python"
    config.opencode.workspace = str(tmp_path)
    config.opencode.run_root = str(tmp_path / "runs")

    class Stream:
        async def readline(self):
            await asyncio.sleep(10)
            return b""

    class Process:
        def __init__(self):
            self.returncode = None
            self.stdout = Stream()
            self.stderr = Stream()

        async def wait(self):
            return self.returncode

        def terminate(self):
            self.returncode = -15

        def kill(self):
            self.returncode = -9

    process = Process()

    async def fake_exec(*command, **kwargs):
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    runner = OpenCodeRunner(config, store, lambda *_: _noop())
    try:
        run_id = await runner.start(prompt="long worker", conversation_id="private:alice")
        await asyncio.sleep(0.05)
        await runner.shutdown()
        record = await store.get_delegated_task(run_id)
        assert record is not None
        assert record.status == "unreported"
    finally:
        await store.close()


async def test_runner_accepts_report_published_just_after_process_exit(monkeypatch, tmp_path) -> None:
    store = MessageStore(str(tmp_path / "tasks.db"), str(tmp_path / "media"))
    await store.initialize()
    config = Config()
    config.opencode.enabled = True
    config.opencode.executable = "python"
    config.opencode.workspace = str(tmp_path)
    config.opencode.run_root = str(tmp_path / "runs")
    reports: list[str] = []

    class Stream:
        async def readline(self):
            return b""

    class Process:
        returncode = 0

        def __init__(self):
            self.stdout = Stream()
            self.stderr = Stream()

        async def wait(self):
            return 0

        def terminate(self):
            self.returncode = -15

        def kill(self):
            self.returncode = -9

    async def fake_exec(*command, **kwargs):
        process = Process()

        async def publish_late() -> None:
            await asyncio.sleep(0.02)
            with open(kwargs["env"]["MUTSUMI_REPORT_PATH"], "w", encoding="utf-8") as handle:
                json.dump({
                    "token": kwargs["env"]["MUTSUMI_REPORT_TOKEN"],
                    "report": "late report",
                }, handle)

        asyncio.create_task(publish_late())
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    runner = OpenCodeRunner(
        config,
        store,
        lambda _run_id, report: _collect_report(reports, report),
    )
    try:
        run_id = await runner.start(prompt="late report", conversation_id="private:alice")
        while runner._tasks:
            await asyncio.sleep(0.05)
        assert reports == ["late report"]
        record = await store.get_delegated_task(run_id)
        assert record is not None and record.status == "reported"
    finally:
        await runner.shutdown()
        await store.close()


async def _collect(target: list[tuple[str, str]], run_id: str, report: str) -> None:
    target.append((run_id, report))


async def _collect_report(target: list[str], report: str) -> None:
    target.append(report)


async def _noop() -> None:
    return None
