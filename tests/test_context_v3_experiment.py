from __future__ import annotations

import json

import pytest

from experiments.context_v3.client import ModelResponse
from experiments.context_v3.context import build_messages
from experiments.context_v3.long_scenario import build_scenario
from experiments.context_v3.long_run import score as score_long
from experiments.context_v3.production import build_ledger_history
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
                    "target": "private:3001",
                    "text": "answers/2026-08-04-climate.md",
                },
            },
        ),
        usage={},
    )

    assert score_long(case, response)["passed"]


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


@pytest.mark.asyncio
async def test_long_production_projection_keeps_current_private_lens_only() -> None:
    records, cases = build_scenario(8)
    history, stats = await build_ledger_history(records, cases[0])
    rendered = json.dumps(history, ensure_ascii=False)

    assert stats["compaction_calls"] == 0
    assert "ALICE-PRIVATE-77" in rendered
    assert "BOB-PRIVATE-29" not in rendered
    assert "CAROL-PRIVATE-51" not in rendered


def test_long_scenario_exposes_numeric_routing_ids_without_making_them_private_facts() -> None:
    records, _ = build_scenario(320)
    rendered = "\n".join(record.text for record in records)

    assert len(records) > 300
    assert "qq:user:3001" in rendered
    assert "qq:user:3002" in rendered
    assert "qq:user:3003" in rendered
    assert "CAROL-PRIVATE-51" in rendered


def test_long_group_answer_routes_to_the_current_actor_private_target() -> None:
    records, cases = build_scenario(8)
    case = next(item for item in cases if item.name == "group_answer_to_alice")
    response = ModelResponse(
        content="",
        tool_calls=(
            {
                "name": "send",
                "arguments": {
                    "target": "private:3001",
                    "artifact": "artifacts/atlas/2026-08-09/answer.md",
                },
            },
        ),
        usage={},
    )

    assert score(case, response, "formatted_llm")["passed"]
