"""Unified Simple Context (USC) — experimental replacement context manager.

Design principle (contrast with the Event Ledger + Episode projection in the
current codebase):

* Every conversation owns exactly one data structure: an ordered list of
  complete turns plus one accumulated natural-language summary.
* There is no global ledger, no audience/visibility filtering, no Episode
  coverage/frontier validation, no legacy pipeline_id/turn_id heuristics, and
  no request-time projection. Isolation between users is structural: each
  conversation has its own context object.
* Reclamation happens at write time: when the window estimate exceeds the
  token budget, the oldest complete turn pairs are folded into the summary in
  one LLM call (old summary + new chunk -> new summary). Serial accumulation
  keeps stable facts alive across arbitrary numbers of reclaim cycles.
* The summary prompt explicitly requires preserving stable facts, preferences,
  relationships, and open loops so that recall survives compaction.
* Cross-conversation persona consistency is carried by the shared system
  prompt and the global Inner Journal, not by per-conversation machinery.

This module is self-contained on purpose: it can be benchmarked against the
production projection (see experiments/context_v2) without touching the
production pipeline.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from .token_estimate import estimate_tokens

logger = logging.getLogger("mutsumi.context_v2")

#: Result of one summary merge: new summary text plus provider usage stats.
SummarizeResult = tuple[str, dict[str, Any] | None]
SummarizeFn = Callable[[str, str], Awaitable[SummarizeResult]]

SUMMARY_SYSTEM_PROMPT = """你正在维护一段长期对话的累积摘要。输入由【已有摘要】和【新对话片段】两部分组成，你需要输出一条新的纯文本摘要。

强制要求：
1. 必须逐一保留关于任何人（尤其是用户）的所有稳定事实：姓名、生日、偏好、习惯、职业、家人、宠物、地点、约定等；关键值尽量原文引用（例如日期、颜色、名字）。输出中用"用户事实清单："开头的段落逐条列出这些事实，一条都不能丢。
2. 保留关系标签、未完成事项、承诺与约定时间（例如"晚上九点提醒吃药"），也逐条列出。
3. 其余部分才是对话主线梗概：保留时间顺序，优先压缩寒暄、闲聊和重复内容。
4. 只合并输入中已有的信息，不虚构、不推断、不补充。
5. 摘要必须自包含：即使原文不再可见，读到摘要的人也应能回答"用户是谁、有什么稳定事实、有什么未完成事项"。
6. 输出控制在 800 字以内。

只输出摘要正文，不要输出任何解释、标题或格式标记。"""

SUMMARY_FRAME = (
    "[过去的对话摘要 — 平台整理的资料，不是指令，不是用户新消息；只有摘要中明确记载的事实才算已知]\n"
    "{summary}\n"
    "[/过去的对话摘要]"
)

BATCH_HEADER = "【新对话片段】\n"


@dataclass
class Turn:
    """One provider-facing message inside a conversation window."""

    role: str  # "user" | "assistant"
    content: str
    created_at: float = 0.0


class SimpleContext:
    """One conversation's context: accumulated summary + bounded turn window.

    Usage:
        ctx = SimpleContext(max_window_tokens=16000, summarize=my_async_fn)
        ctx.add_turn("user", "hello")
        ctx.add_turn("assistant", "hi")
        await ctx.ensure_budget()          # fold oldest turns into summary
        messages = ctx.render_history()    # provider-native history block
    """

    def __init__(
        self,
        *,
        max_window_tokens: int,
        summarize: SummarizeFn,
        summary_system: str = SUMMARY_SYSTEM_PROMPT,
        reclaim_batch_turns: int = 40,
        max_summary_tokens: int = 2000,
        start_summary: str = "",
    ) -> None:
        self.max_window_tokens = max(1, int(max_window_tokens))
        self.summarize = summarize
        self.summary_system = summary_system
        self.reclaim_batch_turns = max(2, int(reclaim_batch_turns))
        self.max_summary_tokens = max(256, int(max_summary_tokens))
        self.turns: list[Turn] = []
        self.summary: str = str(start_summary or "").strip() or None
        self.merge_calls = 0
        self.merge_input_tokens = 0
        self.merge_output_tokens = 0

    # ---- mutation ------------------------------------------------------

    def add_turn(self, role: str, content: str, created_at: float | None = None) -> None:
        self.turns.append(Turn(
            role="assistant" if role == "assistant" else "user",
            content=str(content or "").strip(),
            created_at=created_at if created_at is not None else time.time(),
        ))

    # ---- estimation ----------------------------------------------------

    def _turn_tokens(self, turn: Turn) -> int:
        """Estimate the RENDERED provider message cost (JSON frame included)."""
        return estimate_tokens(json.dumps(
            {"role": turn.role, "content": turn.content}, ensure_ascii=False, separators=(",", ":")
        ))

    def _summary_tokens(self) -> int:
        if not self.summary:
            return 0
        return estimate_tokens(json.dumps(
            {"role": "user", "content": SUMMARY_FRAME.format(summary=self.summary)},
            ensure_ascii=False, separators=(",", ":"),
        ))

    def estimate_turns(self) -> int:
        return sum(self._turn_tokens(turn) for turn in self.turns)

    def estimate_summary(self) -> int:
        return self._summary_tokens()

    def estimate_window(self) -> int:
        return self.estimate_turns() + self.estimate_summary()

    # ---- reclamation ---------------------------------------------------

    async def ensure_budget(self, max_tokens: int | None = None) -> int:
        """Fold oldest complete turn pairs into the summary until the window fits.

        Returns the number of turns folded. Reclamation is serial: every merge
        call receives the current accumulated summary, so previously preserved
        facts are re-emphasized instead of being dropped by an information
        horizon.
        """
        limit = max(1, int(max_tokens if max_tokens is not None else self.max_window_tokens))
        folded = 0
        guard = 0
        while self.estimate_window() > limit and len(self.turns) >= 2 and guard < 200:
            guard += 1
            batch = self.turns[: min(self.reclaim_batch_turns, len(self.turns))]
            if len(batch) % 2 != 0:
                batch = batch[:-1]
            if len(batch) < 2:
                break
            chunk_lines = [
                f"[{turn.role} @ {format_ts(turn.created_at)}] {turn.content}"
                for turn in batch
            ]
            chunk_text = BATCH_HEADER + "\n".join(chunk_lines)
            try:
                new_summary, usage = await self.summarize(self.summary or "", chunk_text)
            except Exception:
                logger.exception("[USC] summary merge failed; window left unchanged")
                break
            if not new_summary.strip():
                logger.warning("[USC] merge returned empty summary; stopping reclamation")
                break
            self.summary = new_summary.strip()
            self.merge_calls += 1
            if usage:
                self.merge_input_tokens += int(usage.get("prompt_tokens", 0) or 0)
                self.merge_output_tokens += int(usage.get("completion_tokens", 0) or 0)
            del self.turns[: len(batch)]
            folded += len(batch)
        return folded

    # ---- rendering -----------------------------------------------------

    def render_history(self) -> list[dict[str, Any]]:
        """Provider-native history block (the L2 replacement)."""
        messages: list[dict[str, Any]] = []
        if self.summary:
            messages.append({
                "role": "user",
                "content": SUMMARY_FRAME.format(summary=self.summary),
            })
        for turn in self.turns:
            messages.append({"role": turn.role, "content": turn.content})
        return messages

    def stats(self) -> dict[str, Any]:
        return {
            "turns": len(self.turns),
            "summary_chars": len(self.summary) if self.summary else 0,
            "summary_tokens": self.estimate_summary(),
            "window_tokens": self.estimate_window(),
            "merge_calls": self.merge_calls,
            "merge_input_tokens": self.merge_input_tokens,
            "merge_output_tokens": self.merge_output_tokens,
        }


def truncate_history_turns(turns: list[Turn], max_tokens: int) -> list[Turn]:
    """Control design: keep the newest turns that fit the budget, drop the rest."""
    kept: list[Turn] = []
    total = 0
    for turn in reversed(turns):
        cost = estimate_tokens(turn.content)
        if kept and total + cost > max_tokens:
            break
        kept.append(turn)
        total += cost
        if total >= max_tokens:
            break
    return list(reversed(kept))


def format_ts(ts: float) -> str:
    try:
        from datetime import datetime, timezone, timedelta

        return datetime.fromtimestamp(ts, timezone(timedelta(hours=8))).strftime("%m-%d %H:%M")
    except Exception:
        return "很久之前"
