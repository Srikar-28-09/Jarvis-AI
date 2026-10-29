"""
tests/test_jarvis_desktop_hotkey_integration.py

Comprehensive tests for the Jarvis desktop experience:
- Global hotkey parsing, configuration, and Win32 RegisterHotKey management
- Single-instance Jarvis window detection, foreground restore, and App Mode launch
- Anti-duplicate window launch guards
- Frontend focus handler existence
- General Agent computer_use capability integration & tool index routing
"""

import os
import re
from unittest.mock import MagicMock, patch

from host_bridge.actions import focus_or_launch_jarvis_window
from host_bridge.config import (
    JARVIS_HOTKEY,
    JARVIS_HOTKEY_ENABLED,
    JARVIS_UI_URL,
)
from host_bridge.hotkey import (
    MOD_ALT,
    MOD_CONTROL,
    MOD_SHIFT,
    MOD_WIN,
    Win32GlobalHotkey,
    parse_hotkey_string,
)
from src.agent_loop import (
    _AGENT_RULES,
    _API_AGENT_RULES,
    _DOMAIN_RULES,
    _looks_like_local_computer_request,
)
from src.tool_index import ToolIndex


def test_hotkey_parsing_ctrl_alt_space():
    """Verify Ctrl+Alt+Space parses into correct Win32 modifier bitmask and VK_SPACE."""
    mods, vk = parse_hotkey_string("Ctrl+Alt+Space")
    assert mods == (MOD_CONTROL | MOD_ALT)
    assert vk == 0x20  # VK_SPACE


def test_hotkey_parsing_custom_combinations():
    """Verify custom shortcuts like Win+Alt+J or Ctrl+Shift+F1 parse accurately."""
    mods, vk = parse_hotkey_string("Win+Alt+J")
    assert mods == (MOD_WIN | MOD_ALT)
    assert vk == ord("J")

    mods, vk = parse_hotkey_string("Ctrl+Shift+F5")
    assert mods == (MOD_CONTROL | MOD_SHIFT)
    assert vk == 0x74  # VK_F5


def test_hotkey_parsing_invalid_combinations():
    """Verify invalid hotkey strings raise ValueError with descriptive errors."""
    import pytest
    with pytest.raises(ValueError, match="cannot be empty"):
        parse_hotkey_string("")

    with pytest.raises(ValueError, match="Unrecognized key"):
        parse_hotkey_string("Ctrl+Alt+UnknownKey123")

    with pytest.raises(ValueError, match="No valid trigger key"):
        parse_hotkey_string("Ctrl+Alt")


def test_hotkey_configuration_defaults():
    """Verify default hotkey configuration in host_bridge/config.py."""
    assert JARVIS_HOTKEY == "Ctrl+Alt+Space"
    assert "7000" in JARVIS_UI_URL
    assert JARVIS_HOTKEY_ENABLED is True


def test_hotkey_listener_lifecycle_mocked():
    """Verify Win32GlobalHotkey start, trigger callback, and stop lifecycle with Win32 mocks."""
    import threading
    called = []
    stop_event = threading.Event()

    def on_press():
        called.append(True)

    hotkey = Win32GlobalHotkey(
        hotkey_str="Ctrl+Alt+Space",
        callback=on_press,
        hotkey_id=9999,
    )

    mock_user32 = MagicMock()
    mock_kernel32 = MagicMock()
    mock_kernel32.GetCurrentThreadId.return_value = 1234
    mock_user32.RegisterHotKey.return_value = 1
    mock_user32.UnregisterHotKey.return_value = 1

    def mock_get_message(msg_ptr, hwnd, min_msg, max_msg):
        # Wait until stop_event is set or timeout, then return 0 (WM_QUIT)
        stop_event.wait(timeout=1.0)
        return 0

    mock_user32.GetMessageW.side_effect = mock_get_message

    with patch("ctypes.windll", create=True) as mock_windll:
        mock_windll.user32 = mock_user32
        mock_windll.kernel32 = mock_kernel32

        success = hotkey.start()
        assert success is True
        assert hotkey.hotkey_id == 9999

        stop_event.set()
        hotkey.stop()
        assert hotkey.is_running is False
        mock_user32.UnregisterHotKey.assert_called()



def test_focus_or_launch_jarvis_window_finds_and_focuses_existing():
    """Verify focus_or_launch_jarvis_window restores and sets foreground when Jarvis window exists."""
    mock_user32 = MagicMock()
    mock_user32.IsWindowVisible.return_value = True
    mock_user32.GetWindowTextLengthW.return_value = len("Odysseus - Personal AI")

    def mock_enum_windows(cb, lparam):
        # Trigger callback with a fake HWND
        cb(12345, lparam)
        return 1

    mock_user32.EnumWindows.side_effect = mock_enum_windows
    mock_user32.GetWindowTextW = lambda hwnd, buf, length: setattr(buf, "value", "Odysseus - Personal AI")

    with patch("ctypes.windll.user32", mock_user32):
        res = focus_or_launch_jarvis_window(url="http://localhost:7000")
        assert res["success"] is True
        assert res["action"] == "focused"
        assert res["hwnd"] == 12345
        mock_user32.ShowWindow.assert_called_with(12345, 9)  # SW_RESTORE
        mock_user32.SetForegroundWindow.assert_called_with(12345)


def test_focus_or_launch_jarvis_window_launches_app_mode_when_missing():
    """Verify focus_or_launch_jarvis_window launches browser in App Mode if no existing window is found."""
    mock_user32 = MagicMock()
    # No matching windows
    mock_user32.EnumWindows.side_effect = lambda cb, lparam: 1

    with patch("ctypes.windll.user32", mock_user32), \
         patch("host_bridge.actions._LAST_JARVIS_LAUNCH_TIME", 0.0), \
         patch("subprocess.Popen") as mock_popen:

        res = focus_or_launch_jarvis_window(url="http://localhost:7000")
        assert res["success"] is True
        assert res["action"] == "launched"
        assert "App Mode" in res["method"]
        mock_popen.assert_called_once()
        args = mock_popen.call_args[0][0]
        assert "--app=http://localhost:7000" in args[1]
        assert mock_popen.call_args[1].get("shell") is False


def test_focus_or_launch_jarvis_window_debounces_rapid_repeated_presses():
    """Verify repeated rapid hotkey presses do not spawn duplicate browser processes."""
    mock_user32 = MagicMock()
    mock_user32.EnumWindows.side_effect = lambda cb, lparam: 1

    with patch("ctypes.windll.user32", mock_user32), \
         patch("subprocess.Popen"):

        # First press launches
        res1 = focus_or_launch_jarvis_window(url="http://localhost:7000")
        assert res1["action"] == "launched"

        # Immediate second press within debounce window is debounced
        res2 = focus_or_launch_jarvis_window(url="http://localhost:7000")
        assert res2["action"] == "debounced"


def test_frontend_static_window_focus_listener_exists():
    """Verify static/app.js contains window focus handler targeting #chat-input."""
    with open("static/app.js", "r", encoding="utf-8") as f:
        app_js = f.read()

    assert "window.addEventListener('focus'" in app_js or 'window.addEventListener("focus"' in app_js
    assert "chat-input" in app_js


def test_agent_rules_include_computer_use_instruction():
    """Verify general Agent rules include explicit computer_use guidance for Windows desktop tasks."""
    assert "computer_use" in _AGENT_RULES
    assert "computer_use" in _API_AGENT_RULES
    assert "computer_use" in _DOMAIN_RULES["files"]
    assert "desktop" in _DOMAIN_RULES["files"].lower()


def test_local_computer_detection_matches_desktop_queries():
    """Verify _looks_like_local_computer_request detects desktop application and GUI requests."""
    assert _looks_like_local_computer_request("Open Notepad and type Hello") is True
    assert _looks_like_local_computer_request("Launch VS Code") is True
    assert _looks_like_local_computer_request("Open calculator") is True
    assert _looks_like_local_computer_request("Start terminal") is True
    assert _looks_like_local_computer_request("Perform desktop task") is True
    assert _looks_like_local_computer_request("What is the capital of France?") is False


def test_tool_index_keyword_hints_select_computer_use():
    """Verify ToolIndex keyword hints map desktop application requests to computer_use."""
    desktop_hints = [
        tools for kws, tools in ToolIndex._KEYWORD_HINTS.items()
        if any("notepad" in kw or "desktop" in kw or "open app" in kw for kw in kws)
    ]
    assert len(desktop_hints) >= 2
    all_selected = set()
    for tools in desktop_hints:
        all_selected.update(tools)
    assert "computer_use" in all_selected
    assert "open_application" in all_selected
