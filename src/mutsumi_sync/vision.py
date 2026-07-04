from __future__ import annotations

import base64
import mimetypes
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx

from .config import Config

PostJson = Callable[..., Awaitable[dict[str, Any]]]


async def _default_post_json(url: str, *, headers: dict, json: dict, timeout: float) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, headers=headers, json=json)
        if resp.status_code != 200:
            raise RuntimeError(f"vision API returned {resp.status_code}: {resp.text[:500]}")
        return resp.json()


def _file_to_data_url(path: str) -> str:
    file_path = Path(path)
    mime = mimetypes.guess_type(file_path.name)[0] or "image/png"
    data = base64.b64encode(file_path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{data}"


async def describe_image(
    *,
    image_file: str | None,
    image_url: str | None,
    config: Config,
    post_json: PostJson = _default_post_json,
) -> str:
    vision = config.vision
    if not vision.enabled:
        return "[Error: vision provider disabled]"
    if not vision.api_key:
        return "[Error: vision.api_key not configured]"
    if not vision.base_url:
        return "[Error: vision.base_url not configured]"
    if not vision.model:
        return "[Error: vision.model not configured]"

    url = image_url
    if not url and image_file:
        try:
            url = _file_to_data_url(image_file)
        except Exception as e:
            return f"[Error: cannot read image file: {e}]"
    if not url:
        return "[Error: image source not available]"

    payload = {
        "model": vision.model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "Describe this image for long-term chat memory. "
                            "Be concise, factual, and preserve visible text, formulas, code, and diagrams."
                        ),
                    },
                    {"type": "image_url", "image_url": {"url": url}},
                ],
            }
        ],
        "temperature": 0,
    }
    headers = {
        "Authorization": f"Bearer {vision.api_key}",
        "Content-Type": "application/json",
    }
    try:
        data = await post_json(
            f"{vision.base_url.rstrip('/')}/chat/completions",
            headers=headers,
            json=payload,
            timeout=vision.timeout_seconds,
        )
    except Exception as e:
        return f"[Error: {e}]"
    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    if not content:
        return "[Error: vision provider returned empty content]"
    return str(content).strip()
