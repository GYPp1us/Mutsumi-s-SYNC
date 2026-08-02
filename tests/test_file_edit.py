from __future__ import annotations

from src.mutsumi_sync.config import Config
from src.mutsumi_sync.tools.file_edit import file_edit_tool
import pytest


async def test_file_edit_supports_read_write_patch_and_list(tmp_path) -> None:
    config = Config()
    config.file_tools.root = str(tmp_path)

    assert "wrote" in await file_edit_tool(
        {"operation": "write", "path": "demo/BOT.md", "content": "hello\nworld\n"},
        config=config,
    )
    read = await file_edit_tool({"operation": "read", "path": "demo/BOT.md"}, config=config)
    assert "hello\nworld" in read

    patched = await file_edit_tool(
        {"operation": "patch", "path": "demo/BOT.md", "old": "world", "new": "skill"},
        config=config,
    )
    assert "patched" in patched
    assert "skill" in (tmp_path / "demo" / "BOT.md").read_text(encoding="utf-8")

    listing = await file_edit_tool(
        {"operation": "list", "path": ".", "recursive": True},
        config=config,
    )
    assert "demo/BOT.md" in listing


async def test_file_edit_rejects_escape_and_protected_paths(tmp_path) -> None:
    config = Config()
    config.file_tools.root = str(tmp_path)

    escaped = await file_edit_tool(
        {"operation": "read", "path": "../outside.txt"},
        config=config,
    )
    protected = await file_edit_tool(
        {"operation": "write", "path": ".env", "content": "SECRET"},
        config=config,
    )

    assert escaped.startswith("[Error:")
    assert protected.startswith("[Error:")


async def test_file_edit_rejects_ambiguous_patch(tmp_path) -> None:
    config = Config()
    config.file_tools.root = str(tmp_path)
    await file_edit_tool(
        {"operation": "write", "path": "note.md", "content": "same\nsame\n"},
        config=config,
    )

    result = await file_edit_tool(
        {"operation": "patch", "path": "note.md", "old": "same", "new": "changed"},
        config=config,
    )
    assert "ambiguous" in result


async def test_file_edit_enforces_read_and_write_limits(tmp_path) -> None:
    config = Config()
    config.file_tools.root = str(tmp_path)
    config.file_tools.max_read_bytes = 4
    config.file_tools.max_write_bytes = 4

    oversized_write = await file_edit_tool(
        {"operation": "write", "path": "large.md", "content": "12345"},
        config=config,
    )
    assert "exceeds" in oversized_write

    (tmp_path / "large.md").write_text("12345", encoding="utf-8")
    oversized_read = await file_edit_tool(
        {"operation": "read", "path": "large.md"},
        config=config,
    )
    assert "exceeds" in oversized_read

    oversized_patch = await file_edit_tool(
        {"operation": "patch", "path": "large.md", "old": "1", "new": "2"},
        config=config,
    )
    assert "exceeds" in oversized_patch


async def test_file_edit_rejects_symlink_escape(tmp_path) -> None:
    config = Config()
    config.file_tools.root = str(tmp_path / "skills")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("secret", encoding="utf-8")
    (tmp_path / "skills").mkdir()
    link = tmp_path / "skills" / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable on this platform")

    result = await file_edit_tool(
        {"operation": "read", "path": "link/secret.md"},
        config=config,
    )
    assert result.startswith("[Error:")
