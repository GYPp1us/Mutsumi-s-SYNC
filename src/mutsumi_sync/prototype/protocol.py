"""Strict, bounded message lists for the independent conversation prototype."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ..output_protocol import OutputProtocolError, parse_final_envelope
from ..pipeline import _unsupported_markdown_features


_MARKERS = ("[TO_SELF]", "[/TO_SELF]", "[TO_USER]", "[/TO_USER]")


@dataclass(frozen=True)
class Reply:
    to_self: str
    messages: list[str]


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject duplicate JSON keys instead of silently choosing the last one."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise OutputProtocolError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def parse_reply(content: str, max_messages: int = 4, max_chars: int = 500) -> Reply:
    """Accept exactly two channels and a TO_USER object containing messages only.

    An empty list is deliberate silence. Every nonempty item becomes one message;
    pipes remain literal text. The entire reply is validated before any item sends.
    """
    if max_messages < 1 or max_chars < 1:
        raise ValueError("reply limits must be positive")
    if not isinstance(content, str):
        raise OutputProtocolError("reply content must be text")
    envelope = parse_final_envelope(content, strict=True)
    if any(marker in envelope.to_self or marker in envelope.to_user for marker in _MARKERS):
        raise OutputProtocolError("nested output protocol markers are not allowed")
    try:
        payload = json.loads(envelope.to_user, object_pairs_hook=_unique_object)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise OutputProtocolError("TO_USER must contain a JSON object") from exc
    if not isinstance(payload, dict) or set(payload) != {"messages"}:
        raise OutputProtocolError("TO_USER must contain only the messages field")
    messages = payload["messages"]
    if not isinstance(messages, list):
        raise OutputProtocolError("messages must be an array")
    if len(messages) > max_messages:
        raise OutputProtocolError(f"at most {max_messages} messages are allowed")
    for message in messages:
        if not isinstance(message, str) or not message.strip():
            raise OutputProtocolError("each message must be nonblank text")
        if len(message) > max_chars:
            raise OutputProtocolError(f"each message must be at most {max_chars} characters")
        # Check decoded strings too: JSON unicode escapes must not bypass the gate.
        if any(marker in message for marker in _MARKERS):
            raise OutputProtocolError("output protocol markers cannot appear in messages")
        unsupported = _unsupported_markdown_features(message)
        if unsupported:
            raise OutputProtocolError("messages must be plain text: " + ", ".join(unsupported))
    return Reply(to_self=envelope.to_self, messages=messages)
