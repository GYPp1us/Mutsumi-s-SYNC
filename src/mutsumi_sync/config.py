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


class ContextConfig(BaseModel):
    window_size: int = 20
    max_tokens: int = 4096


class SessionConfig(BaseModel):
    timeout: int = 300


class Config(BaseModel):
    napcat: NapcatConfig = NapcatConfig()
    model: ModelConfig = ModelConfig()
    context: ContextConfig = ContextConfig()
    session: SessionConfig = SessionConfig()
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
