from __future__ import annotations

import json
import logging

import httpx

logger = logging.getLogger("mutsumi.tools.http_api")

HTTP_API_SCHEMA = {
    "type": "object",
    "properties": {
        "method": {"type": "string", "enum": ["GET", "POST", "PUT", "DELETE", "PATCH"]},
        "url": {"type": "string", "description": "完整 URL 或相对路径"},
        "headers": {"type": "object", "additionalProperties": {"type": "string"}},
        "body": {"type": "object", "description": "JSON body (仅 POST/PUT/PATCH 时使用)"},
    },
    "required": ["method", "url"],
}


async def http_api_call(args: dict) -> str:
    method = args.get("method", "GET").upper()
    url = args["url"]
    headers = args.get("headers", {})
    body = args.get("body")

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            if method == "GET":
                resp = await client.get(url, headers=headers)
            elif method == "POST":
                resp = await client.post(url, headers=headers, json=body or {})
            elif method == "PUT":
                resp = await client.put(url, headers=headers, json=body or {})
            elif method == "DELETE":
                resp = await client.delete(url, headers=headers)
            elif method == "PATCH":
                resp = await client.patch(url, headers=headers, json=body or {})
            else:
                return f"[Error: unsupported method: {method}]"

            resp.raise_for_status()

            content_type = resp.headers.get("content-type", "")
            if "application/json" in (content_type or "").lower():
                body = resp.text[:2000]
            elif "text/" in (content_type or "").lower():
                body = resp.text[:500]
            else:
                body = resp.text[:200]
                if len(resp.text) > 200:
                    ct_short = (content_type or "unknown").split(";")[0]
                    body += f"\n...[truncated {len(resp.text)} chars, {ct_short}]"

            return body
    except httpx.HTTPStatusError as e:
        return f"[Error: HTTP {e.response.status_code}: {e.response.text[:500]}]"
    except Exception as e:
        logger.exception("http_api_call failed")
        return f"[Error: {e}]"
