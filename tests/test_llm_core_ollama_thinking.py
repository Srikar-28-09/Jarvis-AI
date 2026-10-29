"""Tests for Ollama thinking controls across native and /v1 APIs.

Covers:
- _is_ollama_openai_compat_url: URL classification (local host + /v1 path)
- reasoning_effort: none is injected for Ollama /v1 thinking models
- think is not incorrectly sent to /v1/chat/completions
- thinking controls are NOT injected for non-thinking models or other providers
"""
import asyncio
import json
import logging

import pytest

from src import llm_core


# ---------------------------------------------------------------------------
# Fake HTTP client — captures the outgoing payload without network I/O
# ---------------------------------------------------------------------------

class _FakeResp:
    status_code = 200

    async def aiter_lines(self):
        # Yield a minimal done event so stream_llm exits cleanly
        yield json.dumps({"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]})
        yield "data: [DONE]"

    async def aread(self):
        return b""


class _FakeStreamCtx:
    def __init__(self, captured):
        self._captured = captured

    async def __aenter__(self):
        return _FakeResp()

    async def __aexit__(self, *a):
        return False


class _FakeClient:
    """Minimal stand-in for httpx.AsyncClient that captures request payload."""

    def __init__(self):
        self.captured_payload = {}

    def stream(self, method, url, **kw):
        self.captured_payload = kw.get("json") or {}
        return _FakeStreamCtx(self.captured_payload)


class _FakeNativeResp:
    status_code = 200

    def __init__(self, records=None):
        self.records = records or [
            {
                "model": "qwen3:8b",
                "message": {"thinking": "private thought", "content": ""},
                "done": False,
            },
            {
                "model": "qwen3:8b",
                "message": {"thinking": "", "content": "A large language model predicts text."},
                "done": True,
                "done_reason": "stop",
            },
        ]

    async def aiter_lines(self):
        for record in self.records:
            yield json.dumps(record)

    async def aread(self):
        return b""


class _FakeNativeStreamCtx:
    def __init__(self, records=None):
        self.records = records

    async def __aenter__(self):
        return _FakeNativeResp(self.records)

    async def __aexit__(self, *args):
        return False


class _FakeNativeClient:
    def __init__(self, records=None):
        self.records = records
        self.captured_payload = None

    def stream(self, *args, **kwargs):
        self.captured_payload = kwargs.get("json")
        return _FakeNativeStreamCtx(self.records)


def _capture_payload(monkeypatch, url, model, message="hi", *, force_disable_thinking=False,
                     temperature=llm_core.LLMConfig.DEFAULT_TEMPERATURE,
                     max_tokens=llm_core.LLMConfig.DEFAULT_MAX_TOKENS):
    """Run stream_llm, intercept the HTTP payload, and return it."""
    client = _FakeClient()
    monkeypatch.setattr(llm_core, "_get_http_client", lambda: client)
    monkeypatch.setattr(llm_core, "_is_host_dead", lambda u: False)
    monkeypatch.setattr(llm_core, "note_model_activity", lambda *a, **k: None)
    monkeypatch.setattr(llm_core, "_clear_host_dead", lambda *a, **k: None)
    monkeypatch.setattr(llm_core, "get_context_length", lambda u, m: 32768)

    async def run():
        return [c async for c in llm_core.stream_llm(
            url, model, [{"role": "user", "content": message}],
            temperature=temperature,
            max_tokens=max_tokens,
            force_disable_thinking=force_disable_thinking,
        )]

    asyncio.run(run())
    return client.captured_payload


def test_native_ollama_stream_keeps_reasoning_separate_and_reports_finish_reason(monkeypatch, caplog):
    monkeypatch.setattr(llm_core, "_get_http_client", lambda: _FakeNativeClient())
    monkeypatch.setattr(llm_core, "_is_host_dead", lambda _url: False)
    monkeypatch.setattr(llm_core, "note_model_activity", lambda *a, **k: None)
    monkeypatch.setattr(llm_core, "_clear_host_dead", lambda *a, **k: None)
    monkeypatch.setattr(llm_core, "get_context_length", lambda *_a: 32768)

    async def run():
        return [chunk async for chunk in llm_core.stream_llm(
            "http://127.0.0.1:11434/api", "qwen3:8b",
            [{"role": "user", "content": "What is an LLM?"}],
        )]

    with caplog.at_level(logging.INFO):
        chunks = asyncio.run(run())
    events = [json.loads(chunk[6:]) for chunk in chunks if chunk.startswith("data: {")]
    assert any(event.get("thinking") for event in events)
    assert any(event.get("delta") == "A large language model predicts text." for event in events)
    diagnostics = next(event for event in events if event.get("type") == "stream_diagnostics")
    assert diagnostics["finish_reason"] == "stop"
    assert diagnostics["reasoning_chars"] == len("private thought")
    assert diagnostics["final_answer_chars"] == len("A large language model predicts text.")
    finish_log = next(record.message for record in caplog.records if "stage=ollama_stream_finish" in record.message)
    assert "finish_reason=stop" in finish_log
    assert "private thought" not in finish_log
    assert "A large language model predicts text" not in finish_log
    timing_log = "\n".join(record.message for record in caplog.records)
    assert "stage=ollama_http_dispatch" in timing_log
    assert "stage=ollama_response_headers" in timing_log
    assert "stage=llm_first_token" in timing_log
    assert "stage=ollama_first_token" in timing_log
    assert "stage=llm_first_answer_token" in timing_log


def test_native_ollama_reasoning_only_stream_reports_zero_final_answer(monkeypatch, caplog):
    records = [
        {
            "model": "qwen3:8b",
            "message": {"thinking": "private thought", "content": ""},
            "done": True,
            "done_reason": "stop",
        },
    ]
    monkeypatch.setattr(llm_core, "_get_http_client", lambda: _FakeNativeClient(records))
    monkeypatch.setattr(llm_core, "_is_host_dead", lambda _url: False)
    monkeypatch.setattr(llm_core, "note_model_activity", lambda *a, **k: None)
    monkeypatch.setattr(llm_core, "_clear_host_dead", lambda *a, **k: None)
    monkeypatch.setattr(llm_core, "get_context_length", lambda *_a: 32768)

    async def run():
        return [chunk async for chunk in llm_core.stream_llm(
            "http://127.0.0.1:11434/api", "qwen3:8b",
            [{"role": "user", "content": "What is an LLM?"}],
        )]

    with caplog.at_level(logging.INFO):
        chunks = asyncio.run(run())
    events = [json.loads(chunk[6:]) for chunk in chunks if chunk.startswith("data: {")]
    assert sum(bool(event.get("thinking")) for event in events) == 1
    assert not any(event.get("delta") and not event.get("thinking") for event in events)
    diagnostics = next(event for event in events if event.get("type") == "stream_diagnostics")
    assert diagnostics["finish_reason"] == "stop"
    assert diagnostics["response_chars"] == len("private thought")
    assert diagnostics["final_answer_chars"] == 0
    finish_log = next(record.message for record in caplog.records if "stage=ollama_stream_finish" in record.message)
    assert "final_answer_chars=0" in finish_log
    assert "private thought" not in finish_log


# ---------------------------------------------------------------------------
# _is_ollama_openai_compat_url — pure function, no I/O
# ---------------------------------------------------------------------------

class TestIsOllamaOpenAICompatUrl:
    """Unit tests for the URL classifier that gates think-suppression."""

    # Positive cases — should be True
    def test_default_port_v1_root(self):
        assert llm_core._is_ollama_openai_compat_url("http://127.0.0.1:11434/v1")

    def test_default_port_chat_completions(self):
        assert llm_core._is_ollama_openai_compat_url("http://127.0.0.1:11434/v1/chat/completions")

    def test_localhost_default_port(self):
        assert llm_core._is_ollama_openai_compat_url("http://localhost:11434/v1")

    def test_localhost_default_port_with_path(self):
        assert llm_core._is_ollama_openai_compat_url("http://localhost:11434/v1/chat/completions")

    def test_loopback_ipv6(self):
        # IPv6 addresses in URLs require square brackets per RFC 3986
        assert llm_core._is_ollama_openai_compat_url("http://[::1]:11434/v1")

    def test_any_local_non_default_port(self):
        """Localhost on a non-default port (custom OLLAMA_HOST) must also match."""
        assert llm_core._is_ollama_openai_compat_url("http://127.0.0.1:11435/v1")

    def test_localhost_non_default_port(self):
        assert llm_core._is_ollama_openai_compat_url("http://localhost:8080/v1/chat/completions")

    def test_zero_dot_zero_host(self):
        assert llm_core._is_ollama_openai_compat_url("http://0.0.0.0:11434/v1")

    # Negative cases — should be False
    def test_openai_api_v1(self):
        """Real OpenAI endpoint must never match, even though path is /v1."""
        assert not llm_core._is_ollama_openai_compat_url("https://api.openai.com/v1")

    def test_openai_chat_completions(self):
        assert not llm_core._is_ollama_openai_compat_url("https://api.openai.com/v1/chat/completions")

    def test_ollama_native_api_path(self):
        """The native /api path is a different surface and must not match /v1."""
        assert not llm_core._is_ollama_openai_compat_url("http://localhost:11434/api")

    def test_ollama_native_api_chat(self):
        assert not llm_core._is_ollama_openai_compat_url("http://localhost:11434/api/chat")

    def test_remote_openrouter(self):
        assert not llm_core._is_ollama_openai_compat_url("https://openrouter.ai/api/v1")

    def test_empty_string(self):
        assert not llm_core._is_ollama_openai_compat_url("")

    def test_none_like_empty(self):
        assert not llm_core._is_ollama_openai_compat_url(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Payload injection — API-specific thinking controls
# ---------------------------------------------------------------------------

class TestThinkSuppression:
    """Assert supported thinking settings in the final serialized payload."""

    def test_reasoning_effort_none_for_ollama_v1_thinking_model(self, monkeypatch):
        """Simple requests suppress extended reasoning on Ollama /v1."""
        payload = _capture_payload(
            monkeypatch, "http://127.0.0.1:11434/v1/chat/completions", "qwen3:14b"
        )
        assert payload.get("reasoning_effort") == "none"
        assert "think" not in payload

    def test_knowledge_question_uses_low_effort_control_for_qwen3_8b(self, monkeypatch):
        payload = _capture_payload(
            monkeypatch,
            "http://127.0.0.1:11434/v1/chat/completions",
            "qwen3:8b",
            "What is an LLM?",
        )
        assert payload.get("reasoning_effort") == "none"
        assert "think" not in payload

    def test_explicit_low_effort_decision_survives_prompt_shaping(self, monkeypatch):
        payload = _capture_payload(
            monkeypatch,
            "http://127.0.0.1:11434/v1/chat/completions",
            "qwen3:8b",
            "Context includes previous turns. Answer the latest question concisely.",
            force_disable_thinking=True,
        )
        assert payload.get("reasoning_effort") == "none"
        assert "think" not in payload

    def test_no_think_for_ollama_v1_non_thinking_model(self, monkeypatch):
        """think must NOT be set for a plain (non-thinking) model on Ollama /v1."""
        payload = _capture_payload(
            monkeypatch, "http://127.0.0.1:11434/v1/chat/completions", "llama3.2:3b"
        )
        assert "reasoning_effort" not in payload

    def test_no_think_for_openai_endpoint_with_thinking_model_name(self, monkeypatch):
        """think must NOT leak to a real OpenAI endpoint even if the model name
        matches a thinking pattern — the URL guard is what matters."""
        payload = _capture_payload(
            monkeypatch, "https://api.openai.com/v1/chat/completions", "qwen3:14b"
        )
        assert "reasoning_effort" not in payload

    def test_reasoning_effort_none_for_non_default_port_thinking_model(self, monkeypatch):
        """Custom-port localhost Ollama (e.g. OLLAMA_HOST=0.0.0.0:11435) must
        also receive reasoning_effort:none — this is the regression guarded by the
        host-set check added in this fix."""
        payload = _capture_payload(
            monkeypatch, "http://127.0.0.1:11435/v1/chat/completions", "qwen3:14b"
        )
        assert payload.get("reasoning_effort") == "none"

    def test_complex_question_keeps_qwen_reasoning_enabled(self, monkeypatch):
        payload = _capture_payload(
            monkeypatch,
            "http://127.0.0.1:11434/v1/chat/completions",
            "qwen3:8b",
            "Explain why daylight saving time changes local clocks and compare its effects.",
        )
        assert "reasoning_effort" not in payload

    def test_action_request_keeps_qwen_reasoning_enabled(self, monkeypatch):
        payload = _capture_payload(
            monkeypatch,
            "http://127.0.0.1:11434/v1/chat/completions",
            "qwen3:8b",
            "Open Chrome and go to the current weather page.",
        )
        assert "reasoning_effort" not in payload

    def test_native_ollama_payload_can_disable_thinking_for_simple_turns(self):
        payload = llm_core._build_ollama_payload(
            "qwen3:8b",
            [{"role": "user", "content": "What time is it?"}],
            temperature=0.3,
            max_tokens=128,
            disable_thinking=True,
        )
        assert payload["think"] is False

    def test_simple_qwen_direct_route_sends_expected_ollama_settings(self, monkeypatch):
        payload = _capture_payload(
            monkeypatch,
            "http://127.0.0.1:11434/v1/chat/completions",
            "qwen3:8b",
            "What is an LLM?",
            temperature=1.0,
            max_tokens=192,
        )

        assert payload["stream"] is True
        assert payload["reasoning_effort"] == "none"
        assert payload["max_tokens"] == 192
        assert payload["temperature"] == 1.0

    def test_mark_answer_budget_reaches_openai_compatible_payload_without_enabling_reasoning(self, monkeypatch):
        from src.simple_intents import is_low_effort_conversation, reply_token_limit

        message = "Give me an 8-mark answer about the difference between an ocean and a sea."
        payload = _capture_payload(
            monkeypatch,
            "http://127.0.0.1:11434/v1/chat/completions",
            "qwen3:8b",
            message,
            force_disable_thinking=is_low_effort_conversation(message),
            temperature=1.0,
            max_tokens=reply_token_limit(192, message),
        )

        assert payload["reasoning_effort"] == "none"
        assert payload["max_tokens"] == 1024

    @pytest.mark.parametrize(
        "message",
        [
            "What is an LLM? Explain in two simple sentences.",
            "What is NLP? Explain simply.",
            "Define regression in one sentence.",
        ],
    )
    def test_bounded_educational_qwen_turn_sends_simple_answer_payload(self, monkeypatch, message):
        payload = _capture_payload(
            monkeypatch,
            "http://127.0.0.1:11434/v1/chat/completions",
            "qwen3:8b",
            message,
            temperature=1.0,
            max_tokens=192,
        )
        assert payload["reasoning_effort"] == "none"
        assert payload["max_tokens"] == 192

    def test_openai_compatible_payload_diagnostic_reports_sent_settings(self, caplog):
        payload = {
            "model": "qwen3:8b",
            "messages": [{"role": "user", "content": "private prompt"}],
            "stream": True,
            "stream_options": {"include_usage": True},
            "reasoning_effort": "none",
            "temperature": 0.4,
            "max_tokens": 192,
            "top_p": 0.9,
        }

        with caplog.at_level(logging.INFO, logger="request_timing"):
            llm_core._log_ollama_payload_settings(payload)

        record = next(r.message for r in caplog.records if "stage=ollama_payload" in r.message)
        assert "payload_format=openai_compatible" in record
        assert "think=not_sent" in record
        assert "reasoning_effort=none" in record
        assert "temperature=0.4" in record
        assert "max_tokens=192" in record
        assert "num_predict=not_applicable" in record
        assert "num_ctx=not_applicable" in record
        assert "top_p=0.9" in record
        assert "private prompt" not in record

    def test_simple_qwen_native_ollama_payload_sends_effective_options(self, monkeypatch):
        client = _FakeNativeClient()
        monkeypatch.setattr(llm_core, "_get_http_client", lambda: client)
        monkeypatch.setattr(llm_core, "_is_host_dead", lambda _url: False)
        monkeypatch.setattr(llm_core, "note_model_activity", lambda *a, **k: None)
        monkeypatch.setattr(llm_core, "_clear_host_dead", lambda *a, **k: None)
        monkeypatch.setattr(llm_core, "get_context_length", lambda *_a: 32768)

        async def run():
            return [chunk async for chunk in llm_core.stream_llm(
                "http://127.0.0.1:11434/api", "qwen3:8b",
                [{"role": "user", "content": "What is an LLM?"}],
                temperature=1.0,
                max_tokens=192,
            )]

        asyncio.run(run())
        payload = client.captured_payload
        assert payload["stream"] is True
        assert payload["think"] is False
        assert payload["options"] == {
            "temperature": 1.0,
            "num_predict": 192,
            "num_ctx": 32768,
        }
