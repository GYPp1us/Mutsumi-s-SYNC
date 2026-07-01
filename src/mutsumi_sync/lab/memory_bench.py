from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MemoryPressureStep:
    kind: str
    text: str


ANCHOR_FACTS = [
    "My codename is Silver Lantern.",
    "My favorite debugging snack is frozen mango.",
    "The project keyword is blue-harbor-73.",
    "I prefer concise answers with gentle warmth.",
]


def _flood_text(round_index: int, chunk_tokens: int) -> str:
    sentence = (
        f"noise block {round_index}: this is filler context for memory pressure testing. "
        "It should be harmless, repetitive, and easy to compress. "
    )
    return (sentence * max(1, chunk_tokens // 16)).strip()


def build_memory_pressure_steps(rounds: int, chunk_tokens: int) -> list[MemoryPressureStep]:
    steps: list[MemoryPressureStep] = []
    for fact in ANCHOR_FACTS:
        steps.append(MemoryPressureStep(kind="anchor_fact", text=f"Please remember this stable fact: {fact}"))

    for i in range(rounds):
        steps.append(
            MemoryPressureStep(
                kind="context_flood",
                text=_flood_text(i, chunk_tokens),
            )
        )
        if i % 2 == 0:
            steps.append(
                MemoryPressureStep(
                    kind="memory_probe",
                    text=(
                        "Before answering anything else, use your available memory/context and tell me "
                        "the stable facts you still remember about me. Reply by calling the send tool."
                    ),
                )
            )
    return steps
