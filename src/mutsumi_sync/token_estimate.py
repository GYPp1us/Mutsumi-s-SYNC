from __future__ import annotations

import math


def _is_cjk(character: str) -> bool:
    codepoint = ord(character)
    return (
        0x2E80 <= codepoint <= 0x2FFF
        or 0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0x20000 <= codepoint <= 0x323AF
    )


def estimate_tokens(text: str) -> int:
    """Estimate provider tokens without assuming four characters for Chinese."""
    ascii_count = 0
    cjk_count = 0
    other_count = 0
    for character in str(text):
        if character.isascii():
            ascii_count += 1
        elif _is_cjk(character):
            cjk_count += 1
        else:
            other_count += 1
    return max(
        1,
        math.ceil(ascii_count / 4)
        + cjk_count
        + math.ceil(other_count * 3 / 2),
    )


def take_prefix_for_tokens(text: str, max_tokens: int) -> str:
    """Return the longest prefix that fits the estimated token budget."""
    limit = max(1, int(max_tokens))
    source = str(text)
    high = min(len(source), max(1, limit * 4))
    if len(source) <= high and estimate_tokens(source) <= limit:
        return source

    low = 1
    best = 1
    while low <= high:
        middle = (low + high) // 2
        if estimate_tokens(source[:middle]) <= limit:
            best = middle
            low = middle + 1
        else:
            high = middle - 1
    return source[:best]


def split_text_by_token_budget(text: str, max_tokens: int) -> list[str]:
    """Split text without producing chunks over the estimated token budget."""
    source = str(text)
    if not source:
        return [""]
    limit = max(1, int(max_tokens))
    chunks: list[str] = []
    remaining = source
    while remaining:
        chunk = take_prefix_for_tokens(remaining, limit)
        if not chunk:
            chunk = remaining[:1]
        chunks.append(chunk)
        remaining = remaining[len(chunk):]
    return chunks
