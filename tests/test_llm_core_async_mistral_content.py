"""Integration regression tests for Mistral's structured reasoning output.

llm_call_async must normalize Mistral structured content to user-facing text,
matching llm_call (sync) and stream_llm, without returning the private thinking
block as an answer.
"""
import asyncio

import pytest
from fastapi import HTTPException

import src.llm_core as llm_core


class _FakeResponse:
    is_success = True
    status_code = 200
    text = ""

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def _payload(content):
    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


def _call(monkeypatch, content):
    async def fake_post(client, url, headers, **kwargs):
        return _FakeResponse(_payload(content))

    monkeypatch.setattr(llm_core, "httpx_post_kimi_aware_async", fake_post)
    llm_core._response_cache.clear()
    return asyncio.run(llm_core.llm_call_async(
        "http://mistral.test/v1/chat/completions",
        "mistral-medium",
        [{"role": "user", "content": "q"}],
    ))


def test_llm_call_async_normalizes_mistral_structured_content(monkeypatch):
    out = _call(monkeypatch, [
        {"type": "thinking",
         "thinking": [{"type": "text", "text": "Let me work through this..."}],
         "closed": True},
        {"type": "text", "text": "The answer is 42."},
    ])
    assert isinstance(out, str), f"expected str, got {type(out).__name__}"
    assert "The answer is 42." in out
    assert "Let me work through this..." not in out
    assert all(isinstance(v, str) for v in llm_core._response_cache.values())


def test_llm_call_async_thinking_only_fails_without_exposing_reasoning(monkeypatch):
    with pytest.raises(HTTPException, match="no final answer"):
        _call(monkeypatch, [
            {"type": "thinking",
             "thinking": [{"type": "text", "text": "still thinking"}],
             "closed": True},
        ])


def test_llm_call_async_plain_string_passthrough(monkeypatch):
    out = _call(monkeypatch, "plain answer")
    assert out == "plain answer"
