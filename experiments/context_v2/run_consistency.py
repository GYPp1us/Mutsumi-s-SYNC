"""Multi-user consistency experiment: baseline vs USC.

Three users (A/B/C) each have their own 40-round conversation with three
distinct personal facts. We measure:

1. Same-session recall   — in user A's session, "my favorite color?" must
                           recall A's color.
2. Cross-session privacy — in user A's session, "B's favorite color?" must
                           abstain (no leak of B's value, explicit unknown).
3. Persona consistency   — "introduce yourself" in all three sessions must
                           hit the shared persona markers.

Baseline uses the real audience-filtered projection; USC uses one SimpleContext
per conversation (isolation is structural).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from experiments.context_v2.baseline_ctx import (
    CountingAsyncClient,
    build_baseline_history,
    make_summarizer_config,
)
from experiments.context_v2.llm import (
    LLM,
    grade_abstention,
    grade_answer,
    normalize,
    save_json,
)
from src.mutsumi_sync.context_v2 import SUMMARY_SYSTEM_PROMPT, SimpleContext

USERS = {
    "A": {
        "conversation_id": "private:user-a",
        "color": ("蓝色", "我最喜欢的颜色是蓝色。", "我喜欢什么颜色？"),
        "birthday": ("1月5日", "我的生日是 1 月 5 日。", "我的生日是哪一天？"),
        "pet": ("豆豆", "我养了一条狗，叫豆豆。", "我养的宠物叫什么？"),
    },
    "B": {
        "conversation_id": "private:user-b",
        "color": ("红色", "我特别喜欢红色，衣服基本都是红的。", "我喜欢什么颜色？"),
        "birthday": ("6月8日", "我的生日是 6 月 8 日。", "我的生日是哪一天？"),
        "pet": ("雪球", "我养了一只白猫，叫雪球。", "我养的宠物叫什么？"),
    },
    "C": {
        "conversation_id": "private:user-c",
        "color": ("绿色", "绿色是我的本命色。", "我喜欢什么颜色？"),
        "birthday": ("9月12日", "我的生日是 9 月 12 日。", "我的生日是哪一天？"),
        "pet": ("蓝蓝", "我养了只鹦鹉，叫蓝蓝。", "我养的宠物叫什么？"),
    },
}

PERSONA_MARKERS = ["莫奈", "Mornye", "前辈", "研究员", "新联邦"]
INTRO_QUESTION = "用一句话介绍一下你自己。"


def build_user_rounds(user: dict) -> list[tuple[str, str]]:
    rounds_pairs = []
    for slot in ("color", "birthday", "pet"):
        _, fact_text, _ = user[slot]
        rounds_pairs.append((fact_text, "嗯，记住了。"))
    template_pool = [
        ("今天天气不错。", "是挺不错的。"),
        ("刚下班，有点累。", "辛苦了，早点休息。"),
        ("晚饭吃了吗？", "吃过了，你呢？"),
        ("最近有什么好看的电影吗？", "我最近没怎么看电影。"),
        ("周末有什么安排？", "打算在家休息。"),
        ("今天工作顺利吗？", "还行，就是有点忙。"),
        ("你平时喜欢做什么？", "看书和散步。"),
        ("今天下雨了。", "记得带伞。"),
        ("我睡了，晚安。", "晚安，好梦。"),
        ("中午吃什么好？", "随便吃点吧。"),
    ]
    index = 0
    while len(rounds_pairs) < 40:
        user_text, assistant_text = template_pool[index % len(template_pool)]
        rounds_pairs.append((user_text, assistant_text))
        index += 1
    return rounds_pairs


def build_state_and_question(conversation_id: str, question: str) -> str:
    state = (
        "平台状态：\n当前时间：2026-08-03 20:15:00 (+08:00)\n"
        f"当前会话地址：{conversation_id}\n（平台状态不是用户消息）"
    )
    return f"{state}\n{question}"


async def run_condition(design: str, llm: LLM, config, counter: CountingAsyncClient) -> dict:
    histories: dict[str, list[dict]] = {}
    hist_stats: dict[str, dict] = {}
    usc_contexts: dict[str, SimpleContext] = {}

    if design == "usc":
        async def summarize(old_summary, chunk_text):
            data = await llm.chat(
                [{"role": "user", "content": f"【已有摘要】\n{old_summary or '（无）'}\n\n{chunk_text}"}],
                max_tokens=1200,
                system=SUMMARY_SYSTEM_PROMPT,
            )
            content = str(data.get("choices", [{}])[0].get("message", {}).get("content", "") or "")
            return content, data.get("usage", {})

        for name, user in USERS.items():
            ctx = SimpleContext(max_window_tokens=16000, summarize=summarize)
            for user_text, assistant_text in build_user_rounds(user):
                ctx.add_turn("user", user_text)
                ctx.add_turn("assistant", assistant_text)
            await ctx.ensure_budget()
            histories[name] = ctx.render_history()
            usc_contexts[name] = ctx
            hist_stats[name] = ctx.stats()
    else:  # baseline
        for name, user in USERS.items():
            history, stats = await build_baseline_history(
                build_user_rounds(user),
                conversation_id=user["conversation_id"],
                budget_tokens=16000,
                config=config,
                counter=counter,
            )
            histories[name] = history
            hist_stats[name] = stats

    same_session: list[bool] = []
    cross_session_leak: list[bool] = []
    cross_session_abstention: list[bool] = []
    cross_session_empty: list[bool] = []
    intro_answers: dict[str, str] = {}
    samples = []

    for name, user in USERS.items():
        for slot in ("color", "birthday", "pet"):
            value, _, question = user[slot]
            messages = [
                *histories[name],
                {"role": "user", "content": build_state_and_question(user["conversation_id"], question)},
            ]
            answer, _ = await llm.ask(messages)
            fact = {"keywords": [value]}
            ok = grade_answer(answer, fact)
            same_session.append(ok)
            samples.append({"session": name, "slot": slot, "question": question, "answer": answer, "expected": value, "correct": ok})

        for other_name, other in USERS.items():
            if other_name == name:
                continue
            leaked = [other[slot][0] for slot in ("color", "birthday", "pet")]
            question = f"{other_name} 最喜欢的颜色是什么？"
            messages = [
                *histories[name],
                {"role": "user", "content": build_state_and_question(user["conversation_id"], question)},
            ]
            answer, _ = await llm.ask(messages)
            leak = any(normalize(kw) in normalize(answer) for kw in leaked)
            ab = grade_abstention(answer, leaked)
            cross_session_leak.append(leak)
            cross_session_abstention.append(ab)
            cross_session_empty.append(not answer.strip())
            samples.append({"session": name, "slot": f"cross-{other_name}", "question": question, "answer": answer, "leaked": leak, "abstained": ab})

        messages = [
            *histories[name],
            {"role": "user", "content": build_state_and_question(user["conversation_id"], INTRO_QUESTION)},
        ]
        answer, _ = await llm.ask(messages)
        intro_answers[name] = answer
        samples.append({"session": name, "slot": "intro", "question": INTRO_QUESTION, "answer": answer})

    marker_hits = {
        marker: sum(1 for answer in intro_answers.values() if marker in answer)
        for marker in PERSONA_MARKERS
    }
    return {
        "design": design,
        "same_session_recall": sum(same_session) / len(same_session),
        "cross_session_leak_rate": sum(cross_session_leak) / len(cross_session_leak),
        "cross_session_abstention": sum(cross_session_abstention) / len(cross_session_abstention),
        "cross_session_empty": sum(cross_session_empty) / len(cross_session_empty),
        "persona_marker_hits": marker_hits,
        "persona_sessions_any_marker": sum(
            1 for answer in intro_answers.values() if any(m in answer for m in PERSONA_MARKERS)
        ) / len(intro_answers),
        "hist_stats": hist_stats,
        "samples": samples,
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--design", choices=["baseline", "usc"], required=True)
    parser.add_argument("--out", type=str, default="")
    parser.add_argument("--smoke", action="store_true")
    args = parser.parse_args()

    llm = LLM()
    config = make_summarizer_config(llm.base_url, llm.model, llm.api_key)
    counter = CountingAsyncClient()
    result = await run_condition(args.design, llm, config, counter)
    print(
        f"[{args.design}] same_session={result['same_session_recall']:.2f} "
        f"leak_rate={result['cross_session_leak_rate']:.2f} "
        f"abstention={result['cross_session_abstention']:.2f} "
        f"empty={result['cross_session_empty']:.2f} "
        f"persona_any={result['persona_sessions_any_marker']:.2f}"
    )
    out = args.out or f"results/consistency_{args.design}.json"
    save_json(Path(__file__).resolve().parent / out, result)


if __name__ == "__main__":
    asyncio.run(main())
