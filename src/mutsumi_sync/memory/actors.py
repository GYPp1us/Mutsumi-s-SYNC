from __future__ import annotations

from dataclasses import dataclass

from .store import ActorRecord, EventRecord
from .timestamps import format_context_timestamp


@dataclass(frozen=True)
class ConversationLabel:
    kind: str
    name: str

    def render(self) -> str:
        if self.kind == "group":
            return f"群聊「{self.name}」"
        if self.kind == "service":
            return f"服务「{self.name}」"
        if self.kind == "platform":
            return "平台"
        return "私聊"


def conversation_label(conversation_id: str, *, actor_kind: str = "human") -> ConversationLabel:
    parts = str(conversation_id).split(":", 1)
    if parts[0] == "group":
        return ConversationLabel("group", parts[1] if len(parts) > 1 else "unknown")
    if actor_kind == "service" or parts[0] == "service":
        return ConversationLabel("service", parts[1] if len(parts) > 1 else "unknown")
    if parts[0] == "platform":
        return ConversationLabel("platform", parts[1] if len(parts) > 1 else "state")
    return ConversationLabel("private", parts[1] if len(parts) > 1 else "unknown")


def actor_display_name(actor: ActorRecord | None, fallback: str, actor_id: str) -> str:
    if actor is not None:
        return actor.private_alias or actor.relationship or actor.display_name or fallback or actor_id
    return fallback or actor_id


def format_actor_source(
    *,
    conversation_id: str,
    actor_id: str,
    actor_name: str,
    actor_kind: str,
    content: str,
    created_at: float | None = None,
    actor: ActorRecord | None = None,
    prefix: str | None = None,
) -> str:
    label = prefix or conversation_label(conversation_id, actor_kind=actor_kind).render()
    name = actor_display_name(actor, actor_name, actor_id)
    timestamp = format_context_timestamp(created_at)
    time_prefix = f"{timestamp}｜" if timestamp else ""
    return f"{time_prefix}{label}｜{name}（{actor_id}）：{content}"


def format_outbound_source(
    *,
    conversation_id: str,
    content: str,
    created_at: float | None = None,
) -> str:
    label = conversation_label(conversation_id).render()
    return f"回复到{label}｜{content}"
