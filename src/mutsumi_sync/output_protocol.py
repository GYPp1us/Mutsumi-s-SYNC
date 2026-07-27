from __future__ import annotations

from dataclasses import dataclass


_SELF_OPEN = "[TO_SELF]"
_SELF_CLOSE = "[/TO_SELF]"
_USER_OPEN = "[TO_USER]"
_USER_CLOSE = "[/TO_USER]"


@dataclass(frozen=True)
class FinalEnvelope:
    to_self: str
    to_user: str


class OutputProtocolError(ValueError):
    """Raised when final assistant content is not in the dual-channel format."""


def format_final_envelope(*, to_self: str = "", to_user: str = "") -> str:
    """Build the exact final-output envelope used by the model contract."""
    return f"{_SELF_OPEN}\n{to_self.strip()}\n{_SELF_CLOSE}\n{_USER_OPEN}\n{to_user.strip()}\n{_USER_CLOSE}"


def parse_final_envelope(content: str) -> FinalEnvelope:
    """Parse the complete final assistant content without accepting extra prose."""
    text = str(content).strip()
    if not text.startswith(_SELF_OPEN):
        raise OutputProtocolError("content must start with [TO_SELF]")

    self_end = text.find(_SELF_CLOSE, len(_SELF_OPEN))
    if self_end < 0:
        raise OutputProtocolError("missing [/TO_SELF]")
    self_content = text[len(_SELF_OPEN):self_end].strip()

    user_start = self_end + len(_SELF_CLOSE)
    between = text[user_start:]
    whitespace = between[:len(between) - len(between.lstrip())]
    user_start += len(whitespace)
    if text[user_start:user_start + len(_USER_OPEN)] != _USER_OPEN:
        raise OutputProtocolError("[TO_USER] must follow [/TO_SELF]")
    user_content_start = user_start + len(_USER_OPEN)
    user_end = text.find(_USER_CLOSE, user_content_start)
    if user_end < 0:
        raise OutputProtocolError("missing [/TO_USER]")
    if text[user_end + len(_USER_CLOSE):].strip():
        raise OutputProtocolError("content after [/TO_USER] is not allowed")

    return FinalEnvelope(
        to_self=self_content,
        to_user=text[user_content_start:user_end].strip(),
    )
