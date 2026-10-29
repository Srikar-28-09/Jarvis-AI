"""
tests/test_computer_control_tools.py

Comprehensive tests for safe computer & filesystem control tools:
- ListDirectoryTool (list_directory)
- CreateDirectoryTool (create_directory)
- OpenApplicationTool (open_application)
- OpenUrlTool (open_url)
- TakeScreenshotTool (take_screenshot)

Tests cover:
1. Schema & registration validation
2. Capability & security categorization
3. Path containment to /workspace/jarvis
4. Path traversal (../, ..\\) rejection
5. Directory listing & creation
6. Application allowlist matching & rejection of arbitrary binaries
7. URL scheme validation (allow http/https, reject file/javascript/data/etc.)
8. Honest reporting of Docker container limitations for host GUI/display actions
"""

import asyncio
import json
import os
import shutil
import tempfile
import pytest
from unittest.mock import AsyncMock

from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
from src.agent_tools import TOOL_HANDLERS, TOOL_TAGS
from src.agent_tools.computer_control_tools import (
    ListDirectoryTool,
    CreateDirectoryTool,
    OpenApplicationTool,
    OpenUrlTool,
    TakeScreenshotTool,
    _resolve_jarvis_path,
    _APPLICATION_ALLOWLIST,
    JARVIS_WORKSPACE,
)
from src.tool_capabilities import (
    ToolEffect,
    ResultIntegrity,
    capabilities_for_tool,
)
from src.tool_security import (
    PLAN_MODE_READONLY_TOOLS,
    _PLAN_MODE_KNOWN_MUTATORS,
)


# ===========================================================================
# 1. Schema & Registration Tests
# ===========================================================================

def test_computer_control_schemas_exist():
    expected_tools = {
        "list_directory",
        "create_directory",
        "open_application",
        "open_url",
        "take_screenshot",
    }
    schema_names = {
        s["function"]["name"]
        for s in FUNCTION_TOOL_SCHEMAS
        if "function" in s and "name" in s["function"]
    }
    for tool_name in expected_tools:
        assert tool_name in schema_names, f"{tool_name} missing from FUNCTION_TOOL_SCHEMAS"


def test_computer_control_tool_handlers_registered():
    expected_tools = [
        "list_directory",
        "create_directory",
        "open_application",
        "open_url",
        "take_screenshot",
    ]
    for tool_name in expected_tools:
        assert tool_name in TOOL_HANDLERS, f"{tool_name} missing from TOOL_HANDLERS"
        assert callable(TOOL_HANDLERS[tool_name])
        assert tool_name in TOOL_TAGS, f"{tool_name} missing from TOOL_TAGS"


def test_capabilities_and_effects():
    caps = capabilities_for_tool("list_directory")
    assert ToolEffect.READ_WORKSPACE in caps.effects
    assert caps.result_integrity == ResultIntegrity.WORKSPACE_UNTRUSTED

    caps = capabilities_for_tool("create_directory")
    assert ToolEffect.WRITE_PRIVATE in caps.effects

    caps = capabilities_for_tool("open_application")
    assert ToolEffect.EXTERNAL_SIDE_EFFECT in caps.effects

    caps = capabilities_for_tool("open_url")
    assert ToolEffect.BROKERED_NETWORK_READ in caps.effects

    caps = capabilities_for_tool("take_screenshot")
    assert ToolEffect.READ_WORKSPACE in caps.effects


def test_plan_mode_partitions():
    assert "list_directory" in PLAN_MODE_READONLY_TOOLS
    assert "take_screenshot" in PLAN_MODE_READONLY_TOOLS
    assert "create_directory" in _PLAN_MODE_KNOWN_MUTATORS
    assert "open_application" in _PLAN_MODE_KNOWN_MUTATORS
    assert "open_url" in _PLAN_MODE_KNOWN_MUTATORS


# ===========================================================================
# 2. Path Security & Resolution Tests
# ===========================================================================

def test_resolve_jarvis_path_traversal_blocked():
    with pytest.raises(ValueError, match="path traversal"):
        _resolve_jarvis_path("../outside")

    with pytest.raises(ValueError, match="path traversal"):
        _resolve_jarvis_path("foo/../../bar")

    with pytest.raises(ValueError, match="path traversal"):
        _resolve_jarvis_path("..\\windows\\traversal")


def test_resolve_jarvis_path_empty_rejected():
    with pytest.raises(ValueError, match="path is required"):
        _resolve_jarvis_path("")


# ===========================================================================
# 3. ListDirectoryTool & CreateDirectoryTool Execution
# ===========================================================================

def test_list_and_create_directory_lifecycle(monkeypatch):
    async def _run():
        # Setup temporary directory simulating JARVIS_WORKSPACE
        with tempfile.TemporaryDirectory() as tmpdir:
            monkeypatch.setattr(
                "src.agent_tools.computer_control_tools.JARVIS_WORKSPACE",
                tmpdir,
            )

            create_tool = CreateDirectoryTool()
            list_tool = ListDirectoryTool()

            # 1. List empty workspace
            res = await list_tool.execute("")
            assert res["exit_code"] == 0
            assert "(empty)" in res["output"]

            # 2. Create subfolder 'projects/agent'
            res = await create_tool.execute(json.dumps({"path": "projects/agent"}))
            assert res["exit_code"] == 0
            assert res["created"] is True
            assert os.path.exists(os.path.join(tmpdir, "projects", "agent"))

            # 3. Create existing directory -> reports already exists
            res = await create_tool.execute(json.dumps({"path": "projects/agent"}))
            assert res["exit_code"] == 0
            assert res["created"] is False
            assert "already exists" in res["output"]

            # 4. Create a dummy file in the directory
            test_file = os.path.join(tmpdir, "projects", "test.txt")
            with open(test_file, "w") as f:
                f.write("Hello Odysseus")

            # 5. List 'projects' directory
            res = await list_tool.execute(json.dumps({"path": "projects"}))
            assert res["exit_code"] == 0
            assert "agent/" in res["output"]
            assert "test.txt" in res["output"]

            # 6. Test path traversal rejection in execution
            res = await list_tool.execute(json.dumps({"path": "../../etc"}))
            assert res["exit_code"] == 1
            assert "path traversal" in res["error"]

            res = await create_tool.execute(json.dumps({"path": "../escaped"}))
            assert res["exit_code"] == 1
            assert "path traversal" in res["error"]

    asyncio.run(_run())


# ===========================================================================
# 4. OpenApplicationTool Execution
# ===========================================================================

def test_open_application_allowlist(monkeypatch):
    async def _run():
        tool = OpenApplicationTool()

        # Mock host bridge execution as successful
        async def mock_execute(action, params=None, timeout=12.0):
            return {"success": True, "application": params.get("application")}

        monkeypatch.setattr("src.host_bridge_client.execute_host_action", mock_execute)

        # Valid apps
        for app in ["vscode", "chrome", "firefox", "edge", "notepad", "calculator", "terminal", "explorer"]:
            res = await tool.execute(json.dumps({"application": app}))
            assert res["exit_code"] == 0
            assert "Successfully launched" in res["output"]

        # Natural aliases and .exe extensions
        for alias in ["Visual Studio Code", "notepad.exe", "Notepad", "NOTEPAD.EXE", "calc.exe", "Google Chrome"]:
            res = await tool.execute(alias)
            assert res["exit_code"] == 0

        res = await tool.execute("Visual Studio Code")
        assert res["exit_code"] == 0
        assert res["application"] == "VS Code"

        res = await tool.execute("Google Chrome")
        assert res["exit_code"] == 0
        assert res["application"] == "Google Chrome"

        # Arbitrary malicious executable / unapproved app -> blocked before bridge
        res = await tool.execute("powershell.exe")
        assert res["exit_code"] == 1
        assert "not in the allowed application list" in res["error"]

        res = await tool.execute("cmd.exe")
        assert res["exit_code"] == 1
        assert "not in the allowed application list" in res["error"]

        res = await tool.execute(json.dumps({"application": "C:\\Windows\\System32\\cmd.exe"}))
        assert res["exit_code"] == 1
        assert "not in the allowed application list" in res["error"]

    asyncio.run(_run())


def test_open_application_preserves_host_bridge_evidence(monkeypatch):
    """Verify OpenApplicationTool preserves success, focused, hwnd, and pid from Host Bridge."""
    async def _run():
        tool = OpenApplicationTool()

        # 1. Existing instance reused & focused
        async def mock_reuse(action, params=None, timeout=12.0):
            return {
                "success": True,
                "action": "open_application",
                "application": "Notepad",
                "already_running": True,
                "focused": True,
                "hwnd": 0x10204,
                "pid": 4321,
                "message": "Notepad is already running (focused); reused existing instance",
            }

        monkeypatch.setattr("src.host_bridge_client.execute_host_action", mock_reuse)
        res = await tool.execute("notepad")
        assert res["success"] is True
        assert res["exit_code"] == 0
        assert res["application"] == "Notepad"
        assert res["command"] == "notepad"
        assert res["already_running"] is True
        assert res["focused"] is True
        assert res["hwnd"] == 0x10204
        assert res["pid"] == 4321
        assert "already running" in res["output"]

        # 2. Fresh launch
        async def mock_spawn(action, params=None, timeout=12.0):
            return {
                "success": True,
                "action": "open_application",
                "application": "Notepad",
                "already_running": False,
                "focused": None,
                "hwnd": None,
                "pid": 8765,
                "message": "Successfully launched Notepad on Windows host",
            }

        monkeypatch.setattr("src.host_bridge_client.execute_host_action", mock_spawn)
        res_spawn = await tool.execute("notepad")
        assert res_spawn["success"] is True
        assert res_spawn["exit_code"] == 0
        assert res_spawn["application"] == "Notepad"
        assert res_spawn["already_running"] is False
        assert res_spawn["pid"] == 8765

    asyncio.run(_run())


def test_open_application_tool_preserves_uncertain_execution_status(monkeypatch):
    async def _run():
        tool = OpenApplicationTool()

        async def mock_timeout(action, params=None, timeout=12.0):
            assert action == "open_application"
            assert params["application"] == "Google Chrome"
            return {
                "success": False,
                "bridge_available": False,
                "execution_status_uncertain": True,
                "error": "response timed out after launch",
            }

        monkeypatch.setattr("src.host_bridge_client.execute_host_action", mock_timeout)
        result = await tool.execute(json.dumps({"application": "Chrome"}))
        assert result["exit_code"] == 1
        assert result["execution_status_uncertain"] is True

    asyncio.run(_run())


# ===========================================================================
# 5. OpenUrlTool Execution
# ===========================================================================

def test_open_url_safety(monkeypatch):
    async def _run():
        tool = OpenUrlTool()

        async def mock_execute(action, params=None, timeout=12.0):
            return {"success": True, "url": params.get("url")}

        monkeypatch.setattr("src.host_bridge_client.execute_host_action", mock_execute)

        # Safe URLs
        res = await tool.execute("https://github.com/google")
        assert res["exit_code"] == 0
        assert "Successfully opened" in res["output"]
        assert res["scheme"] == "https"
        assert res["host"] == "github.com"

        res = await tool.execute(json.dumps({"url": "http://example.com/page"}))
        assert res["exit_code"] == 0
        assert res["scheme"] == "http"

        # Auto-prefix https if missing scheme
        res = await tool.execute("wikipedia.org")
        assert res["exit_code"] == 0
        assert res["scheme"] == "https"
        assert res["host"] == "wikipedia.org"

        # Dangerous schemes -> rejected before bridge
        dangerous_urls = [
            "file:///C:/Windows/System32/calc.exe",
            "file:///etc/passwd",
            "javascript:alert(1)",
            "data:text/html,<script>alert(1)</script>",
            "powershell://invoke-expression",
            "cmd://dir",
            "telnet://127.0.0.1",
            "smb://192.168.1.1/share",
        ]
        for url in dangerous_urls:
            res = await tool.execute(url)
            assert res["exit_code"] == 1
            assert "not allowed for security reasons" in res["error"] or "unsupported URL scheme" in res["error"]

    asyncio.run(_run())


def test_open_url_preserves_host_bridge_evidence(monkeypatch):
    """Verify OpenUrlTool preserves success and Host Bridge evidence."""
    async def _run():
        tool = OpenUrlTool()

        async def mock_execute(action, params=None, timeout=12.0):
            return {
                "success": True,
                "action": "open_url",
                "url": params.get("url"),
                "focused": True,
                "hwnd": 0x20304,
                "pid": 9999,
            }

        monkeypatch.setattr("src.host_bridge_client.execute_host_action", mock_execute)
        res = await tool.execute("https://github.com/google")
        assert res["success"] is True
        assert res["exit_code"] == 0
        assert res["url"] == "https://github.com/google"
        assert res["scheme"] == "https"
        assert res["host"] == "github.com"
        assert res["focused"] is True
        assert res["hwnd"] == 0x20304
        assert res["pid"] == 9999
        assert "Successfully opened" in res["output"]

    asyncio.run(_run())


def test_open_url_tool_preserves_uncertain_host_bridge_status(monkeypatch):
    async def _run():
        tool = OpenUrlTool()

        async def timed_out_after_dispatch(action, params=None, timeout=12.0):
            return {
                "success": False,
                "action": "open_url",
                "error": "response timed out",
                "execution_status_uncertain": True,
            }

        monkeypatch.setattr("src.host_bridge_client.execute_host_action", timed_out_after_dispatch)
        result = await tool.execute("https://github.com")
        assert result["exit_code"] == 1
        assert result["execution_status_uncertain"] is True

    asyncio.run(_run())


# ===========================================================================
# 6. TakeScreenshotTool Execution
# ===========================================================================

def test_take_screenshot_reports_limitation(monkeypatch):
    async def _run():
        tool = TakeScreenshotTool()

        # 1. When bridge is unavailable
        async def mock_unavailable(action, params=None, timeout=12.0):
            return {"success": False, "bridge_available": False, "error": "Windows host bridge unavailable"}

        monkeypatch.setattr("src.host_bridge_client.execute_host_action", mock_unavailable)
        res = await tool.execute("")
        assert res["exit_code"] == 1
        assert res["available"] is False
        assert "Windows host bridge unavailable" in res["error"]

        # 2. When bridge is available
        async def mock_available(action, params=None, timeout=12.0):
            return {
                "success": True,
                "width": 1920,
                "height": 1080,
                "size_bytes": 45000,
                "file_path": "/workspace/jarvis/screenshots/test.jpg",
                "image_base64": "fake_b64",
            }

        monkeypatch.setattr("src.host_bridge_client.execute_host_action", mock_available)
        res = await tool.execute("")
        assert res["exit_code"] == 0
        assert res["width"] == 1920
        assert res["height"] == 1080
        assert "/workspace/jarvis/screenshots/test.jpg" in res["file_path"]

    asyncio.run(_run())


def test_take_screenshot_format_result_excludes_raw_base64():
    """Verify that formatting a screenshot tool result does not dump huge base64 into text context."""
    from src.tool_execution import format_tool_result

    huge_b64 = "A" * 50000
    tool_result = {
        "output": "Captured desktop screenshot (1536x864, 21543 bytes). Saved to: /workspace/jarvis/screenshots/screenshot_1.jpg",
        "exit_code": 0,
        "file_path": "/workspace/jarvis/screenshots/screenshot_1.jpg",
        "width": 1536,
        "height": 864,
        "format": "jpeg",
        "size_bytes": 21543,
        "image_base64": huge_b64,
    }

    formatted = format_tool_result("take_screenshot: desktop", tool_result)

    # Output message must be present
    assert "Captured desktop screenshot" in formatted
    # Raw huge base64 payload must NOT be present in formatted text for LLM
    assert huge_b64 not in formatted
    assert "AAAAA" not in formatted
    # Context size should be small and tidy
    assert len(formatted) < 500


def test_take_screenshot_returns_real_image_without_hidden_vision_call(monkeypatch):
    """Screenshot capture returns bridge image data; the observation owner analyzes it once."""
    async def _run():
        tool = TakeScreenshotTool()

        async def mock_bridge_action(action, params=None, timeout=12.0):
            assert action == "screenshot"
            return {
                "success": True,
                "width": 1920,
                "height": 1080,
                "size_bytes": 12345,
                "file_path": "/workspace/jarvis/screenshots/test.jpg",
                "image_base64": "valid_mock_b64",
            }

        monkeypatch.setattr("src.host_bridge_client.execute_host_action", mock_bridge_action)
        analyzer = AsyncMock()
        monkeypatch.setattr("src.vision_analyzer.analyze_screenshot", analyzer)

        res = await tool.execute('{"query": "what windows are open?"}')
        assert res["exit_code"] == 0
        assert res["width"] == 1920
        assert res["height"] == 1080
        assert res["image_base64"] == "valid_mock_b64"
        assert res["output"].startswith("Captured desktop screenshot")
        analyzer.assert_not_awaited()

        async def missing_image(action, params=None, timeout=12.0):
            return {"success": True, "width": 10, "height": 10, "image_base64": ""}

        monkeypatch.setattr("src.host_bridge_client.execute_host_action", missing_image)
        missing = await tool.execute("")
        assert missing["exit_code"] == 1
        assert "no screenshot image data" in missing["error"]

    asyncio.run(_run())


def test_screenshot_alias_registration():
    """Verify screenshot tool alias is present in tool schemas and handlers."""
    from src.agent_tools import TOOL_HANDLERS, TOOL_TAGS, FUNCTION_TOOL_SCHEMAS
    from src.agent_loop import _DOMAIN_TOOL_MAP
    from src.tool_security import PLAN_MODE_READONLY_TOOLS

    assert "screenshot" in TOOL_HANDLERS
    assert "take_screenshot" in TOOL_HANDLERS
    assert "screenshot" in TOOL_TAGS
    assert "screenshot" in _DOMAIN_TOOL_MAP["files"]
    assert "screenshot" in PLAN_MODE_READONLY_TOOLS

    schema_names = {s["function"]["name"] for s in FUNCTION_TOOL_SCHEMAS}
    assert "take_screenshot" in schema_names
    assert "screenshot" in schema_names


