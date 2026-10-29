import asyncio
import time
import pytest
from unittest.mock import AsyncMock, patch

from src.mcp_manager import McpManager


@pytest.mark.asyncio
async def test_lazy_mcp_registration_and_discovery():
    mgr = McpManager()
    mgr.register_lazy_builtin(
        server_id="builtin_test",
        name="Test Server",
        command="python",
        args=["fake.py"],
        tools=[
            {
                "name": "fake_tool",
                "description": "A fake tool",
                "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}},
            }
        ],
    )

    # Server status is ready (on-demand) and not yet connected
    status = mgr.get_server_status("builtin_test")
    assert status is not None
    assert status["status"] == "ready (on-demand)"
    assert "builtin_test" not in mgr._sessions

    # Tools are visible to agent prompt and UI
    tools = mgr.get_all_tools()
    assert any(t["name"] == "fake_tool" for t in tools)
    fake_tool = next(t for t in tools if t["name"] == "fake_tool")
    assert fake_tool["server_id"] == "builtin_test"

    # Register builtin_browser as lazy - it must be in OpenAI schemas and prompt desc
    mgr.register_lazy_builtin(
        server_id="builtin_browser",
        name="Built-in: Browser",
        command="npx",
        args=["fake"],
        tools=[{"name": "browser_navigate", "description": "Navigate to URL"}],
    )
    prompt_desc = mgr.get_tool_descriptions_for_prompt()
    assert "browser_navigate" in prompt_desc

    schemas = mgr.get_all_openai_schemas()
    assert any(s["function"]["name"] == "mcp__builtin_browser__browser_navigate" for s in schemas)


@pytest.mark.asyncio
async def test_lazy_mcp_reaping():
    mgr = McpManager()
    mgr.register_lazy_builtin(
        server_id="builtin_test",
        name="Test Server",
        command="python",
        args=["fake.py"],
        tools=[{"name": "fake_tool", "description": "A fake tool"}],
    )

    # Mock session connected
    mgr._sessions["builtin_test"] = object()
    mgr._connections["builtin_test"] = {"status": "connected"}
    mgr._last_used["builtin_test"] = time.time() - 400  # 400 seconds ago

    with patch.object(mgr, "disconnect_server", new_callable=AsyncMock) as mock_disc:
        reaped = await mgr.reap_idle_builtin_servers(max_idle_seconds=300)
        assert "builtin_test" in reaped
        mock_disc.assert_awaited_once_with("builtin_test")
