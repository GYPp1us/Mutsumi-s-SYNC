from __future__ import annotations

from pathlib import Path
import re
from typing import Any

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel, Field


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SYSTEM_PROMPTS_FILE = "system-prompts.yaml"


class SystemPromptsConfig(BaseModel):
    persona: str = ""
    runtime: str
    message_summary: str
    summary_merge: str
    episode_summary: str
    heartbeat: str = (
        "这是一次平台发起的自主性检查，不是用户的新消息。"
        "只有存在具体且自然的话题时才填写 TO_USER，否则保持 TO_USER 为空。"
    )


def load_system_prompts(path: Path) -> SystemPromptsConfig:
    if not path.exists():
        raise FileNotFoundError(f"System prompts file not found: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"System prompts file must contain a YAML mapping: {path}")
    prompts = SystemPromptsConfig(**raw)
    empty = [
        name
        for name, value in prompts.model_dump().items()
        if name != "persona" and not str(value).strip()
    ]
    if empty:
        raise ValueError(f"System prompts cannot be empty ({', '.join(empty)}): {path}")
    return prompts


def resolve_system_prompts_path(value: str, config_dir: Path | None = None) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    candidates = []
    if config_dir is not None:
        candidates.append(config_dir / path)
    candidates.extend((Path.cwd() / path, PROJECT_ROOT / path))
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    return candidates[0].resolve()


class NapcatConfig(BaseModel):
    ws_url: str = "ws://localhost:3000"
    http_url: str = "http://localhost:3000"
    access_token: str = ""


class ModelConfig(BaseModel):
    provider: str = "deepseek"
    model: str = "deepseek-chat"
    temperature: float = 0.7
    api_key: str = ""
    base_url: str = "https://api.deepseek.com/v1"
    reasoning_effort: str = "max"


class VisionConfig(BaseModel):
    enabled: bool = False
    provider: str = "openai-compatible"
    model: str = ""
    api_key: str = ""
    base_url: str = ""
    timeout_seconds: float = 60.0
    access_key_id: str = ""
    secret_access_key: str = ""
    session_token: str = ""
    region: str = "cn-north-1"
    service: str = "cv"
    action: str = "OCRNormal"
    version: str = "2020-08-26"


class LogStreamStoreConfig(BaseModel):
    enabled: bool = True
    path: str = "data/logs/mutsumi.ndjson"
    max_bytes: int = 52_428_800
    backup_count: int = 5
    keep_ansi: bool = True


class LogTextFileConfig(BaseModel):
    enabled: bool = True
    path: str = "data/logs/mutsumi.log"
    max_bytes: int = 52_428_800
    backup_count: int = 5
    keep_ansi: bool = False


class LoggingConfig(BaseModel):
    stream_store: LogStreamStoreConfig = LogStreamStoreConfig()
    text_file: LogTextFileConfig = LogTextFileConfig()


class HeartbeatConfig(BaseModel):
    enabled: bool = True
    private_interval_seconds: int = 900
    group_interval_seconds: int = 10800
    active_window_seconds: int = 86400


class PromptsConfig(BaseModel):
    system_file: str = DEFAULT_SYSTEM_PROMPTS_FILE
    system: SystemPromptsConfig = Field(
        default_factory=lambda: load_system_prompts(PROJECT_ROOT / DEFAULT_SYSTEM_PROMPTS_FILE),
        exclude=True,
    )


class ContextConfig(BaseModel):
    max_tokens: int = 4096
    window_max_tokens: int = 100000
    window_min_tokens: int = 50000
    model_context_tokens: int = 100000
    compression_trigger_ratio: float = 0.8
    compression_target_ratio: float = 0.5
    reserved_output_tokens: int = 8192
    recent_actions_max_count: int = 12
    summaries_max_count: int = 180
    summaries_min_count: int = 90
    debounce_timeout: float = 1.5
    episode_idle_seconds: int = 1800
    episode_max_events: int = 160


class MemoryConfig(BaseModel):
    archive_threshold_tokens: int = 100
    self_note_target_tokens: int = 1000
    self_note_max_multiplier: float = 2.0


class InnerJournalConfig(BaseModel):
    max_entry_chars: int = 1000
    max_context_tokens: int = 16000
    max_context_entries: int = 200


class SummarizerConfig(BaseModel):
    provider: str = "deepseek"
    model: str = "deepseek-chat"
    api_key: str = ""
    base_url: str = "https://api.deepseek.com/v1"
    temperature: float = 0.3
    max_input_tokens: int = 32000


class SessionConfig(BaseModel):
    timeout: int = 300


class MarkdownImageRenderConfig(BaseModel):
    enabled: bool = False
    node_path: str = "node"
    script_path: str = "tools/markdown-renderer/render.mjs"
    output_dir: str = "data/generated/markdown"
    timeout_seconds: float = 20.0
    viewport_width: int = 960
    max_height: int = 12000


class RenderConfig(BaseModel):
    markdown_image: MarkdownImageRenderConfig = MarkdownImageRenderConfig()


class Config(BaseModel):
    napcat: NapcatConfig = NapcatConfig()
    model: ModelConfig = ModelConfig()
    context: ContextConfig = ContextConfig()
    session: SessionConfig = SessionConfig()
    memory: MemoryConfig = MemoryConfig()
    inner_journal: InnerJournalConfig = InnerJournalConfig()
    summarizer: SummarizerConfig = SummarizerConfig()
    render: RenderConfig = RenderConfig()
    vision: VisionConfig = VisionConfig()
    heartbeat: HeartbeatConfig = HeartbeatConfig()
    logging: LoggingConfig = LoggingConfig()
    prompts: PromptsConfig = PromptsConfig()

    _config_path: str | None = None
    _system_prompts_path: str | None = None
    dirty: bool = False

    @classmethod
    def load(cls, config_path: str) -> Config:
        path = Path(config_path)
        if not path.exists():
            path = Path.cwd() / config_path

        if path.exists():
            raw = yaml.safe_load(open(path, encoding="utf-8")) or {}

            legacy_persona = raw.pop("system_prompt", "")
            prompts_raw = raw.get("prompts")
            if isinstance(prompts_raw, dict):
                legacy_persona = prompts_raw.pop("persona", "") or legacy_persona

            env = dotenv_values(Path(path.parent, ".env"))

            def resolve_env(value: Any) -> Any:
                if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
                    key = value[2:-1]
                    return env.get(key, value)
                if isinstance(value, dict):
                    return {k: resolve_env(v) for k, v in value.items()}
                return value

            raw = resolve_env(raw)
            prompts_raw = raw.get("prompts")
            if prompts_raw is None:
                prompts_raw = {}
                raw["prompts"] = prompts_raw
            if isinstance(prompts_raw, dict):
                system_file = str(prompts_raw.get("system_file") or DEFAULT_SYSTEM_PROMPTS_FILE)
                prompts_path = resolve_system_prompts_path(system_file, path.parent)
                system_prompts = load_system_prompts(prompts_path)
                if legacy_persona:
                    system_prompts.persona = str(legacy_persona)
                prompts_raw["system"] = system_prompts.model_dump()
            instance = cls(**raw)
        else:
            instance = cls()
            prompts_path = resolve_system_prompts_path(instance.prompts.system_file)

        instance._config_path = str(path)
        instance._system_prompts_path = str(prompts_path)
        return instance

    def set(self, key: str, value: Any) -> str:
        parts = key.split(".")
        if not parts:
            return "[Error: empty key]"
        if key == "prompts.persona":
            return "[Error: edit persona in prompts.system_file directly, then reload config]"
        if len(parts) >= 2 and parts[:2] == ["prompts", "system"]:
            return "[Error: edit prompts.system_file directly, then reload config]"
        if not hasattr(self, parts[0]):
            return f"[Error: unknown config key: {parts[0]}]"

        current: Any = self
        for i, part in enumerate(parts[:-1]):
            current = getattr(current, part, None)
            if current is None or not isinstance(current, BaseModel):
                return f"[Error: invalid config path: {key}]"

        last_part = parts[-1]
        target = getattr(current, last_part, None)
        if target is None and not hasattr(current, last_part):
            return f"[Error: unknown config key: {key}]"

        if isinstance(value, str):
            try:
                if isinstance(target, bool):
                    normalized = value.strip().lower()
                    if normalized in ("true", "1", "yes", "on"):
                        value = True
                    elif normalized in ("false", "0", "no", "off"):
                        value = False
                    else:
                        return f"[Error: invalid boolean value for {key}: {value}]"
                elif isinstance(target, float):
                    value = float(value)
                elif isinstance(target, int):
                    value = int(value)
            except ValueError as e:
                return f"[Error: cannot set {key} to {value}: {e}]"

        try:
            setattr(current, last_part, value)
        except (TypeError, ValueError) as e:
            return f"[Error: cannot set {key} to {value}: {e}]"

        self.dirty = True
        return f"[OK] {key} = {value}"

    def get(self, key: str) -> Any:
        parts = key.split(".")
        current: Any = self
        for part in parts:
            current = getattr(current, part, None)
            if current is None:
                return f"[Error: unknown config key: {key}]"
        return current

    def save(self) -> None:
        if self._config_path:
            with open(self._config_path, "w", encoding="utf-8") as f:
                yaml.dump(
                    self.model_dump(exclude_none=True, exclude={"_config_path", "dirty"}),
                    f,
                    allow_unicode=True,
                    default_flow_style=False,
                )

    def save_key(self, key: str) -> None:
        if not self._config_path:
            return

        path = Path(self._config_path)
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True) if path.exists() else []
        parts = key.split(".")
        if not parts:
            return

        value = self.get(key)
        if isinstance(value, str) and value.startswith("[Error:"):
            return

        if len(parts) == 1:
            self._save_top_level_key(path, lines, parts[0], value)
            return

        self._save_nested_key(path, lines, parts, value)

    def _format_yaml_scalar(self, value: Any, indent: int = 0) -> str:
        dumped = yaml.safe_dump(
            value,
            allow_unicode=True,
            default_flow_style=False,
            sort_keys=False,
        ).strip()
        if "\n...\n" in f"\n{dumped}\n" or dumped.endswith("\n..."):
            dumped = "\n".join(line for line in dumped.splitlines() if line != "...")
        if "\n" not in dumped:
            return dumped
        pad = " " * indent
        return "\n".join(pad + line if line else line for line in dumped.splitlines())

    def _save_top_level_key(self, path: Path, lines: list[str], key: str, value: Any) -> None:
        replacement = f"{key}: {self._format_yaml_scalar(value)}\n"
        for i, line in enumerate(lines):
            if line.startswith(f"{key}:"):
                lines[i] = replacement
                path.write_text("".join(lines), encoding="utf-8")
                return
        lines.append(replacement)
        path.write_text("".join(lines), encoding="utf-8")

    def _save_nested_key(self, path: Path, lines: list[str], parts: list[str], value: Any) -> None:
        stack: list[tuple[int, str]] = []
        deepest_prefix = 0
        parent_end = len(lines)
        parent_indent = -2
        parent_idx = -1

        for i, line in enumerate(lines):
            match = re.match(r"^(\s*)([A-Za-z0-9_-]+)\s*:(.*?)(\r?\n)?$", line)
            if not match:
                continue
            indent = len(match.group(1).replace("\t", "  "))
            while stack and stack[-1][0] >= indent:
                stack.pop()
            current_path = [item[1] for item in stack] + [match.group(2)]

            if current_path == parts:
                suffix = match.group(3)
                comment = ""
                if " #" in suffix:
                    comment = "  #" + suffix.split(" #", 1)[1]
                newline = match.group(4) or "\n"
                lines[i] = (
                    f"{' ' * indent}{parts[-1]}: {self._format_yaml_scalar(value, indent=indent)}"
                    f"{comment}{newline}"
                )
                path.write_text("".join(lines), encoding="utf-8")
                return

            prefix_len = 0
            for expected, actual in zip(parts[:-1], current_path):
                if expected != actual:
                    break
                prefix_len += 1
            if prefix_len > deepest_prefix and current_path == parts[:prefix_len]:
                deepest_prefix = prefix_len
                parent_idx = i
                parent_indent = indent

            stack.append((indent, match.group(2)))

        if parent_idx >= 0:
            parent_end = len(lines)
            for i in range(parent_idx + 1, len(lines)):
                stripped = lines[i].lstrip(" ")
                if not stripped or stripped.startswith("#"):
                    continue
                indent = len(lines[i]) - len(stripped)
                if indent <= parent_indent:
                    parent_end = i
                    break
        else:
            if lines and not lines[-1].endswith(("\n", "\r")):
                lines[-1] += "\n"
            parent_end = len(lines)
            parent_indent = -2

        additions: list[str] = []
        for depth, key in enumerate(parts[deepest_prefix:-1], start=deepest_prefix):
            additions.append(f"{' ' * (depth * 2)}{key}:\n")
        leaf_indent = (len(parts) - 1) * 2
        additions.append(
            f"{' ' * leaf_indent}{parts[-1]}: {self._format_yaml_scalar(value, indent=leaf_indent)}\n"
        )
        lines[parent_end:parent_end] = additions
        path.write_text("".join(lines), encoding="utf-8")

    def reload(self) -> str:
        if not self._config_path or not Path(self._config_path).exists():
            return "[Error: no config file to reload]"
        loaded = type(self).load(self._config_path)
        for key in type(self).model_fields:
            if key == "dirty":
                continue
            setattr(self, key, getattr(loaded, key))
        self._system_prompts_path = loaded._system_prompts_path
        self.dirty = True
        return "[OK] config reloaded"
