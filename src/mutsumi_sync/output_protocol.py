from __future__ import annotations

from dataclasses import dataclass
import re


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


def parse_final_envelope(content: str, *, strict: bool = True) -> FinalEnvelope:
    """Parse the final assistant content, optionally recovering common omissions."""
    if not strict:
        recovered = recover_final_envelope(content)
        if recovered is not None:
            return recovered

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


def recover_final_envelope(content: str) -> FinalEnvelope | None:
    """Recover common model omissions without weakening strict parsing."""
    text = str(content).strip()
    if not text:
        return None

    markers = (_SELF_OPEN, _SELF_CLOSE, _USER_OPEN, _USER_CLOSE)
    if not any(marker in text for marker in markers):
        return FinalEnvelope(to_self="", to_user=text)

    self_match = re.fullmatch(r"\s*\[TO_SELF\](.*?)\[/TO_SELF\]\s*", text, re.DOTALL)
    user_match = re.search(r"\[TO_USER\](.*?)\[/TO_USER\]", text, re.DOTALL)
    if user_match is None:
        return None
    remainder = text[:user_match.start()] + text[user_match.end():]
    if remainder.strip() and self_match is None:
        return None
    return FinalEnvelope(
        to_self=self_match.group(1).strip() if self_match else "",
        to_user=user_match.group(1).strip(),
    )
