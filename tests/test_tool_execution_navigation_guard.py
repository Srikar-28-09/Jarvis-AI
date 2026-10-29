import asyncio
import json

import pytest

import src.tool_execution as tool_execution
from src.tool_capabilities import ToolRunSecurityContext
from src.agent_tools import TOOL_HANDLERS, ToolBlock
from src.tool_execution import execute_tool_block


@pytest.mark.asyncio
async def test_request_blocks_repeated_and_concurrent_navigation(monkeypatch):
    context = ToolRunSecurityContext()
    entered = asyncio.Event()
    release = asyncio.Event()
    dispatches = []

    async def fake_dispatch(block, **kwargs):
        dispatches.append(block.tool_type)
        entered.set()
        await release.wait()
        return block.tool_type, {"success": True, "exit_code": 0}

    monkeypatch.setattr(tool_execution, "_execute_tool_block_impl", fake_dispatch)
    first = asyncio.create_task(execute_tool_block(
        ToolBlock("open_url", json.dumps({"url": "https://github.com/"})),
        security_context=context,
    ))
    await entered.wait()

    # The alternate browser MCP navigation spelling is protected by the same
    # request-scoped ledger while the first request is still in flight.
    duplicate = await execute_tool_block(
        ToolBlock("mcp__builtin_browser__browser_navigate", json.dumps({"url": "https://github.com"})),
        security_context=context,
    )
    assert duplicate[1]["duplicate_action"] is True
    assert duplicate[1]["navigation_status"] == "in_flight"
    assert dispatches == ["open_url"]

    release.set()
    await first

    repeated = await execute_tool_block(
        ToolBlock("open_url", json.dumps({"url": "HTTPS://GITHUB.COM:443/"})),
        security_context=context,
    )
    assert repeated[1]["duplicate_action"] is True
    assert repeated[1]["navigation_status"] == "completed"
    assert dispatches == ["open_url"]


@pytest.mark.asyncio
async def test_request_allows_distinct_urls_and_new_request_context(monkeypatch):
    dispatches = []

    async def fake_dispatch(block, **kwargs):
        dispatches.append(json.loads(block.content)["url"])
        return block.tool_type, {"success": True, "exit_code": 0}

    monkeypatch.setattr(tool_execution, "_execute_tool_block_impl", fake_dispatch)
    context = ToolRunSecurityContext()
    for url in ("https://github.com", "https://github.com/orgs/example"):
        await execute_tool_block(
            ToolBlock("open_url", json.dumps({"url": url})),
            security_context=context,
        )

    # A fresh request gets a fresh ledger, so a later explicit request to the
    # same URL remains allowed.
    await execute_tool_block(
        ToolBlock("open_url", json.dumps({"url": "https://github.com"})),
        security_context=ToolRunSecurityContext(),
    )
    assert dispatches == [
        "https://github.com",
        "https://github.com/orgs/example",
        "https://github.com",
    ]


@pytest.mark.asyncio
async def test_lost_navigation_response_remains_uncertain_and_is_not_retried(monkeypatch):
    context = ToolRunSecurityContext()
    dispatch_count = 0

    async def lost_response_dispatch(block, **kwargs):
        nonlocal dispatch_count
        dispatch_count += 1
        raise TimeoutError("client lost response after dispatch")

    monkeypatch.setattr(tool_execution, "_execute_tool_block_impl", lost_response_dispatch)
    block = ToolBlock("open_url", json.dumps({"url": "https://github.com"}))
    with pytest.raises(TimeoutError):
        await execute_tool_block(block, security_context=context)

    assert context.navigation_record("https://github.com")["status"] == "uncertain"
    duplicate = await execute_tool_block(block, security_context=context)
    assert duplicate[1]["duplicate_action"] is True
    assert duplicate[1]["execution_status_uncertain"] is True
    assert dispatch_count == 1


@pytest.mark.asyncio
async def test_agent_dispatch_passes_request_navigation_ledger_into_computer_use(monkeypatch):
    context = ToolRunSecurityContext()
    seen_contexts = []

    async def fake_computer_use(content, ctx=None):
        seen_contexts.append(ctx.get("request_security_context"))
        return {"success": True, "exit_code": 0}

    monkeypatch.setitem(TOOL_HANDLERS, "computer_use", fake_computer_use)
    monkeypatch.setattr(tool_execution, "_owner_is_admin", lambda owner: True)
    result = await execute_tool_block(
        ToolBlock("computer_use", json.dumps({"goal": "Open https://github.com"})),
        security_context=context,
    )

    assert result[1]["success"] is True
    assert seen_contexts == [context]
