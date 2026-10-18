import json

import pytest

from src.mutsumi_sync.output_protocol import OutputProtocolError, format_final_envelope
from src.mutsumi_sync.prototype.protocol import parse_reply


def _reply(messages: list[str]) -> str:
    return format_final_envelope(to_self="还在等结果。", to_user=json.dumps({"messages": messages}))


def test_silence_and_multiple_messages_preserve_exact_text() -> None:
    assert parse_reply(_reply([])).messages == []
    parsed = parse_reply(_reply(["a | b", "c \\| d", "  保留缩进\n下一行  "]))
    assert parsed.to_self == "还在等结果。"
    assert parsed.messages == ["a | b", "c \\| d", "  保留缩进\n下一行  "]


@pytest.mark.parametrize("raw", [
    "hello",
    '[TO_USER]{"messages": []}[/TO_USER]',
    '[TO_SELF]x[/TO_SELF]extra[TO_USER]{"messages": []}[/TO_USER]',
    _reply(["hello"]) + "trailing prose",
    '[TO_SELF][TO_SELF]nested[/TO_SELF][TO_USER]{"messages": []}[/TO_USER]',
])
def test_requires_strict_double_envelope(raw: str) -> None:
    with pytest.raises(OutputProtocolError):
        parse_reply(raw)


@pytest.mark.parametrize("payload", [
    "",
    "{broken",
    '{"messages": ["first"], "messages": ["second"]}',
    '{"messages": ["hello"], "send_to": "another person"}',
    '{"messages": "hello"}',
    '{"messages": [null]}',
    '{"messages": [1]}',
    '{"messages": [true]}',
    '{"messages": [{}]}',
    '{"messages": ["   "]}',
    '{"messages": ["\\n\\t"]}',
    "[]",
    "null",
])
def test_rejects_ambiguous_or_invalid_json(payload: str) -> None:
    with pytest.raises(OutputProtocolError):
        parse_reply(format_final_envelope(to_user=payload))


@pytest.mark.parametrize("message", [
    "[TO_SELF]私密内容",
    "[/TO_SELF]",
    "[TO_USER]",
    "[/TO_USER]",
    "# 标题",
    "```python\nprint(1)\n```",
    "| a | b |\n|---|---|",
    "[链接](https://example.com)",
    "$$x^2$$",
])
def test_rejects_protocol_leaks_and_complex_markdown(message: str) -> None:
    with pytest.raises(OutputProtocolError):
        parse_reply(_reply([message]))


def test_json_unicode_escape_cannot_bypass_protocol_marker_gate() -> None:
    raw = format_final_envelope(to_user=r'{"messages":["\u005bTO_SELF\u005dsecret"]}')
    with pytest.raises(OutputProtocolError):
        parse_reply(raw)


def test_limits_reject_whole_reply_instead_of_truncating() -> None:
    assert parse_reply(_reply(["abcd"]), max_messages=1, max_chars=4).messages == ["abcd"]
    with pytest.raises(OutputProtocolError):
        parse_reply(_reply(["abcde"]), max_chars=4)
    with pytest.raises(OutputProtocolError):
        parse_reply(_reply(["good", "extra"]), max_messages=1)
    with pytest.raises(OutputProtocolError):
        parse_reply(_reply(["good", "# invalid later message"]))
