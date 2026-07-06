from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import mimetypes
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import quote

import httpx

from .config import Config

PostJson = Callable[..., Awaitable[dict[str, Any]]]
PostForm = Callable[..., Awaitable[dict[str, Any]]]


async def _default_post_json(url: str, *, headers: dict, json: dict, timeout: float) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, headers=headers, json=json)
        if resp.status_code != 200:
            raise RuntimeError(f"vision API returned {resp.status_code}: {resp.text[:500]}")
        return resp.json()


async def _default_post_form(url: str, *, headers: dict, data: str, timeout: float) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, headers=headers, content=data)
        if resp.status_code != 200:
            raise RuntimeError(f"vision API returned {resp.status_code}: {resp.text[:500]}")
        return resp.json()


def _file_to_data_url(path: str) -> str:
    file_path = Path(path)
    mime = mimetypes.guess_type(file_path.name)[0] or "image/png"
    data = base64.b64encode(file_path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{data}"


def _file_to_base64(path: str) -> str:
    return base64.b64encode(Path(path).read_bytes()).decode("ascii")


def _rfc3986(value: str) -> str:
    return quote(str(value), safe="-_.~")


def _form_encode(params: dict[str, str]) -> str:
    return "&".join(f"{_rfc3986(k)}={_rfc3986(v)}" for k, v in sorted(params.items()))


def _sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _hmac_sha256(key: bytes, value: str) -> bytes:
    return hmac.new(key, value.encode("utf-8"), hashlib.sha256).digest()


def _volcengine_headers(
    *,
    access_key_id: str,
    secret_access_key: str,
    session_token: str,
    region: str,
    service: str,
    host: str,
    query: str,
    body: str,
    now: dt.datetime | None = None,
) -> dict[str, str]:
    timestamp = (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
    x_date = timestamp.strftime("%Y%m%dT%H%M%SZ")
    short_date = x_date[:8]
    content_type = "application/x-www-form-urlencoded"
    payload_hash = _sha256_hex(body)
    header_items = [
        f"content-type:{content_type}",
        f"host:{host}",
        f"x-content-sha256:{payload_hash}",
        f"x-date:{x_date}",
    ]
    if session_token:
        header_items.append(f"x-security-token:{session_token}")
    signed_headers = ";".join(item.split(":", 1)[0] for item in header_items)
    canonical_headers = "\n".join(header_items)
    canonical_request = "\n".join([
        "POST",
        "/",
        query,
        canonical_headers,
        "",
        signed_headers,
        payload_hash,
    ])
    credential_scope = f"{short_date}/{region}/{service}/request"
    string_to_sign = "\n".join([
        "HMAC-SHA256",
        x_date,
        credential_scope,
        _sha256_hex(canonical_request),
    ])
    signing_key = _hmac_sha256(
        _hmac_sha256(
            _hmac_sha256(
                _hmac_sha256(secret_access_key.encode("utf-8"), short_date),
                region,
            ),
            service,
        ),
        "request",
    )
    signature = hmac.new(signing_key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    headers = {
        "Authorization": (
            "HMAC-SHA256 "
            f"Credential={access_key_id}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, "
            f"Signature={signature}"
        ),
        "Content-Type": content_type,
        "Host": host,
        "X-Content-Sha256": payload_hash,
        "X-Date": x_date,
    }
    if session_token:
        headers["X-Security-Token"] = session_token
    return headers


async def _describe_openai_compatible(
    *,
    image_file: str | None,
    image_url: str | None,
    config: Config,
    post_json: PostJson,
) -> str:
    vision = config.vision
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


async def _describe_volcengine_ocr(
    *,
    image_file: str | None,
    image_url: str | None,
    config: Config,
    post_form: PostForm,
) -> str:
    vision = config.vision
    if not vision.access_key_id or not vision.secret_access_key:
        return "[Error: vision.access_key_id and vision.secret_access_key required for volcengine-ocr]"

    params: dict[str, str] = {}
    if image_url:
        params["image_url"] = image_url
    elif image_file:
        try:
            params["image_base64"] = _file_to_base64(image_file)
        except Exception as e:
            return f"[Error: cannot read image file: {e}]"
    else:
        return "[Error: image source not available]"

    body = _form_encode(params)
    host = (
        vision.base_url or "https://visual.volcengineapi.com"
    ).removeprefix("https://").removeprefix("http://").rstrip("/")
    query = _form_encode({"Action": vision.action, "Version": vision.version})
    headers = _volcengine_headers(
        access_key_id=vision.access_key_id,
        secret_access_key=vision.secret_access_key,
        session_token=vision.session_token,
        region=vision.region,
        service=vision.service,
        host=host,
        query=query,
        body=body,
    )
    try:
        data = await post_form(
            f"https://{host}/?{query}",
            headers=headers,
            data=body,
            timeout=vision.timeout_seconds,
        )
    except Exception as e:
        return f"[Error: {e}]"

    if data.get("code") not in (0, 10000, "0", "10000", None):
        return f"[Error: volcengine OCR returned {data.get('code')}: {data.get('message', '')}]"

    line_texts = data.get("data", {}).get("line_texts", [])
    if not line_texts:
        return "[Error: volcengine OCR returned no text]"
    return "OCR text:\n" + "\n".join(str(line) for line in line_texts if str(line).strip())


async def describe_image(
    *,
    image_file: str | None,
    image_url: str | None,
    config: Config,
    post_json: PostJson = _default_post_json,
    post_form: PostForm = _default_post_form,
) -> str:
    vision = config.vision
    if not vision.enabled:
        return "[Error: vision provider disabled]"
    if vision.provider == "volcengine-ocr":
        return await _describe_volcengine_ocr(
            image_file=image_file,
            image_url=image_url,
            config=config,
            post_form=post_form,
        )
    if vision.provider != "openai-compatible":
        return f"[Error: unknown vision.provider: {vision.provider}]"
    return await _describe_openai_compatible(
        image_file=image_file,
        image_url=image_url,
        config=config,
        post_json=post_json,
    )
