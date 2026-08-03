"""Recall experiment: baseline (real projection) vs USC vs truncate control.

Usage:
    python experiments/context_v2/run_recall.py --rounds 1000 --design usc --budget 16000 --repeats 2 --out results/recall_1000_usc.json

Designs:
    baseline  — real Event Ledger + Episode compaction + Life Stream projection
    usc       — Unified Simple Context (summary + bounded window, write-time reclaim)
    truncate  — control: newest raw turns only, old turns silently dropped
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from experiments.context_v2.baseline_ctx import (
    CountingAsyncClient,
    build_baseline_history,
    make_summarizer_config,
)
from experiments.context_v2.data_gen import generate_conversation
from experiments.context_v2.llm import LLM, grade_answer, save_json
from src.mutsumi_sync.context_v2 import (
    SUMMARY_SYSTEM_PROMPT,
    SimpleContext,
    Turn,
    truncate_history_turns,
)
from src.mutsumi_sync.token_estimate import estimate_tokens

CONVERSATION_ID = "private:exp-user"


def build_truncate_history(
    rounds_pairs: list[tuple[str, str]], *, budget_tokens: int
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    turns = []
    for user, assistant in rounds_pairs:
        turns.append(Turn(role="user", content=user))
        turns.append(Turn(role="assistant", content=assistant))
    kept = truncate_history_turns(turns, budget_tokens)
    history = [{"role": turn.role, "content": turn.content} for turn in kept]
    return history, {
        "history_messages": len(history),
        "history_tokens": estimate_tokens(json.dumps(history, ensure_ascii=False, separators=(",", ":"))),
        "summary_calls": 0,
        "summary_input_tokens": 0,
        "summary_output_tokens": 0,
        "dropped_turns": len(turns) - len(kept),
    }


async def build_usc_history(
    rounds_pairs: list[tuple[str, str]],
    *,
    budget_tokens: int,
    llm: LLM,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    async def summarize(old_summary: str, chunk_text: str):
        data = await llm.chat(
            [{"role": "user", "content": f"【已有摘要】\n{old_summary or '（无）'}\n\n{chunk_text}"}],
            max_tokens=1200,
            system=SUMMARY_SYSTEM_PROMPT,
        )
        content = str(data.get("choices", [{}])[0].get("message", {}).get("content", "") or "")
        return content, data.get("usage", {})

    ctx = SimpleContext(max_window_tokens=budget_tokens, summarize=summarize, max_summary_tokens=2000)
    for user, assistant in rounds_pairs:
        ctx.add_turn("user", user)
        ctx.add_turn("assistant", assistant)
    folded = await ctx.ensure_budget()
    history = ctx.render_history()
    stats = ctx.stats()
    stats["folded_turns"] = folded
    stats["history_messages"] = len(history)
    stats["history_tokens"] = estimate_tokens(json.dumps(history, ensure_ascii=False, separators=(",", ":")))
    return history, stats


def build_qa_messages(history: list[dict], question: str) -> list[dict]:
    state = "平台状态：\n当前时间：2026-08-03 20:15:00 (+08:00)\n当前会话地址：private:exp-user\n（平台状态不是用户消息）"
    return [
        *history,
        {"role": "user", "content": f"{state}\n{question}"},
    ]


async def run_condition(
    *,
    rounds: int,
    budget: int,
    design: str,
    repeat: int,
    seed: int,
    llm: LLM,
    config,
    counter: CountingAsyncClient,
) -> dict:
    conv = generate_conversation(rounds=rounds, seed=seed)
    if design == "baseline":
        history, hist_stats = await build_baseline_history(
            conv.rounds,
            conversation_id=CONVERSATION_ID,
            budget_tokens=budget,
            config=config,
            counter=counter,
        )
    elif design == "usc":
        history, hist_stats = await build_usc_history(conv.rounds, budget_tokens=budget, llm=llm)
    elif design == "truncate":
        history, hist_stats = build_truncate_history(conv.rounds, budget_tokens=budget)
    else:
        raise ValueError(f"unknown design: {design}")

    qa_tokens = 0
    fact_results = []
    for fact in conv.facts:
        messages = build_qa_messages(history, fact["question"])
        answer, usage = await llm.ask(messages)
        qa_tokens += int(usage.get("prompt_tokens", 0) or 0)
        fact_results.append({
            "name": fact["name"],
            "depth": fact["depth"],
            "question": fact["question"],
            "answer": answer,
            "correct": grade_answer(answer, fact),
            "qa_input_tokens": int(usage.get("prompt_tokens", 0) or 0),
        })
    correct = sum(1 for item in fact_results if item["correct"])
    depths: dict[str, list[bool]] = {}
    for item in fact_results:
        depths.setdefault(item["depth"], []).append(item["correct"])
    return {
        "rounds": rounds,
        "budget": budget,
        "design": design,
        "repeat": repeat,
        "history_stats": hist_stats,
        "qa_avg_input_tokens": qa_tokens // max(1, len(fact_results)),
        "recall_overall": correct / max(1, len(fact_results)),
        "recall_by_depth": {k: sum(v) / len(v) for k, v in depths.items()},
        "facts": fact_results,
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, required=True)
    parser.add_argument("--design", choices=["baseline", "usc", "truncate"], required=True)
    parser.add_argument("--budget", type=int, default=16000)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--out", type=str, default="")
    parser.add_argument("--smoke", action="store_true")
    args = parser.parse_args()

    llm = LLM()
    config = make_summarizer_config(llm.base_url, llm.model, llm.api_key)
    counter = CountingAsyncClient()

    if args.smoke:
        args.repeats = 1
        args.rounds = 60

    results = []
    for repeat in range(args.repeats):
        seed = 100 + repeat * 7
        result = await run_condition(
            rounds=args.rounds,
            budget=args.budget,
            design=args.design,
            repeat=repeat,
            seed=seed,
            llm=llm,
            config=config,
            counter=counter,
        )
        results.append(result)
        print(
            f"[{args.design} r={args.rounds} b={args.budget}] repeat={repeat} "
            f"recall={result['recall_overall']:.2f} by_depth={result['recall_by_depth']} "
            f"hist_tokens={result['history_stats'].get('history_tokens')} "
            f"sum_calls={result['history_stats'].get('summary_calls', 0)}"
        )

    n = len(results)
    overall = sum(r["recall_overall"] for r in results) / n
    by_depth = {}
    for depth in ("early", "mid", "late"):
        values = [r["recall_by_depth"].get(depth, 0.0) for r in results]
        by_depth[depth] = sum(values) / max(1, len(values))
    summary = {
        "rounds": args.rounds,
        "budget": args.budget,
        "design": args.design,
        "repeats": args.repeats,
        "recall_overall": overall,
        "recall_by_depth": by_depth,
        "qa_avg_input_tokens": sum(r["qa_avg_input_tokens"] for r in results) / n,
        "summarization_calls": sum(r["history_stats"].get("summary_calls", 0) for r in results) / n,
        "summarization_input_tokens": sum(r["history_stats"].get("summary_input_tokens", 0) for r in results) / n,
        "history_tokens": sum(r["history_stats"].get("history_tokens", 0) for r in results) / n,
        "per_repeat": results,
    }
    out = args.out or f"results/recall_r{args.rounds}_{args.design}_b{args.budget}.json"
    save_json(Path(__file__).resolve().parent / out, summary)
    print(f"[summary] overall={overall:.2f} by_depth={by_depth}")


if __name__ == "__main__":
    asyncio.run(main())
