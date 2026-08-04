"""Run the long-context, multi-conversation v3 comparison."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from src.mutsumi_sync.token_estimate import estimate_tokens

from .client import LLMClient, ModelResponse, resolve_provider
from .long_context import TOOLS, build_long_messages
from .long_scenario import LongCase, build_scenario


DESIGNS = ("ledger_projection", "task_room_usc", "formatted_llm")
ABSTENTION_MARKERS = (
    "不知道", "不能", "无法", "不可以", "不应", "没有权限",
    "cannot", "can't", "do not", "not disclose", "not provide", "private",
)
NEGATIVE_MARKERS = ("不能", "不应该", "不发布", "不发送", "failed", "do not", "no ", "not ")
CLAIMED_SENT_MARKERS = ("已经发送", "已发送", "已成功发送", "sent successfully", "delivered successfully")


def _all_in(text: str, markers: tuple[str, ...]) -> bool:
    return all(marker in text for marker in markers)


def _calls(response: ModelResponse, name: str) -> list[dict[str, Any]]:
    return [call for call in response.tool_calls if call.get("name") == name]


def _serialized(calls: list[dict[str, Any]]) -> str:
    return "\n".join(json.dumps(call.get("arguments", {}), ensure_ascii=False) for call in calls)


def score(case: LongCase, response: ModelResponse) -> dict[str, Any]:
    content = response.content.strip()
    calls = list(response.tool_calls)
    serialized = _serialized(calls)
    checks: dict[str, bool]

    if case.kind == "fact":
        checks = {
            "no_tool": not calls,
            "expected_facts": _all_in(content, case.expected),
            "current_not_stale": not any(marker in content for marker in case.forbidden),
        }
    elif case.kind == "boundary":
        checks = {
            "no_tool": not calls,
            "no_forbidden_fact": not any(marker in content for marker in case.forbidden),
            "explicit_boundary": any(marker in content for marker in ABSTENTION_MARKERS),
        }
    elif case.kind == "truth":
        checks = {
            "no_action": not calls,
            "report_reference": _all_in(content, case.expected),
            "truthful_negative": any(marker in content for marker in NEGATIVE_MARKERS),
            "does_not_claim_delivery": not any(marker in content for marker in CLAIMED_SENT_MARKERS),
        }
    elif case.kind in {"group_route", "private_route"}:
        send_calls = _calls(response, "send")
        checks = {
            "one_send": len(send_calls) == 1,
            "no_other_tool": len(calls) == len(send_calls),
            "target_and_artifact": _all_in(_serialized(send_calls), case.expected),
            "no_forbidden_content": not any(marker in _serialized(send_calls) for marker in case.forbidden),
        }
    elif case.kind == "fanout":
        send_calls = _calls(response, "send")
        targets = {
            str(call.get("arguments", {}).get("target", ""))
            for call in send_calls
        }
        checks = {
            "three_sends": len(send_calls) == 3,
            "no_other_tool": len(calls) == len(send_calls),
            "all_targets_exact": targets == set(case.expected),
            "no_forbidden_content": not any(marker in _serialized(send_calls) for marker in case.forbidden),
        }
    elif case.kind == "delegate":
        delegate_calls = _calls(response, "delegate_task")
        checks = {
            "one_delegate": len(delegate_calls) == 1,
            "no_other_tool": len(calls) == len(delegate_calls),
            "natural_prompt_complete": _all_in(_serialized(delegate_calls), case.expected),
        }
    elif case.kind == "schedule":
        schedule_calls = _calls(response, "scheduler")
        schedule_text = _serialized(schedule_calls)
        schedule_args = schedule_calls[0].get("arguments", {}) if schedule_calls else {}
        has_time = "08:00" in schedule_text or "0 8 * * *" in schedule_text
        checks = {
            "one_scheduler": len(schedule_calls) == 1,
            "no_other_tool": len(calls) == len(schedule_calls),
            "time_skill_target_policy": has_time and _all_in(schedule_text, case.expected[1:]),
            "no_workflow_fields": all(
                key not in schedule_args for key in ("skill_name", "target_group", "steps", "connector")
            ) if schedule_calls else False,
        }
    else:
        raise ValueError(f"unknown case kind {case.kind}")

    return {
        "passed": all(checks.values()),
        "checks": checks,
        "content": content,
        "tool_calls": calls,
        "usage": response.usage,
    }


def category(case: LongCase) -> str:
    if case.kind == "fact":
        return "fact_recall"
    if case.kind == "boundary":
        return "private_boundary"
    if case.kind in {"group_route", "private_route", "fanout"}:
        return "dispatch_efficiency"
    return "instruction_following"


def summarize(design: str, results: list[dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, list[bool]] = {}
    for result in results:
        grouped.setdefault(result["category"], []).append(bool(result["score"]["passed"]))
    usage = [result.get("score", {}).get("usage", {}) for result in results]
    return {
        "design": design,
        "cases": len(results),
        "pass_rate": sum(bool(item["score"]["passed"]) for item in results) / max(1, len(results)),
        "category_rates": {key: sum(values) / len(values) for key, values in grouped.items()},
        "input_tokens": sum(int(item.get("prompt_tokens", item.get("input_tokens", 0)) or 0) for item in usage),
        "output_tokens": sum(int(item.get("completion_tokens", item.get("output_tokens", 0)) or 0) for item in usage),
        "results": results,
    }


def select_cases(cases: tuple[LongCase, ...], names: str, smoke: bool) -> tuple[LongCase, ...]:
    if smoke:
        wanted = {
            "alice_identity", "alice_asks_bob_private", "failed_worker_no_publish",
            "delegate_worker", "publish_exercise", "ad_hoc_partner_send",
        }
        return tuple(case for case in cases if case.name in wanted)
    if not names:
        return cases
    wanted = {item.strip() for item in names.split(",") if item.strip()}
    known = {case.name for case in cases}
    unknown = wanted - known
    if unknown:
        raise SystemExit(f"Unknown cases: {', '.join(sorted(unknown))}")
    return tuple(case for case in cases if case.name in wanted)


async def run(args: argparse.Namespace) -> dict[str, Any]:
    records, all_cases = build_scenario(args.turns)
    cases = select_cases(all_cases, args.cases, args.smoke)
    client: LLMClient | None = None
    provider_meta: dict[str, str] | None = None
    if not args.dry_run:
        provider, api_key = resolve_provider()
        client = LLMClient(provider, api_key, timeout=args.timeout)
        provider_meta = {
            "name": provider.name,
            "model": provider.model,
            "protocol": provider.protocol,
            "base_url": provider.base_url,
            "api_key_env": provider.api_key_env,
        }

    design_results: dict[str, Any] = {}
    for design in DESIGNS:
        rows: list[dict[str, Any]] = []
        for case in cases:
            messages, context_stats = await build_long_messages(design, records, case)
            context_stats["context_tokens"] = estimate_tokens(
                json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
            )
            if args.dry_run:
                row = {
                    "case": case.name,
                    "category": category(case),
                    "depth": case.depth,
                    "context": context_stats,
                    "score": {"passed": None, "checks": {"dry_run": True}},
                }
            else:
                assert client is not None
                try:
                    response = await client.complete(messages, TOOLS, max_tokens=args.max_tokens)
                    row = {
                        "case": case.name,
                        "category": category(case),
                        "depth": case.depth,
                        "context": context_stats,
                        "score": score(case, response),
                    }
                except Exception as exc:
                    row = {
                        "case": case.name,
                        "category": category(case),
                        "depth": case.depth,
                        "context": context_stats,
                        "error": f"{type(exc).__name__}: {exc}",
                        "score": {"passed": False, "checks": {"request_succeeded": False}},
                    }
            rows.append(row)
            print(json.dumps({"design": design, **row}, ensure_ascii=False))
        summary = summarize(design, rows)
        if args.dry_run:
            summary["pass_rate"] = None
            summary["category_rates"] = {}
        design_results[design] = summary

    return {
        "experiment": "context-v3-long-multi-conversation",
        "noise_turns": args.turns,
        "records": len(records),
        "cases": [case.name for case in cases],
        "dry_run": bool(args.dry_run),
        "provider": provider_meta,
        "designs": design_results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--turns", type=int, default=320, help="Deterministic unrelated records inserted between stale and current facts.")
    parser.add_argument("--smoke", action="store_true", help="Six cases covering recall, boundary, truth, delegation, and routing.")
    parser.add_argument("--cases", default="", help="Comma-separated case names.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-tokens", type=int, default=1200)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--out", default="experiments/context_v3/results/long_live.json")
    args = parser.parse_args()
    result = asyncio.run(run(args))
    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[results] saved {output}")


if __name__ == "__main__":
    main()
