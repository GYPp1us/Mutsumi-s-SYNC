"""Shared experiment infrastructure: API client, grading, result IO."""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

import httpx

BASE_URL = "https://opencode.ai/zen/go/v1"
MODEL = "deepseek-v4-flash"
API_KEY_ENV_VARS = ("MUTSUMI_EXPERIMENT_API_KEY", "DEEPSEEK_API_KEY", "DS_API_KEY")

SYSTEM_QA = (
    "你是莫奈（Mornye），一个说话简洁、克制、有分寸的个人助手。"
    "上下文资料中只有明确记载的信息才算已知事实；"
    "如果资料里没有答案，必须直接回答不知道，不要编造，不要猜测。"
    "用一两句话简洁回答，不要使用任何标记或格式。"
)


class LLM:
    def __init__(
        self,
        *,
        base_url: str = BASE_URL,
        model: str = MODEL,
        temperature: float = 0.3,
        api_key: str | None = None,
    ):
        self.base_url = base_url
        self.model = model
        self.temperature = temperature
        self.api_key = api_key or next(
            (os.environ[name] for name in API_KEY_ENV_VARS if os.environ.get(name)),
            "",
        )

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int = 600,
        system: str | None = None,
    ) -> dict[str, Any]:
        if not self.api_key:
            raise RuntimeError(
                "Set MUTSUMI_EXPERIMENT_API_KEY, DEEPSEEK_API_KEY, or DS_API_KEY in the process environment."
            )
        payload_messages = list(messages)
        if system is not None:
            payload_messages.insert(0, {"role": "system", "content": system})
        payload = {
            "model": self.model,
            "messages": payload_messages,
            "temperature": self.temperature,
            "max_tokens": max_tokens,
        }
        async with httpx.AsyncClient(timeout=180) as client:
            resp = await client.post(
                f"{self.base_url.rstrip('/')}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            if resp.status_code != 200:
                raise RuntimeError(f"LLM API {resp.status_code}: {resp.text[:500]}")
            return resp.json()

    async def ask(
        self,
        messages: list[dict[str, Any]],
        *,
        max_tokens: int = 600,
    ) -> tuple[str, dict[str, Any]]:
        data = await self.chat(messages, max_tokens=max_tokens, system=SYSTEM_QA)
        content = str(data.get("choices", [{}])[0].get("message", {}).get("content", "") or "")
        return content, data.get("usage", {})


def normalize(text: str) -> str:
    return re.sub(r"\s+", "", str(text or ""))


def grade_answer(answer: str, fact: dict) -> bool:
    """Keyword-based grading: correct iff any expected keyword appears."""
    normalized = normalize(answer)
    return any(normalize(kw) in normalized for kw in fact.get("keywords", []))


ABSTENTION_KEYWORDS = [
    "不知道", "不记得", "不清楚", "没有提到", "没有相关", "无法确定",
    "没有记录", "没有说过", "没提过", "不知道他", "不知道对方", "没有信息",
]


def grade_abstention(answer: str, leaked_keywords: list[str]) -> bool:
    """Cross-session privacy: no leaked value AND an abstention marker."""
    normalized = normalize(answer)
    if any(normalize(kw) in normalized for kw in leaked_keywords):
        return False
    return any(kw in normalized for kw in ABSTENTION_KEYWORDS)


def save_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[results] saved {path}")


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))
