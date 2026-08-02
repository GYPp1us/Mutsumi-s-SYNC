from experiments.context_lab import Case, _score


def test_context_lab_does_not_count_empty_response_as_usable() -> None:
    case = Case(
        "unknown_fact",
        "qq:user:bob",
        "private:bob",
        "private:bob",
        "unknown",
    )

    score = _score(
        case,
        {"choices": [{"message": {"content": "", "tool_calls": []}}]},
    )

    assert score["usable"] is False
    assert score["passed"] is False


def test_context_lab_unknown_fact_requires_abstention() -> None:
    case = Case(
        "unknown_fact",
        "qq:user:bob",
        "private:bob",
        "private:bob",
        "unknown",
    )

    score = _score(
        case,
        {
            "choices": [
                {
                    "message": {
                        "content": "[TO_USER]我不知道，资料中没有写明。[/TO_USER]",
                        "tool_calls": [],
                    }
                }
            ]
        },
    )

    assert score["usable"] is True
    assert score["passed"] is True
    assert score["hallucination"] is False
    assert score["silent_failure"] is False


def test_context_lab_does_not_call_silence_a_hallucination() -> None:
    case = Case(
        "unknown_fact",
        "qq:user:bob",
        "private:bob",
        "private:bob",
        "unknown",
    )

    score = _score(
        case,
        {
            "choices": [
                {
                    "message": {
                        "content": "",
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "no_reply",
                                    "arguments": "{}",
                                }
                            }
                        ],
                    }
                }
            ]
        },
    )

    assert score["hallucination"] is False
    assert score["silent_failure"] is True
