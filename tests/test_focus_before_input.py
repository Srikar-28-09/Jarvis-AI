"""Regression tests for focus-before-input mechanisms in Computer Use / Host Bridge.

Requirements covered:
1. open_application waits for / obtains HWND and establishes editor focus when available.
2. open_application restores / focuses existing application.
3. type_text focuses intended target and verifies keyboard focus before SendInput.
4. SendInput success without target focus or keyboard focus is NOT treated as application-visible success.
5. type_text returns structured evidence showing target HWND, foreground HWND, foreground PID,
   control info, and keyboard focus status.
"""

import pytest
import ctypes
from unittest.mock import patch, MagicMock

from host_bridge.actions import (
    execute_open_application,
    execute_type_text,
    restore_and_focus_window,
    find_application_window,
    ensure_editor_focus,
    get_gui_thread_info_for_window,
)
from src.computer_use_controller import is_deterministically_verifiable


def test_open_application_waits_for_and_obtains_hwnd():
    """1. open_application waits for and captures HWND and PID upon spawn."""
    mock_process = MagicMock()
    mock_process.pid = 17196

    # Before spawn (target_pid=None), app is not running; after spawn (target_pid=17196), window appears
    def mock_find(entry, target_pid=None):
        if target_pid == 17196:
            return (724290, 17196, "Untitled - Notepad")
        return (None, None, None)

    with patch("host_bridge.actions.is_application_running", return_value=(False, None)), \
         patch("subprocess.Popen", return_value=mock_process), \
         patch("host_bridge.actions.find_application_window", side_effect=mock_find), \
         patch("host_bridge.actions.restore_and_focus_window", return_value=True) as mock_restore, \
         patch("host_bridge.actions.ensure_editor_focus", return_value={"established": True, "control": "RichEditD2DPT", "control_hwnd": 8888}):

        res = execute_open_application({"application": "notepad"})

        assert res["success"] is True
        assert res["pid"] == 17196
        assert res["hwnd"] == 724290
        assert res["focused"] is True
        assert res["keyboard_focus_established"] is True
        assert res["control"] == "RichEditD2DPT"
        assert res["control_hwnd"] == 8888
        assert res["already_running"] is False
        assert "Notepad" in res["title"]
        mock_restore.assert_called_with(724290)


def test_open_application_restores_and_focuses_existing_instance():
    """2. open_application restores/focuses the existing application when already running."""
    with patch("host_bridge.actions.is_application_running", return_value=(True, "Untitled - Notepad")), \
         patch("host_bridge.actions.find_application_window", return_value=(724290, 17196, "Untitled - Notepad")), \
         patch("host_bridge.actions.restore_and_focus_window", return_value=True) as mock_restore, \
         patch("host_bridge.actions.ensure_editor_focus", return_value={"established": True, "control": "Edit", "control_hwnd": 7777}), \
         patch("subprocess.Popen") as mock_popen:

        res = execute_open_application({"application": "notepad"})

        assert res["success"] is True
        assert res["already_running"] is True
        assert res["hwnd"] == 724290
        assert res["pid"] == 17196
        assert res["focused"] is True
        assert res["keyboard_focus_established"] is True
        assert res["control"] == "Edit"
        assert res["control_hwnd"] == 7777
        mock_popen.assert_not_called()
        mock_restore.assert_called_with(724290)


def test_type_text_focuses_intended_target_before_sendinput():
    """3. type_text focuses the intended target window before dispatching SendInput."""
    # When target window is not currently foreground (current fg is Antigravity IDE)
    current_fg = {"hwnd": 1111, "title": "Antigravity IDE", "pid": 9999, "tid": 111}
    target_fg = {"hwnd": 724290, "title": "Untitled - Notepad", "pid": 17196, "tid": 222}

    # Simulate get_foreground_window_info returning IDE first, then Notepad after restore_and_focus
    fg_calls = [current_fg, target_fg]
    def mock_fg_info():
        if fg_calls:
            return fg_calls.pop(0)
        return target_fg

    mock_ed_focus = {
        "established": True,
        "control": "RichEditD2DPT",
        "control_hwnd": 99999,
        "gti": {"ok": True, "hwndFocus": 99999, "hwndCaret": 99999, "hwndMenuOwner": None},
    }

    with patch("host_bridge.actions.get_foreground_window_info", side_effect=mock_fg_info), \
         patch("host_bridge.actions.find_application_window", return_value=(724290, 17196, "Untitled - Notepad")), \
         patch("host_bridge.actions.restore_and_focus_window", return_value=True) as mock_restore, \
         patch("host_bridge.actions.ensure_editor_focus", return_value=mock_ed_focus) as mock_ed, \
         patch.object(ctypes.windll.user32, "SendInput", return_value=2) as mock_send:

        res = execute_type_text({
            "text": "Hello Jarvis",
            "application": "notepad",
            "hwnd": 724290,
        })

        assert res["success"] is True
        assert res["focused"] is True
        assert res["keyboard_focus_established"] is True
        assert res["hwnd"] == 724290
        assert res["target_hwnd"] == 724290
        assert res["control"] == "RichEditD2DPT"
        assert res["control_hwnd"] == 99999
        assert res["foreground_title"] == "Untitled - Notepad"
        assert res["length"] == 12
        mock_restore.assert_called_with(724290)
        mock_ed.assert_called_with(724290)
        assert mock_send.call_count == 12  # 12 characters typed


def test_type_text_refuses_when_target_cannot_be_focused():
    """4a. type_text refuses to send keystrokes into the wrong window if top-level focus fails."""
    current_fg = {"hwnd": 1111, "title": "Antigravity IDE", "pid": 9999, "tid": 111}

    with patch("host_bridge.actions.get_foreground_window_info", return_value=current_fg), \
         patch("host_bridge.actions.find_application_window", return_value=(724290, 17196, "Untitled - Notepad")), \
         patch("host_bridge.actions.restore_and_focus_window", return_value=False), \
         patch.object(ctypes.windll.user32, "SendInput") as mock_send:

        res = execute_type_text({
            "text": "Hello Jarvis",
            "application": "notepad",
            "hwnd": 724290,
        })

        assert res["success"] is False
        assert res["focused"] is False
        assert res["keyboard_focus_established"] is False
        assert "not in foreground" in res.get("error", "") or "could not be brought to foreground" in res.get("error", "")
        # Keystrokes must NOT be sent to the unintended window
        mock_send.assert_not_called()


def test_type_text_refuses_when_keyboard_control_focus_cannot_be_established():
    """4b. type_text refuses to dispatch SendInput if editor/keyboard focus cannot be established."""
    target_fg = {"hwnd": 724290, "title": "Untitled - Notepad", "pid": 17196, "tid": 222}

    mock_ed_focus_fail = {
        "established": False,
        "control": "RichEditD2DPT",
        "control_hwnd": 99999,
        "gti": {"ok": True, "hwndFocus": None, "hwndCaret": None, "hwndMenuOwner": None},
    }

    with patch("host_bridge.actions.get_foreground_window_info", return_value=target_fg), \
         patch("host_bridge.actions.ensure_editor_focus", return_value=mock_ed_focus_fail), \
         patch.object(ctypes.windll.user32, "SendInput") as mock_send:

        res = execute_type_text({
            "text": "Hello Jarvis",
            "application": "notepad",
            "hwnd": 724290,
        })

        assert res["success"] is False
        assert res["focused"] is False
        assert res["keyboard_focus_established"] is False
        assert "verified keyboard focus" in res.get("error", "")
        # SendInput must NOT be called when keyboard focus cannot be established
        mock_send.assert_not_called()


def test_type_text_returns_structured_evidence():
    """4c. type_text returns structured evidence showing target HWND, foreground HWND/PID, and control info."""
    target_fg = {"hwnd": 724290, "title": "Untitled - Notepad", "pid": 17196, "tid": 222}

    mock_ed_focus = {
        "established": True,
        "control": "RichEditD2DPT",
        "control_hwnd": 99999,
        "gti": {"ok": True, "hwndFocus": 99999, "hwndCaret": 99999, "hwndMenuOwner": None},
    }

    with patch("host_bridge.actions.get_foreground_window_info", return_value=target_fg), \
         patch("host_bridge.actions.ensure_editor_focus", return_value=mock_ed_focus), \
         patch.object(ctypes.windll.user32, "SendInput", return_value=2):

        res = execute_type_text({
            "text": "Hello Jarvis",
            "application": "notepad",
            "hwnd": 724290,
        })

        assert res["success"] is True
        assert res["target_hwnd"] == 724290
        assert res["foreground_hwnd"] == 724290
        assert res["foreground_pid"] == 17196
        assert res["focus_hwnd"] == 99999
        assert res["caret_hwnd"] == 99999
        assert res["control"] == "RichEditD2DPT"
        assert res["control_hwnd"] == 99999
        assert res["keyboard_focus_established"] is True


def test_deterministic_verifier_rejects_sendinput_without_focus():
    """4d. SendInput success without top-level focus is NOT treated as application-visible success."""
    tool_res = {
        "success": True,
        "length": 12,
        "focused": False,
        "keyboard_focus_established": False,
        "foreground_title": "Antigravity IDE",
    }
    is_verifiable, reason = is_deterministically_verifiable(
        action="type_text",
        params={"text": "Hello Jarvis", "application": "notepad"},
        tool_result=tool_res,
        goal="Open Notepad and type Hello Jarvis",
    )

    assert is_verifiable is False
    assert "not verified in foreground" in reason


def test_deterministic_verifier_rejects_sendinput_without_keyboard_focus():
    """4e. SendInput dispatched without verified keyboard/input focus is rejected by deterministic verifier."""
    tool_res = {
        "success": True,
        "length": 12,
        "focused": True,
        "keyboard_focus_established": False,
        "foreground_title": "Untitled - Notepad",
    }
    is_verifiable, reason = is_deterministically_verifiable(
        action="type_text",
        params={"text": "Hello Jarvis", "application": "notepad"},
        tool_result=tool_res,
        goal="Open Notepad and type Hello Jarvis",
    )

    assert is_verifiable is False
    assert "input focus" in reason


def test_deterministic_verifier_rejects_open_app_without_window_evidence():
    """4f. open_application without verified HWND or focus is NOT treated as deterministically verified."""
    tool_res = {
        "success": True,
        "exit_code": 0,
        "pid": 17196,
        "hwnd": None,
        "focused": False,
    }
    is_verifiable, reason = is_deterministically_verifiable(
        action="open_application",
        params={"application": "notepad"},
        tool_result=tool_res,
        goal="Open Notepad",
    )

    assert is_verifiable is False
    assert "window HWND/focus was not established" in reason


def test_deterministic_verifier_accepts_when_focused_and_hwnd_present():
    """4g. open_application and type_text ARE deterministically verified when focus is confirmed."""
    app_res = {
        "success": True,
        "pid": 17196,
        "hwnd": 724290,
        "focused": True,
        "keyboard_focus_established": True,
        "title": "Untitled - Notepad",
    }
    app_ok, app_reason = is_deterministically_verifiable(
        action="open_application",
        params={"application": "notepad"},
        tool_result=app_res,
        goal="Open Notepad",
    )
    assert app_ok is True
    assert "hwnd=724290" in app_reason
    assert "pid=17196" in app_reason

    type_res = {
        "success": True,
        "focused": True,
        "keyboard_focus_established": True,
        "hwnd": 724290,
        "control": "RichEditD2DPT",
        "foreground_title": "Untitled - Notepad",
        "length": 12,
    }
    type_ok, type_reason = is_deterministically_verifiable(
        action="type_text",
        params={"text": "Hello Jarvis", "application": "notepad"},
        tool_result=type_res,
        goal="Open Notepad and type Hello Jarvis",
    )
    assert type_ok is True
    assert "Untitled - Notepad" in type_reason
    assert "RichEditD2DPT" in type_reason


def test_notepad_focus_and_type_text_requires_deterministic_delivery():
    """Phase 7 Targeted Regression Test:
    Notepad -> focus -> type_text('Hello Jarvis')
    does not report deterministic success unless delivery conditions are satisfied:
    1. Target window is focused.
    2. Keyboard focus is established on the control.
    3. SendInput actually dispatches characters (dispatched > 0).
    4. Text delivery is verified (delivered is not False).
    """
    # 1. Reject when SendInput fails to dispatch characters (dispatched = 0)
    failed_dispatch_res = {
        "success": False,
        "focused": True,
        "keyboard_focus_established": True,
        "hwnd": 724290,
        "control": "RichEditD2DPT",
        "foreground_title": "Untitled - Notepad",
        "length": 12,
        "dispatched": 0,
        "delivered": False,
    }
    ok_disp, reason_disp = is_deterministically_verifiable(
        action="type_text",
        params={"text": "Hello Jarvis", "application": "notepad"},
        tool_result=failed_dispatch_res,
        goal="Open Notepad and type Hello Jarvis",
    )
    assert ok_disp is False
    assert "no characters were dispatched" in reason_disp

    # 2. Reject when SendInput claims success but delivery fails (delivered = False)
    failed_delivery_res = {
        "success": True,
        "focused": True,
        "keyboard_focus_established": True,
        "hwnd": 724290,
        "control": "RichEditD2DPT",
        "foreground_title": "Untitled - Notepad",
        "length": 12,
        "dispatched": 12,
        "delivered": False,
    }
    ok_deliv, reason_deliv = is_deterministically_verifiable(
        action="type_text",
        params={"text": "Hello Jarvis", "application": "notepad"},
        tool_result=failed_delivery_res,
        goal="Open Notepad and type Hello Jarvis",
    )
    assert ok_deliv is False
    assert "failed delivery verification" in reason_deliv

    # 3. Reject when target window is not foreground
    unfocused_res = {
        "success": True,
        "focused": False,
        "hwnd": 724290,
        "foreground_title": "Antigravity IDE",
        "length": 12,
        "dispatched": 12,
        "delivered": True,
    }
    ok_unfoc, reason_unfoc = is_deterministically_verifiable(
        action="type_text",
        params={"text": "Hello Jarvis", "application": "notepad"},
        tool_result=unfocused_res,
        goal="Open Notepad and type Hello Jarvis",
    )
    assert ok_unfoc is False
    assert "was not verified in foreground" in reason_unfoc

    # 4. Accept ONLY when all deterministic delivery conditions are satisfied
    successful_res = {
        "success": True,
        "focused": True,
        "keyboard_focus_established": True,
        "hwnd": 724290,
        "control": "RichEditD2DPT",
        "foreground_title": "Untitled - Notepad",
        "length": 12,
        "dispatched": 12,
        "delivered": True,
        "delivery_verified": True,
        "delivery_method": "wm_gettext",
    }
    ok_succ, reason_succ = is_deterministically_verifiable(
        action="type_text",
        params={"text": "Hello Jarvis", "application": "notepad"},
        tool_result=successful_res,
        goal="Open Notepad and type Hello Jarvis",
    )
    assert ok_succ is True
    assert "Untitled - Notepad" in reason_succ
    assert "RichEditD2DPT" in reason_succ

