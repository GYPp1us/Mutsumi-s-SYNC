from __future__ import annotations

from experiments.context_v3.client import ModelResponse
from experiments.context_v3.context import build_messages
from experiments.context_v3.run import score
from experiments.context_v3.scenario import CASES


def _case(name: str):
    return next(item for item in CASES if item.name == name)


def test_formatted_design_contains_all_roles_but_ledger_is_filtered() -> None:
    alice_case = _case("private_fact_recall")
    formatted = build_messages("formatted_llm", alice_case)[1]["content"]
    ledger = build_messages("ledger_projection", alice_case)[1]["content"]

    assert "ALICE-PRIVATE-17" in formatted
    assert "BOB-PRIVATE-29" in formatted
    assert "BOB-PRIVATE-29" not in ledger
    assert "OWNER-PRIVATE-42" not in ledger


def test_private_delivery_requires_target_and_never_group_target() -> None:
    case = _case("private_answer_delivery")
    response = ModelResponse(
        content="",
        tool_calls=(
            {
                "name": "send",
                "arguments": {
                    "target": "private:alice",
                    "text": "answers/2026-08-04-climate.md",
                },
            },
        ),
        usage={},
    )

    assert score(case, response, "formatted_llm")["passed"]


def test_group_delivery_rejects_answer_artifact() -> None:
    case = _case("group_delivery")
    response = ModelResponse(
        content="",
        tool_calls=(
            {
                "name": "send",
                "arguments": {
                    "target": "group:9001",
                    "text": "exercises/2026-08-04-climate.md",
                },
            },
        ),
        usage={},
    )

    assert score(case, response, "ledger_projection")["passed"]

    leaking = ModelResponse(
        content="",
        tool_calls=(
            {
                "name": "send",
                "arguments": {
                    "target": "group:9001",
                    "text": "answers/2026-08-04-climate.md",
                },
            },
        ),
        usage={},
    )
    assert not score(case, leaking, "ledger_projection")["passed"]


def test_worker_and_scheduler_require_natural_language_contract() -> None:
    delegate = _case("delegate_worker")
    delegate_response = ModelResponse(
        content="",
        tool_calls=(
            {
                "name": "delegate_task",
                "arguments": {
                    "prompt": "Read skills/nature-daily-cet4/SKILL.md; target group:9001; call report_task.",
                },
            },
        ),
        usage={},
    )
    assert score(delegate, delegate_response, "task_room_usc")["passed"]

    schedule = _case("schedule_registration")
    schedule_response = ModelResponse(
        content="",
        tool_calls=(
            {
                "name": "scheduler",
                "arguments": {
                    "when": "08:00",
                    "prompt": "Read skills/nature-daily-cet4/SKILL.md for group:9001.",
                    "context": "Private answer rule and report_task are required.",
                },
            },
        ),
        usage={},
    )
    assert score(schedule, schedule_response, "ledger_projection")["passed"]

