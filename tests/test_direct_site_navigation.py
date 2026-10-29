"""Direct site navigation stays on the approved Host Bridge route."""

import json

import pytest

from src.action_intents import classify_tool_intent
from src.tool_capabilities import ToolRunSecurityContext, canonicalize_navigation_url
from src.agent_tools import ToolBlock
import src.tool_execution as tool_execution
from src.tool_execution import execute_tool_block


@pytest.mark.parametrize("utterance", ["open YouTube", "Please open YouTube", "go to YouTube"])
def test_open_youtube_is_direct_navigation_intent(utterance):
    intent = classify_tool_intent(utterance)
    assert intent.needs_tools
    assert intent.category == "browser"


def test_youtube_navigation_url_normalization():
    assert canonicalize_navigation_url("HTTPS://WWW.YOUTUBE.COM:443") == "https://www.youtube.com/"
    assert canonicalize_navigation_url("www.youtube.com") == "https://www.youtube.com/"


def test_direct_navigation_request_scope_can_limit_route_to_requested_site():
    context = ToolRunSecurityContext(navigation_allowlist={"https://www.youtube.com/"})
    allowed, _ = context.claim_navigation("https://www.youtube.com")
    blocked, result = context.claim_navigation("https://example.com")
    assert allowed is True
    assert blocked is False
    assert result["status"] == "blocked"


@pytest.mark.asyncio
async def test_youtube_navigation_dispatches_once_and_distinct_urls_remain_allowed(monkeypatch):
    dispatches = []

    async def dispatch(block, **kwargs):
        dispatches.append(json.loads(block.content)["url"])
        return block.tool_type, {"success": True, "exit_code": 0}

    monkeypatch.setattr(tool_execution, "_execute_tool_block_impl", dispatch)
    context = ToolRunSecurityContext()
    youtube = ToolBlock("open_url", json.dumps({"url": "https://www.youtube.com/"}))
    await execute_tool_block(youtube, security_context=context)
    duplicate = await execute_tool_block(
        ToolBlock("open_url", json.dumps({"url": "HTTPS://WWW.YOUTUBE.COM:443"})),
        security_context=context,
    )
    assert duplicate[1]["duplicate_action"] is True
    assert dispatches == ["https://www.youtube.com/"]

    await execute_tool_block(
        ToolBlock("open_url", json.dumps({"url": "https://www.youtube.com/watch?v=abc"})),
        security_context=context,
    )
    assert len(dispatches) == 2


@pytest.mark.asyncio
async def test_uncertain_youtube_navigation_is_not_retried(monkeypatch):
    calls = 0

    async def dispatch(block, **kwargs):
        nonlocal calls
        calls += 1
        raise TimeoutError("Host Bridge response lost")

    monkeypatch.setattr(tool_execution, "_execute_tool_block_impl", dispatch)
    context = ToolRunSecurityContext()
    block = ToolBlock("open_url", json.dumps({"url": "https://www.youtube.com/"}))
    with pytest.raises(TimeoutError):
        await execute_tool_block(block, security_context=context)
    duplicate = await execute_tool_block(block, security_context=context)
    assert duplicate[1]["execution_status_uncertain"] is True
    assert calls == 1


def test_direct_youtube_route_forces_host_bridge_tool_without_search():
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "routes/chat_routes.py").read_text(encoding="utf-8")
    assert 'if _direct_site_navigation:' in source
    assert '_forced_tools = {"open_url"}' in source
