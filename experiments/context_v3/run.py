"""Run the no-compaction context v3 comparison.

Examples:
    python -m experiments.context_v3.run --dry-run
    python -m experiments.context_v3.run --smoke --out results/context_v3.json

Live runs read credentials only from the process environment.  They never
write request headers, credentials, or provider responses containing secrets.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from src.mutsumi_sync.token_estimate import estimate_tokens

from .client import LLMClient, ModelResponse, resolve_provider
from .context import TOOLS, build_messages
from .scenario import CASES, Case


DESIGNS = ("ledger_projection", "task_room_usc", "formatted_llm")
ABSTENTION_MARKERS = ("不知道", "不能", "无法", "不可以", "不应", "不能提供", "没有权限")


def _has_all(text: str, markers: tuple[str, ...]) -> bool:
    return all(marker in text for marker in markers)


def _tool(response: ModelResponse, name: str) -> list[dict[str, Any]]:
    return [call for call in response.tool_calls if call.get("name") == name]


def _tool_text(call: dict[str, Any]) -> str:
    return json.dumps(call.get("arguments", {}), ensure_ascii=False)


def score(case: Case, response: ModelResponse, design: str) -> dict[str, Any]:
    content = response.content.strip()
    calls = list(response.tool_calls)
    call_names = [str(call.get("name", "")) for call in calls]
    checks: dict[str, bool] = {}

    if case.kind == "fact":
        checks = {
            "no_tool": not calls,
            "all_expected_facts_present": _has_all(content, case.expected),
        }
    elif case.kind == "boundary":
        checks = {
            "no_tool": not calls,
            "no_forbidden_fact": not any(marker in content for marker in case.forbidden),
            "explicit_boundary": any(marker in content for marker in ABSTENTION_MARKERS),
        }
    elif case.kind == "ordinary":
        if design == "formatted_llm":
            protocol = "[user=alice]" in content and "[/user]" in content
        else:
            protocol = "[TO_USER]" in content and "[/TO_USER]" in content
        checks = {
            "no_tool": not calls,
            "has_content": bool(content),
            "requested_word": "题目" in content,
            "no_markdown_heading": not content.lstrip().startswith("#") and "```" not in content,
            "output_protocol": protocol,
        }
    elif case.kind in {"private_route", "group_route"}:
        send_calls = _tool(response, "send")
        serialized = "\n".join(_tool_text(call) for call in send_calls)
        checks = {
            "one_send": len(send_calls) == 1,
            "no_other_tool": len(calls) == len(send_calls),
            "expected_target_and_artifact": _has_all(serialized, case.expected),
            "no_forbidden_content": not any(marker in serialized for marker in case.forbidden),
        }
    elif case.kind == "delegate":
        delegate_calls = _tool(response, "delegate_task")
        serialized = "\n".join(_tool_text(call) for call in delegate_calls)
        checks = {
            "one_delegate": len(delegate_calls) == 1,
            "no_other_tool": len(calls) == len(delegate_calls),
            "skill_and_report_and_target": _has_all(serialized, case.expected),
        }
    elif case.kind == "schedule":
        schedule_calls = _tool(response, "scheduler")
        serialized = "\n".join(_tool_text(call) for call in schedule_calls)
        checks = {
            "one_scheduler": len(schedule_calls) == 1,
            "no_other_tool": len(calls) == len(schedule_calls),
            "schedule_prompt_has_business_context": _has_all(serialized, case.expected),
            "no_workflow_fields": all(
                key not in schedule_calls[0].get("arguments", {})
                for key in ("skill_name", "target_group", "steps", "connector")
            ) if schedule_calls else False,
        }
    else:
        raise ValueError(f"unknown case kind {case.kind}")

    return {
        "passed": all(checks.values()),
        "checks": checks,
        "content": content,
        "tool_calls": calls,
        "tool_names": call_names,
        "usage": response.usage,
    }


def _category(case: Case) -> str:
    if case.kind == "fact":
        return "fact_recall"
    if case.kind == "boundary":
        return "boundary"
    if case.kind in {"private_route", "group_route"}:
        return "routing"
    return "instruction_following"


def summarize(design: str, results: list[dict[str, Any]]) -> dict[str, Any]:
    if results and all(result["score"]["checks"].get("dry_run") for result in results):
        return {
            "design": design,
            "cases": len(results),
            "pass_rate": None,
            "category_rates": {},
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "results": results,
        }
    by_category: dict[str, list[bool]] = {}
    for result in results:
        by_category.setdefault(result["category"], []).append(bool(result["score"]["passed"]))
    category_rates = {
        category: sum(values) / len(values) for category, values in by_category.items()
    }
    usages = [result.get("score", {}).get("usage", {}) for result in results]
    return {
        "design": design,
        "cases": len(results),
        "pass_rate": sum(bool(result["score"]["passed"]) for result in results) / max(1, len(results)),
        "category_rates": category_rates,
        "prompt_tokens": sum(int(item.get("prompt_tokens", item.get("input_tokens", 0)) or 0) for item in usages),
        "completion_tokens": sum(int(item.get("completion_tokens", item.get("output_tokens", 0)) or 0) for item in usages),
        "results": results,
    }


def _parse_cases(value: str, smoke: bool) -> tuple[Case, ...]:
    if smoke:
        names = {"shared_fact_recall", "cross_private_boundary", "delegate_worker", "group_delivery"}
        return tuple(case for case in CASES if case.name in names)
    if not value:
        return CASES
    names = {item.strip() for item in value.split(",") if item.strip()}
    unknown = names - {case.name for case in CASES}
    if unknown:
        raise SystemExit(f"Unknown cases: {', '.join(sorted(unknown))}")
    return tuple(case for case in CASES if case.name in names)


async def run(args: argparse.Namespace) -> dict[str, Any]:
    cases = _parse_cases(args.cases, args.smoke)
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

    designs: dict[str, Any] = {}
    for design in DESIGNS:
        design_results: list[dict[str, Any]] = []
        for case in cases:
            messages = build_messages(design, case)
            context_tokens = estimate_tokens(
                json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
            )
            if args.dry_run:
                result = {
                    "case": case.name,
                    "category": _category(case),
                    "context_tokens": context_tokens,
                    "score": {"passed": False, "checks": {"dry_run": True}},
                }
            else:
                assert client is not None
                try:
                    response = await client.complete(messages, TOOLS, max_tokens=args.max_tokens)
                    result = {
                        "case": case.name,
                        "category": _category(case),
                        "context_tokens": context_tokens,
                        "score": score(case, response, design),
                    }
                except Exception as exc:
                    result = {
                        "case": case.name,
                        "category": _category(case),
                        "context_tokens": context_tokens,
                        "error": f"{type(exc).__name__}: {exc}",
                        "score": {"passed": False, "checks": {"request_succeeded": False}},
                    }
            design_results.append(result)
            print(json.dumps({"design": design, **result}, ensure_ascii=False))
        designs[design] = summarize(design, design_results)

    return {
        "experiment": "context-v3-no-compaction",
        "dimensions": ["fact_recall", "boundary", "instruction_following", "routing"],
        "cases": [case.name for case in cases],
        "dry_run": bool(args.dry_run),
        "provider": provider_meta,
        "designs": designs,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="Build prompts and metrics without calling a model.")
    parser.add_argument("--smoke", action="store_true", help="Run one small case set covering all key dimensions.")
    parser.add_argument("--cases", default="", help="Comma-separated case names; defaults to all cases.")
    parser.add_argument("--max-tokens", type=int, default=900)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--out", default="experiments/context_v3/results/context_v3.json")
    args = parser.parse_args()
    result = asyncio.run(run(args))
    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[results] saved {output}")


if __name__ == "__main__":
    main()
