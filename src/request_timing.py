"""Request-scoped, content-free latency tracing helpers."""

from __future__ import annotations

import asyncio
import contextvars
import functools
import logging
import re
import uuid
import time
from typing import Any

logger = logging.getLogger("request_timing")

_REQUEST_ID_HEADER = b"x-odysseus-request-id"
_REQUEST_ID_RE = re.compile(rb"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")

_request_id: contextvars.ContextVar[str] = contextvars.ContextVar("odysseus_request_id", default="-")
_llm_state: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "odysseus_llm_state", default=None
)


def set_request_context(request_id: str):
    rid = str(request_id or "").strip()[:128] or "-"
    return _request_id.set(rid), _llm_state.set({"count": 0})


def reset_request_context(tokens) -> None:
    request_token, llm_token = tokens
    _llm_state.reset(llm_token)
    _request_id.reset(request_token)


def current_request_id() -> str:
    return _request_id.get()


def llm_call_count() -> int:
    return int((_llm_state.get() or {}).get("count", 0))


def log_stage(stage: str, elapsed_ms: float, *, outcome: str = "ok", **fields) -> None:
    safe_fields = " ".join(f"{key}={str(value)[:80]}" for key, value in fields.items())
    logger.info(
        "[request_timing] request_id=%s stage=%s elapsed_ms=%.1f outcome=%s%s",
        current_request_id(), stage, max(0.0, elapsed_ms), outcome,
        f" {safe_fields}" if safe_fields else "",
    )


def llm_started(provider: str, model: str, *, operation: str = "stream") -> tuple[int, float]:
    state = _llm_state.get()
    if state is None:
        state = {"count": 0}
        _llm_state.set(state)
    state["count"] += 1
    call_no = state["count"]
    started = time.perf_counter()
    logger.info(
        "[request_timing] request_id=%s stage=llm_start call=%s operation=%s provider=%s model=%s",
        current_request_id(), call_no, operation, provider, model,
    )
    return call_no, started


def llm_first_token(call_no: int, started: float) -> None:
    log_stage("llm_first_token", (time.perf_counter() - started) * 1000, call=call_no)


def llm_finished(call_no: int, started: float, *, outcome: str = "ok") -> None:
    state = _llm_state.get() or {"count": call_no}
    log_stage("llm_complete", (time.perf_counter() - started) * 1000,
              outcome=outcome, call=call_no, llm_calls=state.get("count", call_no))


def timed_tool_execution(func):
    @functools.wraps(func)
    async def wrapper(block, *args, **kwargs):
        started = time.perf_counter()
        tool_name = str(getattr(block, "tool_type", "unknown"))[:64]
        outcome = "ok"
        try:
            result = await func(block, *args, **kwargs)
            if isinstance(result, tuple) and len(result) > 1 and isinstance(result[1], dict) and result[1].get("error"):
                outcome = "error"
            return result
        except BaseException:
            outcome = "error"
            raise
        finally:
            log_stage("tool_execution", (time.perf_counter() - started) * 1000,
                      outcome=outcome, tool=tool_name)
    return wrapper


class RequestTimingMiddleware:
    """ASGI middleware that validates and propagates chat request IDs."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        request_started = time.perf_counter()
        headers = list(scope.get("headers") or ())
        supplied_ids = [value for name, value in headers if name.lower() == _REQUEST_ID_HEADER]
        raw_id = supplied_ids[0] if len(supplied_ids) == 1 else b""
        chat_route = _chat_route_group(scope.get("path", ""))
        if raw_id and _REQUEST_ID_RE.fullmatch(raw_id):
            request_id = raw_id.decode("ascii")
        elif supplied_ids or chat_route != "other":
            request_id = str(uuid.uuid4())
        else:
            request_id = "-"

        # Normalize the header visible to FastAPI so the route and ContextVar
        # use the same ID, including when the server had to create one.
        normalized_headers = [
            (name, value) for name, value in headers
            if name.lower() != _REQUEST_ID_HEADER
        ]
        if request_id != "-":
            normalized_headers.append((_REQUEST_ID_HEADER, request_id.encode("ascii")))
        scope["headers"] = normalized_headers
        scope["odysseus_request_id"] = request_id
        tokens = set_request_context(request_id)
        headers_logged = False
        response_status = None
        terminal_outcome = "ok"

        async def send_with_timing(message):
            nonlocal headers_logged, response_status, terminal_outcome
            if message.get("type") == "http.response.start" and not headers_logged:
                headers_logged = True
                response_status = int(message.get("status") or 0)
                if response_status >= 500:
                    terminal_outcome = "error"
                elif response_status >= 400:
                    terminal_outcome = "http_error"
                log_stage(
                    "response_headers",
                    (time.perf_counter() - request_started) * 1000,
                    outcome=terminal_outcome,
                    route=chat_route,
                )
                if request_id != "-":
                    response_headers = [
                        (name, value) for name, value in message.get("headers", [])
                        if name.lower() != _REQUEST_ID_HEADER
                    ]
                    response_headers.append((_REQUEST_ID_HEADER, request_id.encode("ascii")))
                    message = {**message, "headers": response_headers}
            try:
                await send(message)
            except BaseException as error:
                if isinstance(error, asyncio.CancelledError):
                    terminal_outcome = "cancelled"
                elif isinstance(error, (ConnectionError, OSError)) or type(error).__name__ in {
                    "ClientDisconnect", "ClientDisconnected",
                }:
                    terminal_outcome = "client_disconnect"
                else:
                    terminal_outcome = "error"
                raise

        try:
            await self.app(scope, receive, send_with_timing)
        except BaseException as error:
            if isinstance(error, asyncio.CancelledError):
                terminal_outcome = "cancelled"
            elif isinstance(error, (ConnectionError, OSError)) or type(error).__name__ in {
                "ClientDisconnect", "ClientDisconnected",
            }:
                terminal_outcome = "client_disconnect"
            else:
                terminal_outcome = "error"
            raise
        finally:
            if chat_route != "other":
                log_stage(
                    "request_complete",
                    (time.perf_counter() - request_started) * 1000,
                    outcome=terminal_outcome,
                    route=chat_route,
                    status=response_status or "not_sent",
                )
            reset_request_context(tokens)


def _chat_route_group(path: Any) -> str:
    """Return a content-free category for chat-related HTTP timing events."""
    normalized = str(path or "").rstrip("/")
    if normalized.endswith("/api/chat_stream"):
        return "chat_stream"
    if normalized.endswith("/api/computer_use/stream"):
        return "computer_use_stream"
    return "other"
