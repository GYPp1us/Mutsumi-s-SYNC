from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import secrets
import shutil
import time
import uuid
from typing import Awaitable, Callable

from .config import OpenCodeConfig, resolve_runtime_path
from .memory.store import MessageStore

logger = logging.getLogger("mutsumi.opencode")

REPORT_INSTRUCTION = (
    "\n\n[平台回报协议]\n"
    "任务完成后必须调用 report_task 工具，并在 report 参数中写入简洁的自然语言结果。"
    "不要把 stdout、退出状态或内部过程当作完成回报。"
)


REPORT_PLUGIN_SOURCE = r'''import { tool } from "@opencode-ai/plugin";
import { access, link, unlink, writeFile } from "node:fs/promises";
import { randomUUID } from "node:crypto";

export const MutsumiReportPlugin = async () => ({
  tool: {
    report_task: tool({
      description: "Report the completed task to the bound Mutsumi's SYNC bot.",
      args: {
        report: tool.schema.string(),
      },
      async execute(args) {
        const path = process.env.MUTSUMI_REPORT_PATH;
        const token = process.env.MUTSUMI_REPORT_TOKEN;
        if (!path || !token) throw new Error("Mutsumi report channel is unavailable");
        try {
          await access(path);
          return "Task report was already accepted by Mutsumi's SYNC.";
        } catch {}
        // Write privately, then create the final path exclusively. Two tool
        // calls racing to report the same run must not overwrite one another.
        const temporary = `${path}.${randomUUID()}.tmp`;
        await writeFile(
          temporary,
          JSON.stringify({ token, report: args.report }),
          { encoding: "utf8", flag: "wx" },
        );
        try {
          await link(temporary, path);
          return "Task report accepted by Mutsumi's SYNC.";
        } catch (error) {
          if (error?.code === "EEXIST") {
            return "Task report was already accepted by Mutsumi's SYNC.";
          }
          throw error;
        } finally {
          await unlink(temporary).catch(() => {});
        }
      },
    }),
  },
});

export default MutsumiReportPlugin;
'''


ReportCallback = Callable[[str, str], Awaitable[None]]


def _render_report_plugin(sdk_import: str) -> str:
    """Render the plugin with a resolvable SDK import for this installation."""
    return REPORT_PLUGIN_SOURCE.replace(
        '"@opencode-ai/plugin"',
        json.dumps(sdk_import, ensure_ascii=False),
        1,
    )


def _runtime_path(config, value: str) -> Path:
    return resolve_runtime_path(value, getattr(config, "_config_path", None))


class OpenCodeRunner:
    """Run OpenCode headlessly and accept only its explicit report tool result."""

    def __init__(self, config, store: MessageStore, on_report: ReportCallback):
        self.config = config
        self.store = store
        self.on_report = on_report
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._processes: dict[str, asyncio.subprocess.Process] = {}
        self._shutting_down = False

    def _executable(self) -> str:
        configured = str(self.config.opencode.executable or "opencode")
        configured_path = Path(configured)

        def prefer_native_launcher(path: str) -> str:
            """Avoid handing a Windows shell shim to create_subprocess_exec."""
            if os.name != "nt":
                return path
            candidate = Path(path)
            if candidate.suffix.lower() in {".cmd", ".exe", ".bat"}:
                return path
            for suffix in (".cmd", ".exe", ".bat"):
                native = candidate.with_suffix(suffix)
                if native.is_file():
                    return str(native)
            return path

        if configured_path.is_absolute() or "/" in configured or "\\" in configured:
            if configured_path.suffix.lower() == ".ps1":
                for suffix in (".cmd", ".exe", ""):
                    sibling = configured_path.with_suffix(suffix) if suffix else configured_path.with_suffix("")
                    if sibling.is_file():
                        return str(sibling)
            if configured_path.is_file():
                return prefer_native_launcher(configured)

        resolved = shutil.which(configured)
        if resolved:
            return prefer_native_launcher(resolved)
        if os.name == "nt":
            for launcher in (f"{configured}.cmd", f"{configured}.exe", f"{configured}.bat"):
                resolved = shutil.which(launcher)
                if resolved:
                    return resolved
        for candidate in (
            Path.home() / ".opencode" / "bin" / configured,
            Path.home() / ".opencode" / "bin" / f"{configured}.exe",
            Path.home() / ".local" / "share" / "opencode" / "bin" / configured,
            Path.home() / ".local" / "share" / "opencode" / "bin" / f"{configured}.exe",
        ):
            if candidate.is_file():
                return str(candidate)
        return configured

    @staticmethod
    def _plugin_sdk_import(executable: str) -> str:
        """Prefer the SDK shipped beside the detected OpenCode binary."""
        executable_path = Path(executable)
        candidates: list[Path] = []
        if executable_path.is_file():
            root = executable_path.resolve().parent.parent
            candidates.append(
                root / "node_modules" / "@opencode-ai" / "plugin" / "dist" / "index.js"
            )
        # The Linux installer keeps the CLI binary and its project-level
        # plugin dependency in different roots.  The child process uses an
        # isolated XDG config directory, so a bare package import would no
        # longer find this operator-installed SDK.
        home = Path.home()
        candidates.extend((
            home / ".config" / "opencode" / "node_modules" / "@opencode-ai" / "plugin" / "dist" / "index.js",
            home / ".local" / "share" / "opencode" / "node_modules" / "@opencode-ai" / "plugin" / "dist" / "index.js",
            home / ".opencode" / "node_modules" / "@opencode-ai" / "plugin" / "dist" / "index.js",
        ))
        for candidate in candidates:
            if candidate.is_file():
                return candidate.resolve().as_uri()
        return "@opencode-ai/plugin"

    def _model_reference(self) -> str:
        provider = str(self.config.model.provider or "openai").strip()
        model = str(self.config.model.model or "").strip()
        if not provider or not model:
            raise ValueError("model.provider and model.model are required for OpenCode")
        return f"{provider}/{model}"

    def _config_content(self, plugin_path: Path) -> str:
        """Build a self-contained OpenCode config for one isolated run."""
        provider = str(self.config.model.provider or "openai").strip()
        model = str(self.config.model.model or "").strip()
        if not provider or not model:
            raise ValueError("model.provider and model.model are required for OpenCode")

        options: dict[str, object] = {}
        base_url = str(self.config.model.base_url or "").strip()
        api_key = str(self.config.model.api_key or "").strip()
        if base_url:
            options["baseURL"] = base_url
        if api_key:
            options["apiKey"] = api_key
        provider_config: dict[str, object] = {
            # Mutsumi's model settings use OpenAI-compatible chat completions.
            # OpenCode's built-in `openai` provider defaults to Responses API.
            "npm": "@ai-sdk/openai-compatible",
            "models": {
                model: {
                    "name": model,
                    "limit": {
                        "context": int(self.config.context.model_context_tokens),
                        "output": int(self.config.context.reserved_output_tokens),
                    },
                }
            }
        }
        if options:
            provider_config["options"] = options
        return json.dumps(
            {
                "model": self._model_reference(),
                "plugin": [str(plugin_path)],
                "provider": {provider: provider_config},
            },
            ensure_ascii=False,
        )

    async def start(self, *, prompt: str, conversation_id: str) -> str:
        if not self.config.opencode.enabled:
            raise RuntimeError("OpenCode integration is disabled")
        cleaned_prompt = prompt.strip()
        if not cleaned_prompt:
            raise ValueError("prompt is required")
        executable = self._executable()
        if shutil.which(executable) is None and not Path(executable).is_file():
            raise FileNotFoundError(f"OpenCode executable not found: {executable}")

        run_id = f"oc_{uuid.uuid4().hex}"
        workspace = _runtime_path(self.config, self.config.opencode.workspace)
        if not workspace.is_dir():
            raise ValueError(f"OpenCode workspace does not exist: {workspace}")

        run_root = _runtime_path(self.config, self.config.opencode.run_root)
        run_dir = run_root / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        report_path = run_dir / "report.json"
        plugin_path = run_dir / "mutsumi-report.mjs"
        plugin_path.write_text(
            _render_report_plugin(self._plugin_sdk_import(executable)),
            encoding="utf-8",
        )
        report_token = secrets.token_urlsafe(32)

        try:
            await self.store.create_delegated_task(
                run_id=run_id,
                conversation_id=conversation_id,
                prompt=cleaned_prompt,
                workspace=str(workspace),
                report_token=report_token,
            )
        except Exception:
            shutil.rmtree(run_dir, ignore_errors=True)
            raise
        task = asyncio.create_task(
            self._run(
                run_id=run_id,
                prompt=cleaned_prompt,
                workspace=workspace,
                run_dir=run_dir,
                report_path=report_path,
                plugin_path=plugin_path,
                report_token=report_token,
                executable=executable,
            )
        )
        self._tasks[run_id] = task
        task.add_done_callback(lambda _: self._tasks.pop(run_id, None))
        return run_id

    async def _drain_stream(self, run_id: str, stream, label: str) -> None:
        while True:
            line = await stream.readline()
            if not line:
                return
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                event = json.loads(text)
                kind = str(event.get("type") or event.get("event") or "json")
            except json.JSONDecodeError:
                kind = "text"
            logger.info("[OPENCODE] run=%s stream=%s event=%s", run_id, label, kind)

    async def _read_report(self, path: Path) -> tuple[str, str] | None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(raw, dict):
            return None
        token = str(raw.get("token") or "")
        report = str(raw.get("report") or "").strip()
        if not token or not report:
            return None
        return token, report

    async def _deliver_report(self, run_id: str, report: str) -> bool:
        """Retry the durable wake-up separately from report acceptance."""
        for attempt in range(3):
            try:
                await self.on_report(run_id, report)
                return True
            except Exception:
                if attempt >= 2:
                    logger.exception(
                        "[OPENCODE] report delivery exhausted run=%s",
                        run_id,
                    )
                    return False
                logger.exception(
                    "[OPENCODE] report delivery failed run=%s retry=%d/2",
                    run_id,
                    attempt + 1,
                )
                await asyncio.sleep(0.2 * (attempt + 1))
        return False

    @staticmethod
    async def _stop_process(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()

    async def _run(
        self,
        *,
        run_id: str,
        prompt: str,
        workspace: Path,
        run_dir: Path,
        report_path: Path,
        plugin_path: Path,
        report_token: str,
        executable: str,
    ) -> None:
        started_at = time.time()
        await self.store.update_delegated_task(run_id, status="running", started_at=started_at)
        command = [
            executable,
            "run",
            "--format",
            "json",
            "--dir",
            str(workspace),
            "--model",
            self._model_reference(),
        ]
        if self.config.opencode.auto_approve:
            command.append("--auto")
        # Avoid a second small-model title request for every delegated task.
        command.extend(["--title", f"Mutsumi task {run_id}"])
        command.append(prompt + REPORT_INSTRUCTION)
        env = os.environ.copy()
        env.update({
            "MUTSUMI_REPORT_PATH": str(report_path),
            "MUTSUMI_REPORT_TOKEN": report_token,
            "MUTSUMI_RUN_ID": run_id,
            # Keep each worker away from the operator's global OpenCode DB.
            # OpenCode 1.18 resolves its SQLite state through the XDG roots;
            # OPENCODE_DB is retained for older builds but is not sufficient
            # on its own. A task does not need cross-task session history.
            "OPENCODE_DB": str(run_dir / "opencode.db"),
            "XDG_CONFIG_HOME": str(run_dir / "xdg-config"),
            "XDG_DATA_HOME": str(run_dir / "xdg-data"),
            "XDG_STATE_HOME": str(run_dir / "xdg-state"),
            "XDG_CACHE_HOME": str(run_dir / "xdg-cache"),
            # Do not let a worker block on models.dev or update checks.  The
            # provider and model are declared above in the per-run config.
            "OPENCODE_DISABLE_MODELS_FETCH": "1",
            "OPENCODE_DISABLE_AUTOUPDATE": "1",
            # OpenCode 1.18 installs @opencode-ai/plugin into every project
            # config directory when project config loading is enabled. Each
            # worker has an isolated XDG root, so that would turn every task
            # into a network-dependent dependency bootstrap. The delegated
            # prompt and the generated report plugin are self-contained.
            "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
            "OPENCODE_CONFIG_CONTENT": self._config_content(plugin_path),
        })
        executable_path = Path(executable)
        if executable_path.is_file():
            opencode_root = executable_path.resolve().parent.parent
            node_modules = opencode_root / "node_modules"
            if node_modules.is_dir():
                existing_node_path = env.get("NODE_PATH", "")
                env["NODE_PATH"] = os.pathsep.join(
                    item for item in (str(node_modules), existing_node_path) if item
                )

        process: asyncio.subprocess.Process | None = None
        stdout_task: asyncio.Task[None] | None = None
        stderr_task: asyncio.Task[None] | None = None
        reported = False
        timed_out = False
        exit_code: int | None = None
        try:
            logger.info("[OPENCODE] starting run=%s workspace=%s", run_id, workspace)
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=str(workspace),
                env=env,
                # ``opencode run`` accepts a positional prompt but still
                # keeps reading stdin until EOF.  A service worker has no
                # interactive input, so an inherited stdin leaves OpenCode
                # stuck after ``init`` without ever reaching the provider.
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self._processes[run_id] = process
            stdout_task = asyncio.create_task(self._drain_stream(run_id, process.stdout, "stdout"))
            stderr_task = asyncio.create_task(self._drain_stream(run_id, process.stderr, "stderr"))
            deadline = time.monotonic() + max(1.0, float(self.config.opencode.timeout_seconds))

            async def accept_report_file() -> None:
                """Accept and deliver one atomically-written report file."""
                nonlocal reported
                if reported or not report_path.exists():
                    return
                parsed = await self._read_report(report_path)
                if parsed is None:
                    return
                token, report = parsed
                accepted = await self.store.accept_delegated_report(
                    run_id,
                    token,
                    report,
                    max_chars=self.config.opencode.max_report_chars,
                )
                if not accepted:
                    # A restart or duplicate callback may have accepted it
                    # between polls. The durable task row remains authoritative.
                    record = await self.store.get_delegated_task(run_id)
                    if record is None or record.status != "reported":
                        return
                else:
                    record = await self.store.get_delegated_task(run_id)
                durable_report = str((record.report if record else report) or "").strip()
                if not durable_report:
                    return
                reported = True
                logger.info("[OPENCODE] accepted report run=%s chars=%d", run_id, len(durable_report))
                await self._deliver_report(
                    run_id,
                    durable_report[:self.config.opencode.max_report_chars],
                )

            while True:
                await accept_report_file()
                if reported:
                    # ``report_task`` is the external worker's completion
                    # protocol.  Do not wait for an unrelated final prose
                    # response; OpenCode may keep its watcher alive after
                    # the report has already been accepted.
                    if process.returncode is None:
                        await self._stop_process(process)
                    exit_code = process.returncode
                    break
                if process.returncode is not None:
                    exit_code = process.returncode
                    break
                if time.monotonic() >= deadline:
                    timed_out = True
                    await self._stop_process(process)
                    exit_code = process.returncode
                    break
                await asyncio.sleep(0.2)

            # The worker can atomically publish the report in the same moment
            # its process exits. Give the filesystem watcher a short settle
            # window before declaring an otherwise clean process unreported.
            for _ in range(5):
                await accept_report_file()
                if reported:
                    break
                await asyncio.sleep(0.05)

            if not reported:
                status = "timeout" if timed_out else "unreported"
                await self.store.update_delegated_task(
                    run_id,
                    status=status,
                    exit_code=exit_code,
                    finished_at=time.time(),
                )
                logger.warning("[OPENCODE] run=%s ended status=%s exit_code=%s", run_id, status, exit_code)
            else:
                await self.store.update_delegated_task(
                    run_id,
                    status="reported",
                    exit_code=exit_code,
                    finished_at=time.time(),
                )
        except asyncio.CancelledError:
            if process is not None and process.returncode is None:
                await self._stop_process(process)
            record = await self.store.get_delegated_task(run_id)
            if self._shutting_down:
                # A service restart must preserve an honest, recoverable
                # diagnostic state instead of turning an unfinished worker
                # into a user-requested cancellation.
                if record is not None and record.status in {"pending", "running"}:
                    await self.store.update_delegated_task(
                        run_id,
                        status="unreported",
                        finished_at=time.time(),
                    )
            else:
                await self.store.update_delegated_task(
                    run_id,
                    status="cancelled",
                    finished_at=time.time(),
                )
            raise
        except Exception:
            logger.exception("[OPENCODE] run=%s failed", run_id)
            await self.store.update_delegated_task(
                run_id,
                status="failed",
                finished_at=time.time(),
            )
        finally:
            if stdout_task is not None:
                await asyncio.gather(stdout_task, return_exceptions=True)
            if stderr_task is not None:
                await asyncio.gather(stderr_task, return_exceptions=True)
            self._processes.pop(run_id, None)
            shutil.rmtree(run_dir, ignore_errors=True)

    async def shutdown(self) -> None:
        self._shutting_down = True
        for process in list(self._processes.values()):
            if process.returncode is None:
                process.terminate()
        for task in list(self._tasks.values()):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()
        self._processes.clear()
