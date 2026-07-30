from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import logging
import re
import sqlite3
import uuid
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlparse

from .config import IngressConfig
from .memory.store import ActorRecord, EventRecord, EventType, MessageStore
from .message.receiver import ServiceMessageEvent

logger = logging.getLogger("mutsumi.ingress")

INGRESS_PATH = "/v1/events"
_MAX_HEADER_BYTES = 16 * 1024
_SERVICE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class IngressRequestError(ValueError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class ServiceIngress:
    """Small loopback HTTP/1.1 ingress for NapCat-shaped service events."""

    def __init__(
        self,
        config: IngressConfig,
        store: MessageStore,
        on_event: Callable[[ServiceMessageEvent, str], Awaitable[None]],
    ):
        self.config = config
        self.store = store
        self.on_event = on_event
        self._server: asyncio.AbstractServer | None = None
        self._client_tasks: set[asyncio.Task[Any]] = set()

    @property
    def is_running(self) -> bool:
        return self._server is not None

    async def start(self) -> None:
        if not self.config.enabled:
            logger.info("[INGRESS] disabled")
            return
        if not self.config.token:
            raise ValueError("ingress.token is required when ingress is enabled")
        if not str(self.config.target_user_id).strip():
            raise ValueError("ingress.target_user_id is required when ingress is enabled")
        target_user_id = str(self.config.target_user_id).strip()
        if not target_user_id.isdigit() or int(target_user_id) <= 0:
            raise ValueError("ingress.target_user_id must be a positive QQ number")
        if not _is_loopback_host(self.config.host):
            raise ValueError("ingress.host must be a loopback address")
        if not (0 <= int(self.config.port) <= 65535):
            raise ValueError("ingress.port must be between 0 and 65535")

        self._server = await asyncio.start_server(
            self._handle_client,
            host=self.config.host,
            port=self.config.port,
            limit=_MAX_HEADER_BYTES + max(1024, int(self.config.max_body_bytes)),
        )
        addresses = ", ".join(str(sock.getsockname()) for sock in self._server.sockets or [])
        logger.info("[INGRESS] listening host=%s port=%s addresses=%s", self.config.host, self.config.port, addresses)

    async def close(self) -> None:
        if self._server is None:
            return
        self._server.close()
        await self._server.wait_closed()
        self._server = None
        current = asyncio.current_task()
        clients = [task for task in self._client_tasks if task is not current and not task.done()]
        if clients:
            await asyncio.gather(*clients, return_exceptions=True)
        logger.info("[INGRESS] stopped")

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._client_tasks.add(task)
        try:
            try:
                request = await asyncio.wait_for(
                    self._read_request(reader),
                    timeout=max(1.0, float(self.config.request_timeout_seconds)),
                )
                response_status, response_body = await self._process_request(request)
            except asyncio.TimeoutError:
                response_status, response_body = 408, {"status": "failed", "retcode": 408, "message": "request timeout"}
            except IngressRequestError as exc:
                response_status, response_body = exc.status, {
                    "status": "failed",
                    "retcode": exc.status,
                    "message": str(exc),
                }
            except Exception:
                logger.exception("[INGRESS] request handling failed")
                response_status, response_body = 500, {
                    "status": "failed",
                    "retcode": 500,
                    "message": "internal ingress error",
                }

            await self._write_response(writer, response_status, response_body)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            if task is not None:
                self._client_tasks.discard(task)

    async def _read_request(self, reader: asyncio.StreamReader) -> tuple[str, str, dict[str, str], bytes]:
        try:
            header_bytes = await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError) as exc:
            raise IngressRequestError(400, "invalid HTTP headers") from exc
        if len(header_bytes) > _MAX_HEADER_BYTES:
            raise IngressRequestError(431, "HTTP headers too large")

        lines = header_bytes[:-4].decode("iso-8859-1").split("\r\n")
        request_line = lines[0].split()
        if len(request_line) != 3:
            raise IngressRequestError(400, "invalid HTTP request line")
        method, path, version = request_line
        if version not in {"HTTP/1.0", "HTTP/1.1"}:
            raise IngressRequestError(505, "HTTP version not supported")

        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ":" not in line:
                raise IngressRequestError(400, "invalid HTTP header")
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()

        try:
            content_length = int(headers.get("content-length", "0"))
        except ValueError as exc:
            raise IngressRequestError(400, "invalid Content-Length") from exc
        if content_length < 0 or content_length > int(self.config.max_body_bytes):
            raise IngressRequestError(413, "request body too large")
        if headers.get("transfer-encoding", "").lower() not in {"", "identity"}:
            raise IngressRequestError(400, "chunked transfer encoding is not supported")
        try:
            body = await reader.readexactly(content_length)
        except asyncio.IncompleteReadError as exc:
            raise IngressRequestError(400, "incomplete request body") from exc
        return method, path, headers, body

    async def _process_request(self, request: tuple[str, str, dict[str, str], bytes]) -> tuple[int, dict[str, Any]]:
        method, path, headers, body = request
        if method != "POST":
            raise IngressRequestError(405, "only POST is supported")
        if path.split("?", 1)[0] != INGRESS_PATH:
            raise IngressRequestError(404, "unknown ingress path")
        if not self._authorized(headers.get("authorization", "")):
            raise IngressRequestError(401, "unauthorized")
        if headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise IngressRequestError(415, "Content-Type must be application/json")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise IngressRequestError(400, "body must be UTF-8 JSON") from exc
        if not isinstance(payload, dict):
            raise IngressRequestError(400, "body must be a JSON object")

        event = self._parse_event(payload)
        service_id = str(event.user_id)
        message_id = str(event.message_id).strip()
        if not message_id:
            raise IngressRequestError(400, "message_id is required")
        event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"mutsumi-ingress:{service_id}:{message_id}"))
        existing = await self.store.get_event(event_id)
        if existing is not None:
            await self._retry_received(existing, event_id)
            logger.info("[INGRESS] duplicate service=%s message_id=%s event_id=%s", service_id, message_id, event_id)
            return 202, {
                "status": "ok",
                "retcode": 0,
                "data": {"accepted": True, "duplicate": True, "event_id": event_id},
            }

        actor_id = f"service:{service_id}"
        actor_name = _clean_actor_name(
            (event.sender or {}).get("card") or (event.sender or {}).get("nickname"),
            service_id,
        )
        content = event.raw_message or _text_from_segments(event.message)
        await self.store.ensure_actor(ActorRecord(
            actor_id=actor_id,
            kind="service",
            platform="service",
            platform_subject_id=service_id,
            display_name=actor_name,
        ))
        record = EventRecord(
            event_id=event_id,
            conversation_id=actor_id,
            actor_id=actor_id,
            actor_kind="service",
            actor_name=actor_name,
            event_type=EventType.INBOUND.value,
            content=content,
            payload=payload,
            visibility="private",
            audience=str(self.config.target_user_id),
            status="received",
        )
        try:
            await self.store.append_event(record)
        except sqlite3.IntegrityError:
            # Two retries can pass get_event() concurrently. The unique event
            # ID makes the losing insert an ordinary idempotent retry.
            existing = await self.store.get_event(event_id)
            if existing is None:
                raise
            await self._retry_received(existing, event_id)
            logger.info("[INGRESS] concurrent duplicate service=%s message_id=%s event_id=%s", service_id, message_id, event_id)
            return 202, {
                "status": "ok",
                "retcode": 0,
                "data": {"accepted": True, "duplicate": True, "event_id": event_id},
            }
        await self.on_event(event, event_id)
        logger.info("[INGRESS] accepted service=%s message_id=%s event_id=%s", service_id, message_id, event_id)
        return 202, {
            "status": "ok",
            "retcode": 0,
            "data": {"accepted": True, "duplicate": False, "event_id": event_id},
        }

    async def _retry_received(self, record: EventRecord, event_id: str) -> None:
        if record.status != "received":
            return
        try:
            pending_event = self._parse_event(record.payload or {})
            await self.on_event(pending_event, event_id)
        except Exception:
            logger.exception("[INGRESS] retry enqueue failed event_id=%s", event_id)
            raise

    def _authorized(self, value: str) -> bool:
        prefix, _, token = value.partition(" ")
        return prefix.lower() == "bearer" and bool(token) and hmac.compare_digest(token, self.config.token)

    @staticmethod
    def _parse_event(payload: dict[str, Any]) -> ServiceMessageEvent:
        if payload.get("post_type") != "message" or payload.get("message_type") != "private":
            raise IngressRequestError(400, "only private message events are supported")
        try:
            event = ServiceMessageEvent.model_validate({**payload, "source_kind": "service"})
        except Exception as exc:
            raise IngressRequestError(400, "invalid NapCat message event") from exc
        service_id = str(event.user_id).strip()
        if not _SERVICE_ID_RE.fullmatch(service_id):
            raise IngressRequestError(400, "user_id must be a safe service identifier")
        if not isinstance(event.message, list) or not event.message:
            raise IngressRequestError(400, "message must be a non-empty NapCat segment list")
        _validate_segments(event.message)
        return event

    async def _write_response(
        self,
        writer: asyncio.StreamWriter,
        status: int,
        payload: dict[str, Any],
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        reason = {202: "Accepted", 400: "Bad Request", 401: "Unauthorized", 405: "Method Not Allowed",
                  408: "Request Timeout", 413: "Payload Too Large", 415: "Unsupported Media Type",
                  431: "Request Header Fields Too Large", 505: "HTTP Version Not Supported", 404: "Not Found",
                  500: "Internal Server Error"}.get(status, "Error")
        headers = (
            f"HTTP/1.1 {status} {reason}\r\n"
            "Connection: close\r\n"
            "Content-Type: application/json; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n\r\n"
        ).encode("ascii")
        writer.write(headers + body)
        await writer.drain()


def _text_from_segments(segments: list[dict[str, Any]]) -> str:
    return "".join(
        str(segment.get("data", {}).get("text", ""))
        for segment in segments
        if segment.get("type") == "text"
    )


def _is_loopback_host(host: str) -> bool:
    normalized = str(host).strip().lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _clean_actor_name(value: Any, fallback: str) -> str:
    cleaned = " ".join(str(value or fallback).split())
    return (cleaned or fallback)[:128]


def _validate_segments(segments: list[dict[str, Any]]) -> None:
    has_content = False
    for segment in segments:
        if not isinstance(segment, dict):
            raise IngressRequestError(400, "message segments must be JSON objects")
        segment_type = str(segment.get("type") or "")
        data = segment.get("data")
        if not isinstance(data, dict):
            raise IngressRequestError(400, "message segment data must be a JSON object")
        if segment_type == "text":
            text = data.get("text")
            if not isinstance(text, str):
                raise IngressRequestError(400, "text segment requires string data.text")
            has_content = has_content or bool(text)
            continue
        if segment_type == "image":
            if data.get("file"):
                raise IngressRequestError(400, "external image segments cannot use local file paths")
            image_url = str(data.get("url") or "").strip()
            parsed = urlparse(image_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise IngressRequestError(400, "external image segments require an HTTP(S) data.url")
            has_content = True
            continue
        raise IngressRequestError(400, f"unsupported external message segment: {segment_type or 'unknown'}")
    if not has_content:
        raise IngressRequestError(400, "message has no text or image content")
