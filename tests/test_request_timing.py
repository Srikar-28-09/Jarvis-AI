"""Request timing logs must correlate invocations without recording prompts."""

import asyncio
import re

from src.request_timing import (
    RequestTimingMiddleware,
    llm_finished,
    llm_first_token,
    llm_started,
    reset_request_context,
    set_request_context,
    current_request_id,
)


def test_llm_timing_is_request_correlated_and_content_free(caplog):
    caplog.set_level("INFO", logger="request_timing")
    tokens = set_request_context("test-request-123")
    try:
        call_no, started = llm_started("ollama", "qwen3:8b")
        llm_first_token(call_no, started)
        llm_finished(call_no, started)
    finally:
        reset_request_context(tokens)

    output = "\n".join(record.getMessage() for record in caplog.records)
    assert "request_id=test-request-123" in output
    assert "stage=llm_start call=1" in output
    assert "stage=llm_first_token" in output
    assert "stage=llm_complete" in output
    assert "private prompt" not in output


def test_response_header_timing_preserves_valid_request_id(caplog):
    observed = {}

    async def app(scope, receive, send):
        observed["request_id"] = current_request_id()
        observed["header"] = dict(scope["headers"])[b"x-odysseus-request-id"].decode()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})

    # ASGI send is async; capture emitted events with a small adapter.
    async def run_with_async_send():
        sent = []

        async def send(event):
            sent.append(event)

        await RequestTimingMiddleware(app)(
            {
                "type": "http",
                "path": "/api/chat_stream",
                "headers": [(b"x-odysseus-request-id", b"headers-test-1")],
            },
            lambda: None,
            send,
        )
        return sent

    with caplog.at_level("INFO", logger="request_timing"):
        events = asyncio.run(run_with_async_send())
    assert len(events) == 2
    assert observed == {"request_id": "headers-test-1", "header": "headers-test-1"}
    assert dict(events[0]["headers"])[b"x-odysseus-request-id"] == b"headers-test-1"
    assert "request_id=headers-test-1 stage=response_headers" in caplog.text
    assert "route=chat_stream" in caplog.text
    assert "request_id=headers-test-1 stage=request_complete" in caplog.text


def test_chat_request_without_id_gets_one_for_stream_and_response(caplog):
    observed = []

    async def app(scope, receive, send):
        await asyncio.sleep(0)
        observed.append((current_request_id(), dict(scope["headers"])[b"x-odysseus-request-id"].decode()))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})

    async def run():
        sent = []

        async def send(event):
            sent.append(event)

        await RequestTimingMiddleware(app)(
            {"type": "http", "path": "/api/chat_stream", "headers": []},
            lambda: None,
            send,
        )
        return sent

    with caplog.at_level("INFO", logger="request_timing"):
        events = asyncio.run(run())
    request_id, header_id = observed[0]
    assert re.fullmatch(r"[0-9a-f-]{36}", request_id)
    assert header_id == request_id
    assert dict(events[0]["headers"])[b"x-odysseus-request-id"].decode() == request_id
    assert f"request_id={request_id} stage=response_headers" in caplog.text
    assert f"request_id={request_id} stage=request_complete" in caplog.text
    assert "request_id=- stage=response_headers" not in caplog.text


def test_invalid_request_id_is_replaced_without_log_injection(caplog):
    observed = {}

    async def app(scope, receive, send):
        observed["request_id"] = current_request_id()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok", "more_body": False})

    async def run():
        sent = []

        async def send(event):
            sent.append(event)

        await RequestTimingMiddleware(app)(
            {
                "type": "http",
                "path": "/api/chat_stream",
                "headers": [(b"x-odysseus-request-id", b"invalid id\\nforged=true")],
            },
            lambda: None,
            send,
        )
        return sent

    with caplog.at_level("INFO", logger="request_timing"):
        events = asyncio.run(run())
    request_id = observed["request_id"]
    assert re.fullmatch(r"[0-9a-f-]{36}", request_id)
    assert b"forged=true" not in dict(events[0]["headers"])[b"x-odysseus-request-id"]
    assert "forged=true" not in caplog.text


def test_chat_disconnect_is_logged_and_context_is_reset(caplog):
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"partial", "more_body": True})

    async def run():
        async def send(event):
            if event.get("type") == "http.response.body":
                raise OSError("client disconnected")

        try:
            await RequestTimingMiddleware(app)(
                {"type": "http", "path": "/api/chat_stream", "headers": []},
                lambda: None,
                send,
            )
        except OSError:
            pass
        return current_request_id()

    with caplog.at_level("INFO", logger="request_timing"):
        after = asyncio.run(run())
    assert after == "-"
    assert "outcome=client_disconnect" in caplog.text
    assert "stage=request_complete" in caplog.text


def test_tracked_background_task_does_not_inherit_chat_request_trace():
    from routes.chat_helpers import _spawn_bg

    async def run():
        tokens = set_request_context("foreground-chat-id")
        try:
            async def inspect_context():
                return current_request_id()

            background_id = await _spawn_bg(inspect_context())
            return background_id, current_request_id()
        finally:
            reset_request_context(tokens)

    assert asyncio.run(run()) == ("-", "foreground-chat-id")
