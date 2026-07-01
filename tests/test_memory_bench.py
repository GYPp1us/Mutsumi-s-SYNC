from src.mutsumi_sync.lab.memory_bench import build_memory_pressure_steps


def test_memory_pressure_steps_include_growth_and_recall():
    steps = build_memory_pressure_steps(rounds=3, chunk_tokens=64)
    assert len(steps) > 3
    assert any(step.kind == "anchor_fact" for step in steps)
    assert any(step.kind == "memory_probe" for step in steps)
    assert any(step.kind == "context_flood" for step in steps)
    probe = next(step for step in steps if step.kind == "memory_probe")
    assert "send tool" in probe.text
