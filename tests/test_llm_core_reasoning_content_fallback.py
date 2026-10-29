"""Regression tests for reasoning_content fallback in non-streaming paths.

Covers the five cases requested during PR review:
  1. llm_call (sync): content="" + reasoning_content="..." → returns reasoning text
  2. llm_call_async (async): same
  3. Normal content wins over reasoning_content when both present
  4. Streaming agent path: reasoning-only round does NOT emit the generic error
  5. Streaming agent path: reasoning tokens are NOT duplicated as normal answer text
"""
import asyncio
import json

import httpx
import pytest

from src import llm_core


# ---------------------------------------------------------------------------
# Helpers: fake httpx responses for the non-streaming llm_call* paths
# ---------------------------------------------------------------------------

def _sync_response(payload: dict) -> httpx.Response:
    req = httpx.Request("POST", "http://test/v1/chat/completions")
    return httpx.Response(200, request=req, json=payload)


def _openai_msg(content, reasoning_content=None):
    msg = {"content": content}
    if reasoning_content is not None:
        msg["reasoning_content"] = reasoning_content
    return {"choices": [{"message": msg}]}


# ---------------------------------------------------------------------------
# 1. llm_call (sync): empty content → falls back to reasoning_content
# ---------------------------------------------------------------------------

def test_llm_call_returns_reasoning_content_when_content_empty(monkeypatch):
    monkeypatch.setattr(
        llm_core.httpx, "post",
        lambda *a, **kw: _sync_response(_openai_msg("", "I reasoned through it")),
    )
    result = llm_core.llm_call(
        "http://test/v1", "qwen3-8b",
        [{"role": "user", "content": "think"}],
    )
    assert result == "I reasoned through it"


# ---------------------------------------------------------------------------
# 2. llm_call_async (async): empty content → falls back to reasoning_content
# ---------------------------------------------------------------------------

def test_llm_call_async_returns_reasoning_content_when_content_empty(monkeypatch):
    class _FakeAsyncClient:
        async def post(self, *a, **kw):
            req = httpx.Request("POST", "http://test-async/v1/chat/completions")
            return httpx.Response(200, request=req,
                                  json=_openai_msg("", "async reasoning text"))

    monkeypatch.setattr(llm_core, "_get_http_client",
                        lambda: _FakeAsyncClient())

    result = asyncio.run(llm_core.llm_call_async(
        "http://test-async/v1", "qwen3-8b",
        [{"role": "user", "content": "think"}],
    ))
    assert result == "async reasoning text"


# ---------------------------------------------------------------------------
# 3. Normal content takes priority over reasoning_content when both present
# ---------------------------------------------------------------------------

def test_llm_call_content_wins_over_reasoning_content(monkeypatch):
    monkeypatch.setattr(
        llm_core.httpx, "post",
        lambda *a, **kw: _sync_response(
            _openai_msg("Normal answer", "some reasoning")
        ),
    )
    result = llm_core.llm_call(
        "http://test/v1", "some-model",
        [{"role": "user", "content": "hi"}],
    )
    assert result == "Normal answer"


# ---------------------------------------------------------------------------
# Streaming agent path tests (4 and 5)
# These import and test _empty_response_fallback — the real production helper
# extracted from stream_agent_loop.  If the fallback branch is reverted or
# changed, these tests will fail.
# ---------------------------------------------------------------------------

import sys
from unittest.mock import MagicMock

# Mock heavy DB/tool deps before importing agent_loop
for _mod in [
    "sqlalchemy", "sqlalchemy.orm", "sqlalchemy.ext",
    "sqlalchemy.ext.declarative", "sqlalchemy.ext.hybrid",
    "sqlalchemy.sql", "sqlalchemy.sql.expression",
    "src.database", "src.agent_tools",
    "core.models", "core.database",
]:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

from src.agent_loop import (  # noqa: E402
    _empty_response_fallback,
    _final_answer_recovery_messages,
    _should_recover_reasoning_only_response,
)


# ---------------------------------------------------------------------------
# 4. Reasoning-only round: generic error is suppressed
# ---------------------------------------------------------------------------

def test_stream_agent_reasoning_only_returns_explicit_no_answer_state():
    final_response, chunk = _empty_response_fallback(
        full_response="",
        round_reasoning="I reasoned carefully",
        tool_events=[],
    )
    assert chunk is not None
    assert "I reasoned carefully" not in chunk
    assert "final answer" in final_response.lower()
    assert "final answer" in chunk.lower()


# ---------------------------------------------------------------------------
# 5. Reasoning tokens are NOT re-emitted as a normal answer delta
# ---------------------------------------------------------------------------

def test_final_answer_recovery_does_not_include_reasoning():
    reasoning_text = "secret-token-4312"
    recovery = _final_answer_recovery_messages([
        {"role": "user", "content": "What is an LLM?"},
        {"role": "assistant", "content": "", "reasoning_content": reasoning_text},
    ])
    assert all(reasoning_text not in str(message) for message in recovery)
    assert "concise, direct final answer" in recovery[-1]["content"]


def test_existing_final_answer_is_preserved_by_empty_response_guard():
    answer = "A large language model is an AI model trained on text."
    final_response, chunk = _empty_response_fallback(
        full_response=answer,
        round_reasoning="some hidden reasoning",
        tool_events=[],
    )
    assert final_response == answer
    assert chunk is None


def test_reasoning_only_recovery_is_one_shot_eligible_only_for_safe_simple_turns():
    assert _should_recover_reasoning_only_response(
        "", "reasoning exists", [], "What is an LLM?"
    )
    assert not _should_recover_reasoning_only_response(
        "", "reasoning exists", [{"tool": "browser"}], "What is an LLM?"
    )
    assert not _should_recover_reasoning_only_response(
        "", "reasoning exists", [], "Open YouTube"
    )
    assert not _should_recover_reasoning_only_response(
        "already answered", "reasoning exists", [], "What is an LLM?"
    )
    assert not _should_recover_reasoning_only_response(
        "", "", [], "What is an LLM?"
    )
