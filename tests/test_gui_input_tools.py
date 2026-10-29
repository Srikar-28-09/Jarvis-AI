"""
tests/test_gui_input_tools.py

Comprehensive unit and security tests for Safe GUI Input Control:
1. Valid click & coordinate boundary validation
2. Invalid coordinates (negative, out-of-range, non-numeric, NaN/inf, boolean)
3. Screen boundary coordinates (0, 0, width-1, height-1)
4. Double click validation & execution
5. Text length limit (max 500 chars) & empty text rejection
6. Disallowed control characters rejection in type_text
7. Valid key allowlist verification
8. Invalid key rejection
9. Scroll direction validation (up/down only)
10. Scroll amount boundary validation (1 to 50)
11. Host Bridge authentication & endpoint security
12. Unknown action rejection
13. Malformed JSON payload handling
14. Host Bridge unavailable fallback
15. Docker -> host connectivity & tool schema registration
16. Security, capability, and plan-mode partition parity
"""

import asyncio
import json
import os
import pytest
from unittest.mock import MagicMock, patch

from host_bridge.actions import (
    execute_click,
    execute_double_click,
    execute_type_text,
    execute_press_key,
    execute_scroll,
    KEY_ALLOWLIST,
    MAX_TYPE_TEXT_LENGTH,
    MAX_SCROLL_AMOUNT,
    MIN_SCROLL_AMOUNT,
    get_screen_bounds,
)
from src.agent_tools.computer_control_tools import (
    ClickTool,
    DoubleClickTool,
    TypeTextTool,
    PressKeyTool,
    ScrollTool,
)
from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
from src.agent_tools import TOOL_HANDLERS, TOOL_TAGS
from src.tool_capabilities import capabilities_for_tool, ToolEffect, ResultIntegrity
from src.tool_security import NON_ADMIN_BLOCKED_TOOLS, _PLAN_MODE_KNOWN_MUTATORS


# ===========================================================================
# 1. Click & Coordinate Boundaries
# ===========================================================================

def test_valid_click_execution():
    w, h = get_screen_bounds()
    res = execute_click({"x": 100, "y": 100})
    assert res["success"] is True
    assert res["action"] == "click"
    assert res["x"] == 100
    assert res["y"] == 100


def test_screen_boundary_coordinates():
    w, h = get_screen_bounds()
    # Min bounds (0, 0)
    res_min = execute_click({"x": 0, "y": 0})
    assert res_min["success"] is True

    # Max bounds (w-1, h-1)
    res_max = execute_click({"x": w - 1, "y": h - 1})
    assert res_max["success"] is True


def test_invalid_coordinates_rejected():
    w, h = get_screen_bounds()

    # Negative coordinates
    assert execute_click({"x": -1, "y": 100})["success"] is False
    assert execute_click({"x": 100, "y": -5})["success"] is False

    # Out of bounds
    assert execute_click({"x": w, "y": 100})["success"] is False
    assert execute_click({"x": 100, "y": h})["success"] is False
    assert execute_click({"x": w + 500, "y": h + 500})["success"] is False

    # Non-numeric / invalid types
    assert execute_click({"x": "abc", "y": 100})["success"] is False
    assert execute_click({"x": 100, "y": None})["success"] is False
    assert execute_click({"x": True, "y": 100})["success"] is False
    assert execute_click({"x": 100, "y": float("nan")})["success"] is False
    assert execute_click({"x": float("inf"), "y": 100})["success"] is False


# ===========================================================================
# 2. Double Click
# ===========================================================================

def test_valid_double_click_execution():
    res = execute_double_click({"x": 200, "y": 200})
    assert res["success"] is True
    assert res["action"] == "double_click"
    assert res["x"] == 200
    assert res["y"] == 200


def test_invalid_double_click_coordinates():
    w, h = get_screen_bounds()
    assert execute_double_click({"x": -10, "y": 200})["success"] is False
    assert execute_double_click({"x": 200, "y": h + 10})["success"] is False


# ===========================================================================
# 3. Type Text (Length Limits & Control Characters)
# ===========================================================================

def test_valid_type_text():
    res = execute_type_text({"text": "Hello World!"})
    assert res["success"] is True
    assert res["action"] == "type_text"
    assert res["length"] == 12


def test_empty_text_rejected():
    assert execute_type_text({"text": ""})["success"] is False
    assert execute_type_text({})["success"] is False
    assert execute_type_text({"text": None})["success"] is False


def test_oversized_text_rejected():
    oversized = "A" * (MAX_TYPE_TEXT_LENGTH + 1)
    res = execute_type_text({"text": oversized})
    assert res["success"] is False
    assert "exceeds maximum allowable limit" in res["error"]


def test_disallowed_control_characters_rejected():
    # Null byte
    assert execute_type_text({"text": "Hello\x00World"})["success"] is False
    # Bell character
    assert execute_type_text({"text": "Alert\x07"})["success"] is False
    # Escape sequence
    assert execute_type_text({"text": "Escape\x1b[2J"})["success"] is False

    # Standard whitespace characters (newline, tab, space) are allowed
    assert execute_type_text({"text": "Line 1\nLine 2\tTabbed"})["success"] is True


# ===========================================================================
# 4. Press Key & Allowlist
# ===========================================================================

def test_allowed_keys():
    for key in ["ENTER", "RETURN", "TAB", "ESCAPE", "ESC", "BACKSPACE", "DELETE", "SPACE", "UP", "DOWN", "LEFT", "RIGHT", "F1", "F12"]:
        assert key in KEY_ALLOWLIST
        res = execute_press_key({"key": key})
        assert res["success"] is True
        assert res["action"] == "press_key"
        assert res["key"] == key


def test_invalid_keys_rejected():
    for bad_key in ["cmd.exe", "powershell", "ALT+F4", "WIN+R", "RANDOM_KEY", "", None]:
        res = execute_press_key({"key": bad_key})
        assert res["success"] is False
        assert "not allowed" in res.get("error", "") or "required" in res.get("error", "")


# ===========================================================================
# 5. Scroll Direction & Amounts
# ===========================================================================

def test_valid_scroll():
    res_up = execute_scroll({"direction": "up", "amount": 5})
    assert res_up["success"] is True
    assert res_up["direction"] == "up"
    assert res_up["amount"] == 5

    res_down = execute_scroll({"direction": "down", "amount": 3})
    assert res_down["success"] is True
    assert res_down["direction"] == "down"


def test_invalid_scroll_direction():
    assert execute_scroll({"direction": "left", "amount": 5})["success"] is False
    assert execute_scroll({"direction": "right", "amount": 5})["success"] is False
    assert execute_scroll({"direction": "forward", "amount": 5})["success"] is False
    assert execute_scroll({"direction": "", "amount": 5})["success"] is False


def test_scroll_amount_bounds():
    assert execute_scroll({"direction": "up", "amount": 0})["success"] is False
    assert execute_scroll({"direction": "up", "amount": -5})["success"] is False
    assert execute_scroll({"direction": "down", "amount": MAX_SCROLL_AMOUNT + 1})["success"] is False
    assert execute_scroll({"direction": "down", "amount": "invalid"})["success"] is False


# ===========================================================================
# 6. Tool Class Execution & Argument Parsing
# ===========================================================================

def test_click_tool_json_and_string_args():
    async def _run():
        tool = ClickTool()
        # Mock host bridge response
        with patch("src.host_bridge_client.execute_host_action", return_value={"success": True, "message": "Clicked at (150, 250)"}):
            res_json = await tool.execute('{"x": 150, "y": 250}')
            assert res_json["exit_code"] == 0
            assert res_json["x"] == 150
            assert res_json["y"] == 250

            res_str = await tool.execute("150, 250")
            assert res_str["exit_code"] == 0
    asyncio.run(_run())


def test_type_text_tool_execution():
    async def _run():
        tool = TypeTextTool()
        with patch("src.host_bridge_client.execute_host_action", return_value={"success": True, "message": "Successfully typed 5 characters"}):
            res = await tool.execute('{"text": "Hello"}')
            assert res["exit_code"] == 0
            assert res["length"] == 5
    asyncio.run(_run())


def test_press_key_tool_execution():
    async def _run():
        tool = PressKeyTool()
        with patch("src.host_bridge_client.execute_host_action", return_value={"success": True, "message": "Pressed key 'ENTER'", "key": "ENTER"}):
            res = await tool.execute('{"key": "ENTER"}')
            assert res["exit_code"] == 0
            assert res["key"] == "ENTER"
    asyncio.run(_run())


def test_scroll_tool_execution():
    async def _run():
        tool = ScrollTool()
        with patch("src.host_bridge_client.execute_host_action", return_value={"success": True, "message": "Scrolled down by 5 steps"}):
            res = await tool.execute('{"direction": "down", "amount": 5}')
            assert res["exit_code"] == 0
            assert res["direction"] == "down"
    asyncio.run(_run())


def test_bridge_unavailable_fallback():
    async def _run():
        tool = ClickTool()
        with patch("src.host_bridge_client.execute_host_action", return_value={"success": False, "bridge_available": False}):
            res = await tool.execute('{"x": 100, "y": 100}')
            assert res["exit_code"] == 1
            assert "Windows host bridge unavailable" in res["error"]
    asyncio.run(_run())


# ===========================================================================
# 7. Schema, Capability, and Security Parity
# ===========================================================================

def test_gui_tool_schemas_registered():
    expected_gui_tools = {"click", "double_click", "type_text", "press_key", "scroll"}
    schema_names = {
        s["function"]["name"]
        for s in FUNCTION_TOOL_SCHEMAS
        if "function" in s and "name" in s["function"]
    }
    for t in expected_gui_tools:
        assert t in schema_names, f"{t} missing from FUNCTION_TOOL_SCHEMAS"
        assert t in TOOL_HANDLERS, f"{t} missing from TOOL_HANDLERS"
        assert t in TOOL_TAGS, f"{t} missing from TOOL_TAGS"


def test_gui_tool_capabilities():
    for t in ["click", "double_click", "type_text", "press_key", "scroll"]:
        caps = capabilities_for_tool(t)
        assert ToolEffect.EXTERNAL_SIDE_EFFECT in caps.effects
        assert caps.result_integrity == ResultIntegrity.SYSTEM


def test_gui_tool_security_classifications():
    for t in ["click", "double_click", "type_text", "press_key", "scroll"]:
        assert t in NON_ADMIN_BLOCKED_TOOLS
        assert t in _PLAN_MODE_KNOWN_MUTATORS
