from __future__ import annotations

import argparse
from pathlib import Path
import shutil

import yaml


OPERATIONAL_KEYS = (
    "runtime",
    "message_summary",
    "summary_merge",
    "episode_summary",
    "heartbeat",
)
VERSIONED_KEYS = ("persona", *OPERATIONAL_KEYS)


def _load_mapping(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"system prompt file must contain a mapping: {path}")
    return data


def sync_system_prompts(shared_path: Path, release_path: Path, backup_tag: str) -> Path | None:
    """Atomically refresh all versioned prompts while preserving extra operator keys."""
    release_data = _load_mapping(release_path)
    missing = [key for key in VERSIONED_KEYS if not str(release_data.get(key) or "").strip()]
    if missing:
        raise ValueError(f"release system prompts missing required fields: {', '.join(missing)}")

    shared_data = _load_mapping(shared_path) if shared_path.exists() else {}
    candidate = {
        "persona": release_data.get("persona", ""),
        **{key: release_data[key] for key in OPERATIONAL_KEYS},
    }
    for key, value in shared_data.items():
        if key not in candidate:
            candidate[key] = value

    if shared_data == candidate:
        return None

    backup: Path | None = None
    if shared_path.exists():
        backup = shared_path.with_name(f"{shared_path.name}.bak-{backup_tag}")
        shutil.copy2(shared_path, backup)

    shared_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = shared_path.with_name(f"{shared_path.name}.tmp")
    temporary.write_text(
        yaml.safe_dump(candidate, allow_unicode=True, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )
    temporary.replace(shared_path)
    return backup


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("shared_path", type=Path)
    parser.add_argument("release_path", type=Path)
    parser.add_argument("backup_tag")
    args = parser.parse_args()

    shared_existed = args.shared_path.exists()
    backup = sync_system_prompts(args.shared_path, args.release_path, args.backup_tag)
    if backup is not None:
        print(f"synchronized shared system prompts; backup={backup}")
    elif shared_existed:
        print("shared system prompts already current")
    else:
        print("created shared system prompts from release defaults")


if __name__ == "__main__":
    main()
