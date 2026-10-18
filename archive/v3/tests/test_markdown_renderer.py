from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.mutsumi_sync.config import Config
from src.mutsumi_sync.tools.markdown_renderer import render_markdown_image


def test_template_prefers_computer_modern_family_for_latin_text():
    css = Path("tools/markdown-renderer/template.css").read_text(encoding="utf-8")

    assert '"KaTeX_Main"' in css
    assert '"KaTeX_Math"' in css
    assert '"KaTeX_AMS"' in css
    assert '"Noto Serif CJK SC"' in css


async def test_render_markdown_image_requires_enabled_config():
    config = Config()

    with pytest.raises(RuntimeError, match="markdown image renderer is disabled"):
        await render_markdown_image("# Hello", config=config)


async def test_render_markdown_image_calls_node_renderer(tmp_path, monkeypatch):
    config = Config()
    config.render.markdown_image.enabled = True
    config.render.markdown_image.node_path = "node-test"
    config.render.markdown_image.script_path = "renderer.mjs"
    config.render.markdown_image.output_dir = str(tmp_path)
    config.render.markdown_image.timeout_seconds = 3
    output = tmp_path / "out.png"
    calls: list[dict] = []

    async def fake_run(node_path: str, script_path: str, payload_json: str, timeout: float) -> str:
        calls.append({
            "node_path": node_path,
            "script_path": script_path,
            "payload_json": payload_json,
            "timeout": timeout,
        })
        return json.dumps({"ok": True, "file": str(output)})

    monkeypatch.setattr("src.mutsumi_sync.tools.markdown_renderer._run_renderer_process", fake_run)

    result = await render_markdown_image("# Hello", config=config)

    assert result == str(output)
    assert calls[0]["node_path"] == "node-test"
    assert calls[0]["script_path"] == "renderer.mjs"
    assert calls[0]["timeout"] == 3
    payload = json.loads(calls[0]["payload_json"])
    assert payload["markdown"] == "# Hello"
    assert payload["outputDir"] == str(tmp_path)
    assert payload["viewportWidth"] == 960
