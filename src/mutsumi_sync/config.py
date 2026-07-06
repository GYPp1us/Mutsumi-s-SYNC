from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel


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


class HeartbeatConfig(BaseModel):
    enabled: bool = True
    interval_seconds: int = 2700
    aggressive_provider_cache_retention: bool = False


class ContextConfig(BaseModel):
    max_tokens: int = 4096
    window_max_tokens: int = 100000
    window_min_tokens: int = 50000
    summaries_max_count: int = 180
    summaries_min_count: int = 90
    debounce_timeout: float = 1.5


class MemoryConfig(BaseModel):
    archive_threshold_tokens: int = 100
    self_note_target_tokens: int = 1000
    self_note_max_multiplier: float = 2.0


class SummarizerConfig(BaseModel):
    provider: str = "deepseek"
    model: str = "deepseek-chat"
    api_key: str = ""
    base_url: str = "https://api.deepseek.com/v1"
    temperature: float = 0.3


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
    summarizer: SummarizerConfig = SummarizerConfig()
    render: RenderConfig = RenderConfig()
    vision: VisionConfig = VisionConfig()
    heartbeat: HeartbeatConfig = HeartbeatConfig()
    system_prompt: str = ""

    _config_path: str | None = None
    dirty: bool = False

    @classmethod
    def load(cls, config_path: str) -> Config:
        path = Path(config_path)
        if not path.exists():
            path = Path.cwd() / config_path

        if path.exists():
            raw = yaml.safe_load(open(path, encoding="utf-8")) or {}

            env = dotenv_values(Path(path.parent, ".env"))

            def resolve_env(value: Any) -> Any:
                if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
                    key = value[2:-1]
                    return env.get(key, value)
                if isinstance(value, dict):
                    return {k: resolve_env(v) for k, v in value.items()}
                return value

            raw = resolve_env(raw)
            instance = cls(**raw)
        else:
            instance = cls()

        instance._config_path = str(path)
        return instance

    def set(self, key: str, value: Any) -> str:
        parts = key.split(".")
        if not parts:
            return "[Error: empty key]"
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
            if isinstance(target, float):
                value = float(value)
            elif isinstance(target, int):
                value = int(value)
            elif isinstance(target, bool):
                value = value.lower() in ("true", "1", "yes")

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
                    self.model_dump(exclude_none=True, exclude={"_config_path", "dirty", "system_prompt"}),
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
        section = parts[0]
        leaf = parts[-1]
        section_idx = None
        for i, line in enumerate(lines):
            if line.startswith(f"{section}:"):
                section_idx = i
                break

        if section_idx is None:
            lines.append(f"{section}:\n")
            lines.append(f"  {leaf}: {self._format_yaml_scalar(value, indent=2)}\n")
            path.write_text("".join(lines), encoding="utf-8")
            return

        insert_at = len(lines)
        for i in range(section_idx + 1, len(lines)):
            line = lines[i]
            stripped = line.lstrip(" ")
            indent = len(line) - len(stripped)
            if stripped and indent == 0 and not stripped.startswith("#"):
                insert_at = i
                break
            if indent == 2 and stripped.startswith(f"{leaf}:"):
                lines[i] = f"  {leaf}: {self._format_yaml_scalar(value, indent=2)}\n"
                path.write_text("".join(lines), encoding="utf-8")
                return

        lines.insert(insert_at, f"  {leaf}: {self._format_yaml_scalar(value, indent=2)}\n")
        path.write_text("".join(lines), encoding="utf-8")

    def reload(self) -> str:
        if not self._config_path or not Path(self._config_path).exists():
            return "[Error: no config file to reload]"
        with open(self._config_path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if not data:
            return "[OK] config file empty, unchanged"
        for key, value in data.items():
            if hasattr(self, key):
                if isinstance(value, dict):
                    obj = getattr(self, key)
                    if isinstance(obj, BaseModel):
                        for k, v in value.items():
                            if hasattr(obj, k):
                                setattr(obj, k, v)
                else:
                    setattr(self, key, value)
        self.dirty = True
        return "[OK] config reloaded"
