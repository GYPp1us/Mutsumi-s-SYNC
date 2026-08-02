from __future__ import annotations

import os
from pathlib import Path
import tempfile
from typing import Any

from ..config import resolve_runtime_path


FILE_EDIT_SCHEMA = {
    "type": "object",
    "properties": {
        "operation": {
            "type": "string",
            "enum": ["list", "read", "write", "patch"],
            "description": "The file operation to perform.",
        },
        "path": {
            "type": "string",
            "description": "Path relative to the configured skills/file workspace.",
        },
        "content": {
            "type": "string",
            "description": "Complete UTF-8 content for write.",
        },
        "old": {
            "type": "string",
            "description": "Exact existing text for patch.",
        },
        "new": {
            "type": "string",
            "description": "Replacement text for patch.",
        },
        "replace_all": {
            "type": "boolean",
            "description": "For patch, replace every exact occurrence instead of the first.",
        },
        "recursive": {
            "type": "boolean",
            "description": "For list, include descendants recursively.",
        },
    },
    "required": ["operation"],
    "additionalProperties": False,
}


_PROTECTED_NAMES = {
    ".git",
    ".env",
    "config.yaml",
    "system-prompts.yaml",
    "node_modules",
    "data",
}


def _workspace_root(config: Any) -> Path:
    configured = Path(str(config.file_tools.root or "skills")).expanduser()
    config_path = getattr(config, "_config_path", None)
    if configured.is_absolute():
        return configured.resolve()
    return resolve_runtime_path(str(configured), config_path)


def _safe_path(config: Any, raw_path: str, *, allow_root: bool = True) -> tuple[Path, Path]:
    root = _workspace_root(config)
    candidate_text = str(raw_path or ".").strip()
    if Path(candidate_text).is_absolute():
        raise ValueError("path must be relative to the configured file workspace")
    candidate = (root / candidate_text).resolve()
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError("path escapes the configured file workspace") from exc

    parts = {part.lower() for part in relative.parts}
    if parts & {name.lower() for name in _PROTECTED_NAMES}:
        raise ValueError("path is protected")
    if not allow_root and candidate == root:
        raise ValueError("a file path is required")
    return root, candidate


def _limit_bytes(config: Any, value: str, *, write: bool) -> None:
    limit = int(
        config.file_tools.max_write_bytes
        if write
        else config.file_tools.max_read_bytes
    )
    if limit <= 0:
        raise ValueError("configured file size limit must be positive")
    if len(value.encode("utf-8")) > limit:
        kind = "write" if write else "read"
        raise ValueError(f"{kind} exceeds the configured {limit}-byte limit")


def _read_limited(path: Path, limit: int) -> str:
    if limit <= 0:
        raise ValueError("configured read file size limit must be positive")
    with path.open("rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"read exceeds the configured {limit}-byte limit")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("file is not valid UTF-8") from exc


def _relative(root: Path, path: Path) -> str:
    rel = path.relative_to(root).as_posix()
    return rel or "."


def _list_files(config: Any, path: Path, recursive: bool) -> str:
    if not path.exists():
        raise ValueError("directory does not exist")
    if not path.is_dir():
        raise ValueError("list requires a directory")

    entries: list[str] = []
    iterator = path.rglob("*") if recursive else path.iterdir()
    for item in sorted(iterator, key=lambda value: value.as_posix()):
        try:
            _safe_path(config, _relative(_workspace_root(config), item))
        except ValueError:
            continue
        label = _relative(_workspace_root(config), item)
        if item.is_dir():
            label += "/"
        entries.append(label)
        if len(entries) >= 500:
            break
    if not entries:
        return "[OK] empty directory"
    suffix = "\n[truncated after 500 entries]" if len(entries) >= 500 else ""
    return "[OK] files:\n" + "\n".join(entries) + suffix


def _write_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


async def file_edit_tool(args: dict[str, Any], *, config, **_: Any) -> str:
    """List, read, write, or exact-patch files in the configured workspace."""
    if not config.file_tools.enabled:
        return "[Error: file tool is disabled]"

    operation = str(args.get("operation") or "").strip().lower()
    if operation not in {"list", "read", "write", "patch"}:
        return "[Error: operation must be list, read, write, or patch]"

    try:
        root, path = _safe_path(config, str(args.get("path") or "."), allow_root=operation != "write")
        if operation == "list":
            return _list_files(config, path, bool(args.get("recursive", False)))

        if operation == "read":
            if not path.is_file():
                raise ValueError("read requires an existing file")
            content = _read_limited(path, int(config.file_tools.max_read_bytes))
            return f"[OK] path={_relative(root, path)}\n{content}"

        if operation == "write":
            content = args.get("content")
            if not isinstance(content, str):
                raise ValueError("content is required for write")
            _limit_bytes(config, content, write=True)
            _write_atomic(path, content)
            return f"[OK] wrote path={_relative(root, path)} bytes={len(content.encode('utf-8'))}"

        old = args.get("old")
        new = args.get("new")
        if not isinstance(old, str) or not isinstance(new, str):
            raise ValueError("old and new are required for patch")
        if not old:
            raise ValueError("old must not be empty for patch")
        if not path.is_file():
            raise ValueError("patch requires an existing file")
        content = _read_limited(path, int(config.file_tools.max_read_bytes))
        count = content.count(old)
        if count == 0:
            raise ValueError("patch text was not found")
        if count > 1 and not bool(args.get("replace_all", False)):
            raise ValueError("patch text is ambiguous; set replace_all or make old more specific")
        replacement = content.replace(old, new) if args.get("replace_all") else content.replace(old, new, 1)
        _limit_bytes(config, replacement, write=True)
        _write_atomic(path, replacement)
        return f"[OK] patched path={_relative(root, path)} replacements={count if args.get('replace_all') else 1}"
    except (OSError, UnicodeError, ValueError) as exc:
        return f"[Error: {exc}]"
