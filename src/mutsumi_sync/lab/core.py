from __future__ import annotations

import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ..config import Config


def default_lab_prompt() -> str:
    return (
        "你是 Mutsumi，一位温和、清醒、有一点点幽默感的 QQ 聊天助手。"
        "你会先理解对方的语气，再决定是认真回答、顺着聊，还是轻巧地安抚。"
        "你喜欢自然、短而有温度的表达，不端着，不装腔，不讲大道理。"
        "当需要动手做事时，你优先用工具完成；当信息不足时，你会自然追问。"
        "如果用户看起来疲惫、焦躁或迷茫，你会先给情绪上的支撑，再给行动建议。"
        "你会把记忆工具当作长期照看的笔记本，把 send 当作实际发言动作。"
        "任何要给用户看的回复都必须调用 send 工具发出；不要只写 content。"
        "使用 self_note、memory_save、memory_search 等工具之后，最终给用户看的确认、解释、答案也必须再调用 send。"
        "普通 content 不会发送给用户，只会被系统丢弃并记录警告。"
        "如果已经调用 send，就不要再在普通 content 里重复同一句话。"
        "回答要像真实人在聊天，而不是说明书。"
    )


@dataclass(frozen=True)
class ScenarioStep:
    text: str


@dataclass(frozen=True)
class Scenario:
    name: str
    prompt: str
    steps: list[ScenarioStep]


@dataclass(frozen=True)
class LabSession:
    session_id: str
    seed_root: Path
    sessions_root: Path
    session_root: Path

    @classmethod
    def create(cls, seed_root: Path, sessions_root: Path) -> LabSession:
        sessions_root.mkdir(parents=True, exist_ok=True)
        seed_root.mkdir(parents=True, exist_ok=True)
        session_id = uuid.uuid4().hex[:12]
        session_root = sessions_root / session_id
        if session_root.exists():
            shutil.rmtree(session_root)
        session_root.mkdir(parents=True, exist_ok=False)
        return cls(
            session_id=session_id,
            seed_root=seed_root,
            sessions_root=sessions_root,
            session_root=session_root,
        )


def load_scenario(path: str) -> Scenario:
    p = Path(path)
    raw: dict[str, Any] = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    steps = [ScenarioStep(text=str(step.get("text", ""))) for step in raw.get("steps", [])]
    return Scenario(
        name=str(raw.get("name", p.stem)),
        prompt=str(raw.get("prompt", "")),
        steps=steps,
    )


def resolve_config_path(config_path: str) -> str:
    path = Path(config_path)
    if path.exists():
        return str(path.resolve())

    cwd_candidate = Path.cwd() / config_path
    if cwd_candidate.exists():
        return str(cwd_candidate)

    for parent in Path.cwd().parents:
        candidate = parent / config_path
        if candidate.exists():
            return str(candidate)

    return config_path


def load_lab_config(config_path: str = "config.yaml", *, reasoning_effort: str = "high") -> Config:
    config = Config.load(resolve_config_path(config_path))
    config.model.reasoning_effort = reasoning_effort
    return config
