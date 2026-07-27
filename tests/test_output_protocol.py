from src.mutsumi_sync.output_protocol import (
    OutputProtocolError,
    format_final_envelope,
    parse_final_envelope,
)


def test_final_envelope_round_trips_and_allows_separator_whitespace():
    content = format_final_envelope(
        to_self="我还需要确认发送结果。",
        to_user="已经处理好了。",
    )

    parsed = parse_final_envelope(content)

    assert parsed.to_self == "我还需要确认发送结果。"
    assert parsed.to_user == "已经处理好了。"


def test_final_envelope_allows_empty_channels():
    assert parse_final_envelope(format_final_envelope()).to_user == ""
    assert parse_final_envelope(format_final_envelope(to_user="reply")).to_self == ""


def test_final_envelope_rejects_prose_outside_channels():
    for content in (
        "先说一句\n" + format_final_envelope(to_user="reply"),
        format_final_envelope(to_user="reply") + "\n再说一句",
        "[TO_SELF]x[/TO_SELF]说明文字[TO_USER]reply[/TO_USER]",
    ):
        try:
            parse_final_envelope(content)
        except OutputProtocolError:
            pass
        else:
            raise AssertionError("prose outside the final envelope must be rejected")
