from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ..config import Config


async def render_markdown_image(markdown: str, *, config: Config) -> str:
    render_config = config.render.markdown_image
    if not render_config.enabled:
        raise RuntimeError("markdown image renderer is disabled")
    if not markdown.strip():
        raise RuntimeError("markdown image content is empty")

    output_dir = Path(render_config.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "markdown": markdown,
        "outputDir": str(output_dir),
        "viewportWidth": render_config.viewport_width,
        "maxHeight": render_config.max_height,
    }

    stdout = await _run_renderer_process(
        render_config.node_path,
        render_config.script_path,
        json.dumps(payload, ensure_ascii=False),
        render_config.timeout_seconds,
    )

    try:
        result = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"markdown renderer returned invalid JSON: {e}") from e

    if not result.get("ok"):
        message = result.get("error") or "markdown renderer failed"
        raise RuntimeError(str(message))

    file_path = result.get("file")
    if not isinstance(file_path, str) or not file_path:
        raise RuntimeError("markdown renderer did not return an output file")
    return file_path


async def _run_renderer_process(node_path: str, script_path: str, payload_json: str, timeout: float) -> str:
    process = await asyncio.create_subprocess_exec(
        node_path,
        script_path,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(payload_json.encode("utf-8")),
            timeout=timeout,
        )
    except asyncio.TimeoutError as e:
        process.kill()
        await process.wait()
        raise RuntimeError(f"markdown renderer timed out after {timeout:g}s") from e

    stderr_text = stderr.decode("utf-8", errors="replace").strip()
    if process.returncode != 0:
        raise RuntimeError(stderr_text or f"markdown renderer exited with code {process.returncode}")
    return stdout.decode("utf-8", errors="replace")
