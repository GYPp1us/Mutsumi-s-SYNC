"""Re-score a saved live run after adjusting only deterministic graders."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .client import ModelResponse
from .long_run import category, score
from .long_scenario import build_scenario


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input")
    parser.add_argument("--out", default="")
    parser.add_argument("--turns", type=int, default=320)
    args = parser.parse_args()
    source = Path(args.input)
    data = json.loads(source.read_text(encoding="utf-8"))
    _, cases = build_scenario(args.turns)
    by_name = {case.name: case for case in cases}
    for design_summary in data.get("designs", {}).values():
        rows = design_summary.get("results", [])
        for row in rows:
            case = by_name[row["case"]]
            old = row.get("score", {})
            response = ModelResponse(
                content=str(old.get("content", "") or ""),
                tool_calls=tuple(old.get("tool_calls", []) or []),
                usage=old.get("usage", {}) or {},
            )
            row["category"] = category(case)
            row["score"] = score(case, response)
        grouped: dict[str, list[bool]] = {}
        for row in rows:
            grouped.setdefault(row["category"], []).append(bool(row["score"]["passed"]))
        design_summary["pass_rate"] = sum(bool(row["score"]["passed"]) for row in rows) / max(1, len(rows))
        design_summary["category_rates"] = {
            name: sum(values) / len(values) for name, values in grouped.items()
        }
    destination = Path(args.out) if args.out else source.with_name(source.stem + "_regraded.json")
    destination.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[results] saved {destination}")


if __name__ == "__main__":
    main()
