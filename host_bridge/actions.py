"""
host_bridge/actions.py

Concrete, safe handlers for allowed Windows host operations:
- Application launching (from fixed allowlist)
- URL opening (http/https only)
- Desktop screenshot capture
- Safe GUI input: click, double_click, type_text, press_key, scroll

All actions validate parameters strictly, enforce screen boundaries and allowlists,
and avoid arbitrary command/binary execution.
"""

from __future__ import annotations

import base64
import ctypes
from ctypes import wintypes
from datetime import datetime
import logging
import math
import os
from pathlib import Path
import subprocess
import threading
import time
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse
import webbrowser

from .config import (
    APPLICATION_ALLOWLIST,
    DANGEROUS_URL_SCHEMES,
    HOST_WORKSPACE_DIR,
    IS_LINUX,
    IS_WINDOWS,
    SAFE_URL_SCHEMES,
)


logger = logging.getLogger("host_bridge.actions")

# ---------------------------------------------------------------------------
# Rate Limiting & Safety Constants
# ---------------------------------------------------------------------------
MAX_TYPE_TEXT_LENGTH = 500
MAX_SCROLL_AMOUNT = 50
MIN_SCROLL_AMOUNT = 1
DEFAULT_SCROLL_AMOUNT = 5
MAX_GUI_ACTIONS_PER_SEC = 30
_LAST_GUI_ACTION_TIME = 0.0
_MUTTER_SCREENCAST_LOCK = threading.Lock()
_SCREENSHOT_TIMING = threading.local()

# Win32 Mouse & Keyboard Constants
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_WHEEL = 0x0800
WHEEL_DELTA = 120

INPUT_KEYBOARD = 1
KEYEVENTF_UNICODE = 0x0004
KEYEVENTF_KEYUP = 0x0002
ULONG_PTR = ctypes.c_size_t

# Strict Keyboard Key Allowlist -> Windows Virtual Key (VK) Codes
KEY_ALLOWLIST: Dict[str, int] = {
    "ENTER": 0x0D,
    "RETURN": 0x0D,
    "TAB": 0x09,
    "ESCAPE": 0x1B,
    "ESC": 0x1B,
    "BACKSPACE": 0x08,
    "BACK": 0x08,
    "DELETE": 0x2E,
    "DEL": 0x2E,
    "SPACE": 0x20,
    "UP": 0x26,
    "DOWN": 0x28,
    "LEFT": 0x25,
    "RIGHT": 0x27,
    "HOME": 0x24,
    "END": 0x23,
    "PAGEUP": 0x21,
    "PAGE_UP": 0x21,
    "PGUP": 0x21,
    "PAGEDOWN": 0x22,
    "PAGE_DOWN": 0x22,
    "PGDN": 0x22,
    "INSERT": 0x2D,
    "INS": 0x2D,
    "CAPSLOCK": 0x14,
    "CAPS_LOCK": 0x14,
    "F1": 0x70,
    "F2": 0x71,
    "F3": 0x72,
    "F4": 0x73,
    "F5": 0x74,
    "F6": 0x75,
    "F7": 0x76,
    "F8": 0x77,
    "F9": 0x78,
    "F10": 0x79,
    "F11": 0x7A,
    "F12": 0x7B,
}

LINUX_KEY_NAME_MAP: Dict[str, str] = {
    "ENTER": "Return",
    "RETURN": "Return",
    "TAB": "Tab",
    "ESCAPE": "Escape",
    "ESC": "Escape",
    "BACKSPACE": "BackSpace",
    "BACK": "BackSpace",
    "DELETE": "Delete",
    "DEL": "Delete",
    "SPACE": "space",
    "UP": "Up",
    "DOWN": "Down",
    "LEFT": "Left",
    "RIGHT": "Right",
    "HOME": "Home",
    "END": "End",
    "PAGEUP": "Page_Up",
    "PAGE_UP": "Page_Up",
    "PGUP": "Page_Up",
    "PAGEDOWN": "Page_Down",
    "PAGE_DOWN": "Page_Down",
    "PGDN": "Page_Down",
    "INSERT": "Insert",
    "INS": "Insert",
    "CAPSLOCK": "Caps_Lock",
    "CAPS_LOCK": "Caps_Lock",
    "F1": "F1",
    "F2": "F2",
    "F3": "F3",
    "F4": "F4",
    "F5": "F5",
    "F6": "F6",
    "F7": "F7",
    "F8": "F8",
    "F9": "F9",
    "F10": "F10",
    "F11": "F11",
    "F12": "F12",
}

PYAUTOGUI_KEY_MAP: Dict[str, str] = {
    "ENTER": "enter",
    "RETURN": "return",
    "TAB": "tab",
    "ESCAPE": "esc",
    "ESC": "esc",
    "BACKSPACE": "backspace",
    "BACK": "backspace",
    "DELETE": "delete",
    "DEL": "delete",
    "SPACE": "space",
    "UP": "up",
    "DOWN": "down",
    "LEFT": "left",
    "RIGHT": "right",
    "HOME": "home",
    "END": "end",
    "PAGEUP": "pageup",
    "PAGE_UP": "pageup",
    "PGUP": "pageup",
    "PAGEDOWN": "pagedown",
    "PAGE_DOWN": "pagedown",
    "PGDN": "pagedown",
    "INSERT": "insert",
    "INS": "insert",
    "CAPSLOCK": "capslock",
    "CAPS_LOCK": "capslock",
    "F1": "f1",
    "F2": "f2",
    "F3": "f3",
    "F4": "f4",
    "F5": "f5",
    "F6": "f6",
    "F7": "f7",
    "F8": "f8",
    "F9": "f9",
    "F10": "f10",
    "F11": "f11",
    "F12": "f12",
}

WINFUNCTYPE = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)

# Ctypes structures for SendInput (complete 40-byte union on 64-bit Windows)
class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]

class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ULONG_PTR),
    ]

class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]

class _INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("mi", MOUSEINPUT),
        ("ki", KEYBDINPUT),
        ("hi", HARDWAREINPUT),
    ]

class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [
        ("type", wintypes.DWORD),
        ("u", _INPUT_UNION),
    ]


def _enforce_gui_rate_limit() -> None:
    """Enforce a minimum spacing between GUI actions to prevent flood attacks."""
    global _LAST_GUI_ACTION_TIME
    now = time.monotonic()
    elapsed = now - _LAST_GUI_ACTION_TIME
    min_interval = 1.0 / MAX_GUI_ACTIONS_PER_SEC
    if elapsed < min_interval:
        time.sleep(min_interval - elapsed)
    _LAST_GUI_ACTION_TIME = time.monotonic()


def get_screen_bounds() -> Tuple[int, int]:
    """Get current primary screen dimensions (width, height) in pixels."""
    if IS_WINDOWS and hasattr(ctypes, "windll") and hasattr(ctypes.windll, "user32"):
        try:
            user32 = ctypes.windll.user32
            w = user32.GetSystemMetrics(0)  # SM_CXSCREEN
            h = user32.GetSystemMetrics(1)  # SM_CYSCREEN
            if w > 0 and h > 0:
                return int(w), int(h)
        except Exception:
            pass

    # Linux / Cross-platform primary display query
    try:
        import pyautogui
        sz = pyautogui.size()
        if sz[0] > 0 and sz[1] > 0:
            return int(sz[0]), int(sz[1])
    except Exception:
        pass

    try:
        import mss
        with mss.mss() as sct:
            mon = sct.monitors[0]
            if mon["width"] > 0 and mon["height"] > 0:
                return int(mon["width"]), int(mon["height"])
    except Exception:
        pass

    return 1920, 1080  # Safe default fallback bounds



def _validate_coordinate(val: Any, name: str, max_val: int) -> Tuple[Optional[int], Optional[str]]:
    """Validate a single coordinate value against integer bounds [0, max_val)."""
    if val is None or isinstance(val, bool):
        return None, f"Coordinate '{name}' is required and must be an integer"

    if isinstance(val, float):
        if not math.isfinite(val) or not val.is_integer():
            return None, f"Coordinate '{name}' must be a finite integer, got {val}"
        val = int(val)
    elif isinstance(val, str):
        val_str = val.strip()
        try:
            val = int(val_str)
        except ValueError:
            return None, f"Coordinate '{name}' must be an integer, got '{val}'"
    elif not isinstance(val, int):
        return None, f"Coordinate '{name}' must be an integer"

    if val < 0:
        return None, f"Coordinate '{name}' ({val}) cannot be negative"

    if val >= max_val:
        return None, f"Coordinate '{name}' ({val}) is outside screen bounds (0 to {max_val - 1})"

    return val, None


PS_SCREENSHOT_SCRIPT = """
Add-Type -AssemblyName System.Windows.Forms,System.Drawing
$bounds = [System.Windows.Forms.Screen]::PrimaryScreen.Bounds
$bmp = New-Object System.Drawing.Bitmap $bounds.Width, $bounds.Height
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.CopyFromScreen($bounds.Location, [System.Drawing.Point]::Empty, $bounds.Size)
$ms = New-Object System.IO.MemoryStream
$bmp.Save($ms, [System.Drawing.Imaging.ImageFormat]::Jpeg)
$b64 = [System.Convert]::ToBase64String($ms.ToArray())
$g.Dispose()
$bmp.Dispose()
$ms.Dispose()
Write-Output "$($bounds.Width)x$($bounds.Height)"
Write-Output $b64
"""


# ---------------------------------------------------------------------------
# Host Actions
# ---------------------------------------------------------------------------

def normalize_app_name(raw_name: str) -> str:
    """Normalize raw application string for allowlist lookup."""
    clean = str(raw_name or "").strip().lower()
    if clean.endswith(".exe"):
        clean = clean[:-4].strip()
    return clean


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hwndActive", wintypes.HWND),
        ("hwndFocus", wintypes.HWND),
        ("hwndCapture", wintypes.HWND),
        ("hwndMenuOwner", wintypes.HWND),
        ("hwndMoveSize", wintypes.HWND),
        ("hwndCaret", wintypes.HWND),
        ("rcCaret", wintypes.RECT),
    ]


class POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


def get_foreground_window_info() -> Dict[str, Any]:
    """Retrieve information about the currently active foreground window."""
    if not hasattr(ctypes, "windll") or not hasattr(ctypes.windll, "user32"):
        return {"hwnd": None, "title": "", "pid": None, "tid": None}

    user32 = ctypes.windll.user32
    try:
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return {"hwnd": None, "title": "", "pid": None, "tid": None}

        length = user32.GetWindowTextLengthW(hwnd)
        title = ""
        if length > 0:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value.strip()

        lpdw_pid = wintypes.DWORD()
        tid = user32.GetWindowThreadProcessId(hwnd, ctypes.byref(lpdw_pid))
        return {"hwnd": hwnd, "title": title, "pid": lpdw_pid.value, "tid": tid}
    except Exception as e:
        logger.debug("Failed to query foreground window info: %s", e)
        return {"hwnd": None, "title": "", "pid": None, "tid": None}


def get_gui_thread_info_for_window(hwnd: int) -> Dict[str, Any]:
    """Retrieve GUI thread info (active window, keyboard focus, caret, menu) for an HWND."""
    if not hwnd or not hasattr(ctypes, "windll") or not hasattr(ctypes.windll, "user32"):
        return {
            "ok": False,
            "tid": None,
            "pid": None,
            "hwndActive": None,
            "hwndFocus": None,
            "hwndCaret": None,
            "hwndMenuOwner": None,
            "flags": 0,
        }

    user32 = ctypes.windll.user32
    try:
        lpdw_pid = wintypes.DWORD()
        tid = user32.GetWindowThreadProcessId(hwnd, ctypes.byref(lpdw_pid))
        if not tid:
            return {
                "ok": False,
                "tid": None,
                "pid": None,
                "hwndActive": None,
                "hwndFocus": None,
                "hwndCaret": None,
                "hwndMenuOwner": None,
                "flags": 0,
            }

        gti = GUITHREADINFO()
        gti.cbSize = ctypes.sizeof(GUITHREADINFO)
        ok = user32.GetGUIThreadInfo(tid, ctypes.byref(gti))
        return {
            "ok": bool(ok),
            "tid": tid,
            "pid": lpdw_pid.value,
            "hwndActive": gti.hwndActive,
            "hwndFocus": gti.hwndFocus,
            "hwndCaret": gti.hwndCaret,
            "hwndMenuOwner": gti.hwndMenuOwner,
            "flags": gti.flags,
        }
    except Exception as e:
        logger.debug("GetGUIThreadInfo failed for HWND %s: %s", hwnd, e)
        return {
            "ok": False,
            "tid": None,
            "pid": None,
            "hwndActive": None,
            "hwndFocus": None,
            "hwndCaret": None,
            "hwndMenuOwner": None,
            "flags": 0,
        }


def find_application_editor_control(top_hwnd: int) -> Optional[Tuple[int, str, Tuple[int, int, int, int]]]:
    """Find the primary text editor or input control within a top-level window.
    
    Returns (control_hwnd, class_name, (left, top, right, bottom)) or None.
    Prioritizes known editor classes (RichEditD2DPT, Edit, Scintilla, RichEdit).
    """
    if not top_hwnd or not hasattr(ctypes, "windll") or not hasattr(ctypes.windll, "user32"):
        return None

    user32 = ctypes.windll.user32
    priority_classes = [
        "richeditd2dpt",     # Modern Windows 11 Notepad editor
        "edit",              # Classic Notepad, standard Win32 dialogs/inputs
        "scintilla",         # Notepad++, code editors
        "richedit60w", "richedit50w", "richedit20w", "richedit", # Rich edit controls
        "notepadtextbox",    # Modern Notepad text container
    ]

    candidates = []
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _enum_cb(ch, lp):
        if hasattr(user32, "IsWindowVisible") and not user32.IsWindowVisible(ch):
            return True
        cls_buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(ch, cls_buf, 256)
        cname = cls_buf.value.strip()
        cname_lower = cname.lower()

        rect = wintypes.RECT()
        if hasattr(user32, "GetWindowRect"):
            user32.GetWindowRect(ch, ctypes.byref(rect))
        w = max(0, rect.right - rect.left)
        h = max(0, rect.bottom - rect.top)
        area = w * h

        for rank, pclass in enumerate(priority_classes):
            if pclass in cname_lower:
                candidates.append((rank, area, ch, cname, (rect.left, rect.top, rect.right, rect.bottom)))
                break
        return True

    try:
        user32.EnumChildWindows(top_hwnd, WNDENUMPROC(_enum_cb), 0)
    except Exception as e:
        logger.debug("EnumChildWindows failed on HWND %s: %s", top_hwnd, e)
        return None

    if not candidates:
        return None

    candidates.sort(key=lambda x: (x[0], -x[1]))
    best = candidates[0]
    return (best[2], best[3], best[4])


def ensure_editor_focus(target_hwnd: int) -> Dict[str, Any]:
    """Ensure that the editor/input control of target_hwnd has keyboard focus."""
    if not target_hwnd or not hasattr(ctypes, "windll") or not hasattr(ctypes.windll, "user32"):
        return {"established": False, "control": None, "control_hwnd": None, "gti": {}}

    user32 = ctypes.windll.user32
    kernel32 = getattr(ctypes.windll, "kernel32", None)

    gti = get_gui_thread_info_for_window(target_hwnd)
    ed = find_application_editor_control(target_hwnd)

    if not ed:
        is_focused = bool(gti.get("hwndFocus") and not gti.get("hwndMenuOwner"))
        return {"established": is_focused, "control": None, "control_hwnd": None, "gti": gti}

    ed_hwnd, ed_class, (l, t, r, b) = ed

    # Dismiss menu if open
    if gti.get("hwndMenuOwner") and hasattr(user32, "SendMessageW"):
        user32.SendMessageW(target_hwnd, 0x0100, 0x1B, 0)  # WM_KEYDOWN VK_ESCAPE
        user32.SendMessageW(target_hwnd, 0x0101, 0x1B, 0)  # WM_KEYUP VK_ESCAPE
        time.sleep(0.02)
        gti = get_gui_thread_info_for_window(target_hwnd)

    # Check if already focused
    if gti.get("hwndFocus") == ed_hwnd and not gti.get("hwndMenuOwner"):
        return {"established": True, "control": ed_class, "control_hwnd": ed_hwnd, "gti": gti}

    # Attach thread input and set focus
    target_tid = gti.get("tid")
    our_tid = kernel32.GetCurrentThreadId() if kernel32 and hasattr(kernel32, "GetCurrentThreadId") else 0
    attached = False
    if our_tid and target_tid and our_tid != target_tid and hasattr(user32, "AttachThreadInput"):
        attached = bool(user32.AttachThreadInput(our_tid, target_tid, True))

    try:
        if hasattr(user32, "SetFocus"):
            user32.SetFocus(ed_hwnd)
    finally:
        if attached and hasattr(user32, "AttachThreadInput"):
            user32.AttachThreadInput(our_tid, target_tid, False)

    time.sleep(0.03)
    gti2 = get_gui_thread_info_for_window(target_hwnd)
    is_focused = bool(
        (gti2.get("hwndFocus") == ed_hwnd) or
        (gti2.get("hwndCaret") == ed_hwnd) or
        (gti2.get("hwndFocus") and not gti2.get("hwndMenuOwner"))
    )
    return {
        "established": is_focused,
        "control": ed_class,
        "control_hwnd": ed_hwnd,
        "gti": gti2,
    }


def restore_and_focus_window(hwnd: int) -> bool:
    """Restore window if minimized and bring it to the foreground with clean input focus."""
    if not hwnd or not hasattr(ctypes, "windll") or not hasattr(ctypes.windll, "user32"):
        return False

    user32 = ctypes.windll.user32
    kernel32 = getattr(ctypes.windll, "kernel32", None)
    try:
        # Check if minimized (IsIconic) -> restore
        if hasattr(user32, "IsIconic") and user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            time.sleep(0.05)
        elif hasattr(user32, "ShowWindow"):
            user32.ShowWindow(hwnd, 5)  # SW_SHOW
            time.sleep(0.02)

        if hasattr(user32, "GetForegroundWindow") and user32.GetForegroundWindow() == hwnd:
            return True

        # AttachThreadInput to foreground thread to permit SetForegroundWindow cleanly without Alt key
        our_tid = kernel32.GetCurrentThreadId() if kernel32 and hasattr(kernel32, "GetCurrentThreadId") else 0
        fg_hwnd = user32.GetForegroundWindow() if hasattr(user32, "GetForegroundWindow") else 0
        fg_tid = user32.GetWindowThreadProcessId(fg_hwnd, None) if fg_hwnd else 0
        target_tid = user32.GetWindowThreadProcessId(hwnd, None) if hasattr(user32, "GetWindowThreadProcessId") else 0

        attached_fg = False
        attached_target = False
        if hasattr(user32, "AttachThreadInput"):
            if our_tid and fg_tid and our_tid != fg_tid:
                attached_fg = bool(user32.AttachThreadInput(our_tid, fg_tid, True))
            if our_tid and target_tid and our_tid != target_tid:
                attached_target = bool(user32.AttachThreadInput(our_tid, target_tid, True))

        set_fg_res = True
        try:
            if hasattr(user32, "AllowSetForegroundWindow"):
                user32.AllowSetForegroundWindow(0xFFFFFFFF)  # ASFW_ANY
            if hasattr(user32, "BringWindowToTop"):
                user32.BringWindowToTop(hwnd)
            if hasattr(user32, "SetForegroundWindow"):
                set_fg_res = bool(user32.SetForegroundWindow(hwnd))
        finally:
            if attached_target and hasattr(user32, "AttachThreadInput"):
                user32.AttachThreadInput(our_tid, target_tid, False)
            if attached_fg and hasattr(user32, "AttachThreadInput"):
                user32.AttachThreadInput(our_tid, fg_tid, False)

        # Brief synchronization: poll up to 150ms for foreground activation
        deadline = time.monotonic() + 0.15
        while time.monotonic() < deadline:
            if hasattr(user32, "GetForegroundWindow") and user32.GetForegroundWindow() == hwnd:
                return True
            time.sleep(0.02)

        fg_hwnd = user32.GetForegroundWindow() if hasattr(user32, "GetForegroundWindow") else None
        return (fg_hwnd == hwnd) or set_fg_res
    except Exception as e:
        logger.debug("restore_and_focus_window failed for HWND %s: %s", hwnd, e)
        return False


def find_application_window(
    entry: Dict[str, Any],
    target_pid: Optional[int] = None,
) -> Tuple[Optional[int], Optional[int], Optional[str]]:
    """Find a top-level visible window matching an application entry or target PID.
    
    Returns (hwnd, pid, window_title).
    """
    if not hasattr(ctypes, "windll") or not hasattr(ctypes.windll, "user32"):
        return None, None, None

    user32 = ctypes.windll.user32
    app_name = entry.get("name", "")
    proc_name = entry.get("process_name") or entry.get("command", "")
    window_titles = [wt.lower() for wt in (entry.get("window_titles") or [app_name.lower(), proc_name.lower()])]

    found_hwnd = None
    found_pid = None
    found_title = None

    WNDENUMPROC = WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _enum_cb(hwnd, lparam):
        nonlocal found_hwnd, found_pid, found_title
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True

        lpdw_pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(lpdw_pid))
        win_pid = lpdw_pid.value

        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value.strip()
        title_lower = title.lower()

        # Check target PID match first
        if target_pid is not None and win_pid == target_pid:
            found_hwnd = hwnd
            found_pid = win_pid
            found_title = title
            return False

        # Match against configured window titles
        for wt in window_titles:
            if wt in title_lower:
                found_hwnd = hwnd
                found_pid = win_pid
                found_title = title
                return False

        return True

    try:
        user32.EnumWindows(WNDENUMPROC(_enum_cb), 0)
    except Exception as e:
        logger.debug("Win32 EnumWindows search failed: %s", e)

    return found_hwnd, found_pid, found_title


def is_application_running(entry: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
    """Check if an allowed application is already running on the host.
    
    Returns (is_running, window_or_process_identifier).
    """
    hwnd, pid, title = find_application_window(entry)
    if title:
        return True, title
    if hwnd:
        return True, str(hwnd)

    app_name = entry.get("name", "")
    proc_name = entry.get("process_name") or entry.get("command", "")

    if IS_WINDOWS:
        proc_name_exe = proc_name if proc_name.lower().endswith(".exe") else f"{proc_name}.exe"
        try:
            res = subprocess.run(
                ["tasklist", "/fi", f"imagename eq {proc_name_exe}", "/fo", "csv", "/nh"],
                capture_output=True,
                text=True,
                timeout=3,
                shell=False,
            )
            if res.returncode == 0 and proc_name_exe.lower() in res.stdout.lower():
                return True, proc_name_exe
        except Exception as e:
            logger.debug("tasklist process check failed for %s: %s", proc_name_exe, e)
    elif IS_LINUX:
        try:
            res = subprocess.run(
                ["pgrep", "-f", proc_name],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if res.returncode == 0 and res.stdout.strip():
                pids = res.stdout.strip().split()
                return True, f"{proc_name} (PID {pids[0]})"
        except Exception as e:
            logger.debug("Linux pgrep check failed for %s: %s", proc_name, e)

    return False, None



def focus_application_window(entry: Dict[str, Any]) -> bool:
    """Attempt to bring an already running application window to the foreground."""
    hwnd, pid, title = find_application_window(entry)
    if hwnd:
        return restore_and_focus_window(hwnd)

    app_name = entry.get("name", "")
    proc_name = entry.get("command", "")
    window_titles = entry.get("window_titles") or [app_name.lower(), proc_name.lower()]

    if not hasattr(ctypes, "windll") or not hasattr(ctypes.windll, "user32"):
        return False

    user32 = ctypes.windll.user32
    target_hwnd = None

    WNDENUMPROC = WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _enum_cb(hwnd, lparam):
        nonlocal target_hwnd
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length > 0:
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value.strip()
            title_lower = title.lower()
            for wt in window_titles:
                if wt.lower() in title_lower:
                    target_hwnd = hwnd
                    return False
        return True

    try:
        user32.EnumWindows(WNDENUMPROC(_enum_cb), 0)
    except Exception as e:
        logger.debug("Win32 EnumWindows focus search failed: %s", e)
        return False

    if target_hwnd:
        return restore_and_focus_window(target_hwnd)

    return False


_LAST_JARVIS_LAUNCH_TIME = 0.0


def focus_or_launch_jarvis_window(url: str = "http://localhost:7000") -> Dict[str, Any]:
    """Find and focus an existing Jarvis/Odysseus browser window, or launch in App Mode.
    
    Prevents duplicate window instances when hotkey is pressed repeatedly.
    """
    global _LAST_JARVIS_LAUNCH_TIME

    window_keywords = ["odysseus", "jarvis", "localhost:7000", "127.0.0.1:7000"]
    target_hwnd = None
    target_title = None

    if hasattr(ctypes, "windll") and hasattr(ctypes.windll, "user32"):
        user32 = ctypes.windll.user32
        WNDENUMPROC = WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def _enum_cb(hwnd, lparam):
            nonlocal target_hwnd, target_title
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                title = buf.value.strip()
                title_lower = title.lower()
                for kw in window_keywords:
                    if kw in title_lower:
                        target_hwnd = hwnd
                        target_title = title
                        return False
            return True

        try:
            user32.EnumWindows(WNDENUMPROC(_enum_cb), 0)
        except Exception as e:
            logger.debug("Win32 EnumWindows for Jarvis window search failed: %s", e)

    if target_hwnd:
        try:
            user32 = ctypes.windll.user32
            # SW_RESTORE = 9
            user32.ShowWindow(target_hwnd, 9)
            user32.BringWindowToTop(target_hwnd)
            user32.SetForegroundWindow(target_hwnd)
            logger.info("Focused existing Jarvis window (HWND %s): '%s'", target_hwnd, target_title)
            return {
                "success": True,
                "action": "focused",
                "hwnd": target_hwnd,
                "title": target_title,
                "message": f"Focused existing Jarvis window: {target_title}",
            }
        except Exception as e:
            logger.warning("Failed to bring existing Jarvis window to foreground: %s", e)

    # Debounce launch if a launch occurred in the last 2 seconds
    now = time.monotonic()
    if (now - _LAST_JARVIS_LAUNCH_TIME) < 2.0:
        logger.info("Jarvis window launch debounced (already launched within last 2 seconds).")
        return {
            "success": True,
            "action": "debounced",
            "message": "Jarvis launch already initiated",
        }
    _LAST_JARVIS_LAUNCH_TIME = now

    # Try launching in App Mode, then default browser
    app_flag = f"--app={url}"
    launched = False
    launch_method = None

    browser_candidates = ("msedge", "chrome") if IS_WINDOWS else (
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
        "msedge",
        "chrome",
    )

    for browser_cmd in browser_candidates:
        try:
            subprocess.Popen([browser_cmd, app_flag], shell=False)
            launched = True
            launch_method = f"{browser_cmd} App Mode"
            logger.info("Launched Jarvis in %s (%s)", launch_method, url)
            break
        except FileNotFoundError:
            continue
        except Exception as e:
            logger.debug("Failed launching with %s: %s", browser_cmd, e)

    if not launched and IS_LINUX:
        try:
            subprocess.Popen(["firefox", "--new-window", url], shell=False)
            launched = True
            launch_method = "firefox"
            logger.info("Launched Jarvis in Firefox (%s)", url)
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.debug("Failed launching with firefox: %s", e)

    if not launched:
        try:
            webbrowser.open(url)
            launched = True
            launch_method = "default browser"
            logger.info("Launched Jarvis in default browser (%s)", url)
        except Exception as e:
            logger.error("Failed to open Jarvis UI URL '%s': %s", url, e)
            return {
                "success": False,
                "error": f"Failed to launch browser: {e}",
            }

    return {
        "success": True,
        "action": "launched",
        "method": launch_method,
        "url": url,
        "message": f"Launched Jarvis UI using {launch_method}",
    }



def execute_open_application(params: Dict[str, Any]) -> Dict[str, Any]:
    """Open an application from the fixed server allowlist.
    
    If single-instance reuse is enabled for the application (e.g. Notepad, Calculator)
    and it is already running, brings the existing window to the foreground without
    spawning a duplicate process.
    """
    raw_app = str(params.get("application") or params.get("app_name") or params.get("name") or params.get("app") or "").strip()
    if not raw_app:
        return {"success": False, "error": "Application name is required"}

    app_key = normalize_app_name(raw_app)
    entry = APPLICATION_ALLOWLIST.get(app_key)
    if not entry:
        # Also try direct lowercase match
        entry = APPLICATION_ALLOWLIST.get(raw_app.strip().lower())

    if not entry:
        allowed = sorted(set(v["name"] for v in APPLICATION_ALLOWLIST.values()))
        return {
            "success": False,
            "error": f"Application '{raw_app}' is not allowed. Allowed applications: {', '.join(allowed)}",
        }

    # Check if this application should be reused when already running
    single_instance = entry.get("single_instance", False)
    if single_instance:
        hwnd, pid, title = find_application_window(entry)
        if hwnd:
            focused = restore_and_focus_window(hwnd)
            ed_focus = ensure_editor_focus(hwnd)
            focus_msg = "focused" if focused else "found running"
            logger.info("Application '%s' is already running (HWND %s, PID %s). Reusing existing instance (no Popen).", entry["name"], hwnd, pid)
            return {
                "success": True,
                "action": "open_application",
                "application": entry["name"],
                "already_running": True,
                "focused": focused,
                "keyboard_focus_established": ed_focus.get("established", False),
                "control": ed_focus.get("control"),
                "control_hwnd": ed_focus.get("control_hwnd"),
                "hwnd": hwnd,
                "pid": pid,
                "title": title,
                "message": f"{entry['name']} is already running ({focus_msg}); reused existing instance",
            }

        running, identifier = is_application_running(entry)
        if running:
            focused = focus_application_window(entry)
            if focused:
                hwnd, pid, title = find_application_window(entry)
                ed_focus = ensure_editor_focus(hwnd) if hwnd else {"established": False}
                logger.info("Application '%s' is already running (%s, HWND %s). Focused existing instance.", entry["name"], identifier, hwnd)
                return {
                    "success": True,
                    "action": "open_application",
                    "application": entry["name"],
                    "already_running": True,
                    "focused": True,
                    "keyboard_focus_established": ed_focus.get("established", False),
                    "control": ed_focus.get("control"),
                    "control_hwnd": ed_focus.get("control_hwnd"),
                    "hwnd": hwnd,
                    "pid": pid,
                    "title": title or (identifier if isinstance(identifier, str) and not identifier.endswith(".exe") else None),
                    "message": f"{entry['name']} is already running (focused); reused existing instance",
                }
            # If no window could be focused despite process existing (e.g. background/zombie process),
            # do not spawn another copy: the process may already have opened its window,
            # and launching again can create another browser window or tab.
            logger.warning("Process %s is running but could not be focused; not launching a duplicate.", identifier)
            return {
                "success": True,
                "action": "open_application",
                "application": entry["name"],
                "already_running": True,
                "focused": False,
                "hwnd": None,
                "pid": None,
                "title": None,
                "message": f"{entry['name']} is already running, but its window could not be focused. No duplicate was launched.",
            }

    # Not running (or multi-instance allowed) -> spawn new process
    cmd = entry["command"]
    args = entry.get("args", [])
    try:
        proc = subprocess.Popen([cmd, *args], shell=False)
        spawned_pid = getattr(proc, "pid", None)
    except FileNotFoundError:
        try:
            os.startfile(cmd)
            spawned_pid = None
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to launch {entry['name']}: {e}",
            }
    except Exception as e:
        return {
            "success": False,
            "error": f"Failed to launch {entry['name']}: {e}",
        }

    # Poll briefly for top-level window to exist and establish focus
    hwnd = None
    pid = spawned_pid
    title = None
    if isinstance(spawned_pid, int):
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            hwnd, pid, title = find_application_window(entry, target_pid=spawned_pid)
            if hwnd:
                break
            time.sleep(0.05)
    else:
        hwnd, pid, title = find_application_window(entry)

    focused = False
    ed_focus = {}
    if hwnd:
        focused = restore_and_focus_window(hwnd)
        ed_focus = ensure_editor_focus(hwnd)

    return {
        "success": True,
        "action": "open_application",
        "application": entry["name"],
        "already_running": False,
        "focused": focused,
        "keyboard_focus_established": ed_focus.get("established", False),
        "control": ed_focus.get("control"),
        "control_hwnd": ed_focus.get("control_hwnd"),
        "hwnd": hwnd,
        "pid": pid or spawned_pid,
        "title": title,
        "message": f"Successfully launched {entry['name']} (PID: {pid or spawned_pid}, HWND: {hwnd}, focused: {focused})",
    }


def execute_open_url(params: Dict[str, Any]) -> Dict[str, Any]:
    """Open a URL in the host's default web browser."""
    raw_url = str(params.get("url") or "").strip()
    if not raw_url:
        return {"success": False, "error": "URL is required"}

    if "://" not in raw_url and not any(raw_url.lower().startswith(s + ":") for s in (SAFE_URL_SCHEMES | DANGEROUS_URL_SCHEMES)):
        raw_url = "https://" + raw_url

    try:
        parsed = urlparse(raw_url)
    except Exception:
        return {"success": False, "error": f"Invalid URL format: '{raw_url}'"}

    scheme = (parsed.scheme or "").lower()
    if scheme in DANGEROUS_URL_SCHEMES:
        return {
            "success": False,
            "error": f"URL scheme '{scheme}://' is rejected for security. Only http:// and https:// allowed.",
        }

    if scheme not in SAFE_URL_SCHEMES:
        return {
            "success": False,
            "error": f"Unsupported URL scheme '{scheme}://'. Only http:// and https:// allowed.",
        }

    if not parsed.netloc:
        return {"success": False, "error": f"URL '{raw_url}' has no valid hostname."}

    try:
        opened = webbrowser.open(raw_url)
        if not opened:
            # Do not try a second launcher: browser.open may have caused a
            # navigation even when its backend reports False, so falling back
            # to startfile/xdg-open can create a duplicate tab or window.
            return {
                "success": False,
                "action": "open_url",
                "url": raw_url,
                "error": "Browser launcher did not confirm the navigation; no fallback was attempted.",
                "execution_status_uncertain": True,
            }
        return {
            "success": True,
            "action": "open_url",
            "url": raw_url,
            "host": parsed.netloc,
            "message": f"Opened {raw_url} in browser",
        }
    except Exception as e:
        return {
            "success": False,
            "error": f"Failed to open URL '{raw_url}': {e}",
            "execution_status_uncertain": True,
        }


def _is_wayland_session() -> bool:
    """Return True if the current Linux desktop session is running under Wayland."""
    session_type = os.environ.get("XDG_SESSION_TYPE", "").strip().lower()
    if session_type == "wayland":
        return True
    if session_type == "x11":
        return False
    return bool(os.environ.get("WAYLAND_DISPLAY", "").strip())


def _is_image_all_black(img: Any) -> bool:
    """Return True if a PIL Image has invalid dimensions or is entirely black/near-zero."""
    from PIL import ImageStat

    w = getattr(img, "width", 0) or (img.size[0] if hasattr(img, "size") else 0)
    h = getattr(img, "height", 0) or (img.size[1] if hasattr(img, "size") else 0)
    if w <= 0 or h <= 0:
        return True

    rgb_img = img.convert("RGB") if getattr(img, "mode", None) != "RGB" else img
    if hasattr(rgb_img, "getbbox") and rgb_img.getbbox() is None:
        return True

    stat = ImageStat.Stat(rgb_img)
    extrema = stat.extrema or []
    mean = stat.mean or []
    if not extrema:
        return True

    if all(hi == 0 for _, hi in extrema):
        return True
    if all(hi <= 2 for _, hi in extrema) and all(m < 0.5 for m in mean):
        return True

    return False


def _finalize_captured_image(
    img: Any,
    raw_jpeg_bytes: Optional[bytes] = None,
) -> Tuple[int, int, bytes, str]:
    """Validate that a captured PIL Image is non-empty and not all-black, then return JPEG bytes + base64."""
    import io

    w = getattr(img, "width", 0) or (img.size[0] if hasattr(img, "size") else 0)
    h = getattr(img, "height", 0) or (img.size[1] if hasattr(img, "size") else 0)
    if w <= 0 or h <= 0:
        raise ValueError(f"Invalid screenshot dimensions: {w}x{h}")

    rgb_img = img.convert("RGB") if getattr(img, "mode", None) != "RGB" else img
    if _is_image_all_black(rgb_img):
        raise ValueError(
            f"Captured {w}x{h} screenshot frame is entirely black (all pixels zero/near-zero)"
        )

    encode_started = time.perf_counter()
    if raw_jpeg_bytes is None:
        buf = io.BytesIO()
        rgb_img.save(buf, format="JPEG", quality=85)
        raw_jpeg_bytes = buf.getvalue()
    jpeg_encode_seconds = time.perf_counter() - encode_started

    if not raw_jpeg_bytes:
        raise ValueError("Encoded screenshot JPEG payload is empty")

    base64_started = time.perf_counter()
    b64_data = base64.b64encode(raw_jpeg_bytes).decode("ascii")
    _SCREENSHOT_TIMING.jpeg_encode_seconds = jpeg_encode_seconds
    _SCREENSHOT_TIMING.base64_encode_seconds = time.perf_counter() - base64_started
    return int(w), int(h), raw_jpeg_bytes, b64_data


def _load_and_validate_image_file(file_path: str) -> Tuple[int, int, bytes, str]:
    """Load an image file from disk, validate that it is non-empty and not all-black, and return JPEG data."""
    from PIL import Image

    if not os.path.exists(file_path) or os.path.getsize(file_path) == 0:
        raise RuntimeError("Screenshot output file was not created or is empty")

    with Image.open(file_path) as im:
        im.load()
        is_jpeg = getattr(im, "format", "").upper() == "JPEG"
        raw_jpeg = None
        if is_jpeg:
            with open(file_path, "rb") as f:
                raw_jpeg = f.read()
        return _finalize_captured_image(im, raw_jpeg_bytes=raw_jpeg)


def _import_gi_gio_glib() -> Tuple[Any, Any]:
    """Import PyGObject Gio and GLib, including system dist-packages fallback when running inside a venv."""
    import sys

    try:
        import gi
        gi.require_version("Gio", "2.0")
        gi.require_version("GLib", "2.0")
        from gi.repository import Gio, GLib
        return Gio, GLib
    except ImportError:
        for dist_dir in ("/usr/lib/python3/dist-packages", "/usr/lib64/python3/dist-packages"):
            if os.path.isdir(dist_dir) and dist_dir not in sys.path:
                sys.path.append(dist_dir)
        import gi
        gi.require_version("Gio", "2.0")
        gi.require_version("GLib", "2.0")
        from gi.repository import Gio, GLib
        return Gio, GLib


def _is_gnome_screen_locked(bus: Optional[Any] = None) -> bool:
    """Return True if the GNOME screen lock / screensaver is currently active."""
    try:
        Gio, GLib = _import_gi_gio_glib()
        if bus is None:
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        res = bus.call_sync(
            "org.gnome.ScreenSaver",
            "/org/gnome/ScreenSaver",
            "org.gnome.ScreenSaver",
            "GetActive",
            None,
            GLib.VariantType("(b)"),
            Gio.DBusCallFlags.NONE,
            1000,
            None,
        )
        return bool(res.unpack()[0])
    except Exception:
        return False


def _capture_with_mutter_screencast() -> Tuple[int, int, bytes, str]:
    """Capture desktop on GNOME Wayland via org.gnome.Mutter.ScreenCast D-Bus + PipeWire GStreamer."""
    import tempfile

    with _MUTTER_SCREENCAST_LOCK:
        Gio, GLib = _import_gi_gio_glib()
        tmp_path: Optional[str] = None
        ctx = None
        ctx_pushed = False
        bus = None
        session_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                tmp_path = tmp.name

            ctx = GLib.MainContext.new()
            ctx.push_thread_default()
            ctx_pushed = True

            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            res = bus.call_sync(
                "org.gnome.Mutter.ScreenCast",
                "/org/gnome/Mutter/ScreenCast",
                "org.gnome.Mutter.ScreenCast",
                "CreateSession",
                GLib.Variant("(a{sv})", ({},)),
                GLib.VariantType("(o)"),
                Gio.DBusCallFlags.NONE,
                5000,
                None,
            )
            session_path = res.unpack()[0]

            stream_res = bus.call_sync(
                "org.gnome.Mutter.ScreenCast",
                session_path,
                "org.gnome.Mutter.ScreenCast.Session",
                "RecordMonitor",
                GLib.Variant("(sa{sv})", ("", {"cursor-mode": GLib.Variant("u", 1)})),
                GLib.VariantType("(o)"),
                Gio.DBusCallFlags.NONE,
                5000,
                None,
            )
            stream_path = stream_res.unpack()[0]

            loop = GLib.MainLoop.new(ctx, False)
            node_ids = []

            def _on_stream_added(connection, sender, object_path, interface_name, signal_name, parameters):
                node_ids.append(parameters.unpack()[0])
                loop.quit()

            sub_id = bus.signal_subscribe(
                "org.gnome.Mutter.ScreenCast",
                "org.gnome.Mutter.ScreenCast.Stream",
                "PipeWireStreamAdded",
                stream_path,
                None,
                Gio.DBusSignalFlags.NONE,
                _on_stream_added,
            )
            try:
                bus.call_sync(
                    "org.gnome.Mutter.ScreenCast",
                    session_path,
                    "org.gnome.Mutter.ScreenCast.Session",
                    "Start",
                    None,
                    None,
                    Gio.DBusCallFlags.NONE,
                    5000,
                    None,
                )
                timeout_src = GLib.timeout_source_new(3000)
                timeout_src.set_callback(lambda *_: (loop.quit(), False)[1])
                timeout_src.attach(ctx)
                loop.run()
                timeout_src.destroy()
            finally:
                bus.signal_unsubscribe(sub_id)

            if not node_ids:
                raise RuntimeError("Mutter ScreenCast did not emit a PipeWire stream node within 3s")

            node_id = node_ids[0]
            gst_res = subprocess.run(
                [
                    "gst-launch-1.0",
                    "-q",
                    "pipewiresrc",
                    f"path={node_id}",
                    "num-buffers=1",
                    "!",
                    "videoconvert",
                    "!",
                    "jpegenc",
                    "quality=85",
                    "!",
                    "filesink",
                    f"location={tmp_path}",
                ],
                capture_output=True,
                text=True,
                timeout=8,
            )
            if gst_res.returncode != 0:
                err_txt = (gst_res.stderr or "").strip() or f"exit code {gst_res.returncode}"
                raise RuntimeError(f"GStreamer pipewiresrc failed: {err_txt}")

            return _load_and_validate_image_file(tmp_path)
        except PermissionError:
            raise
        except Exception as e:
            err_msg = str(e)
            err_lower = err_msg.lower()
            if "session creation inhibited" in err_lower:
                screen_locked = _is_gnome_screen_locked(bus=bus)
                if screen_locked:
                    diag = "org.gnome.ScreenSaver reports the desktop is locked"
                else:
                    diag = "desktop may be locked, suspended, or in a restricted GNOME session mode"
                raise PermissionError(
                    f"Desktop capture is unavailable because GNOME has restricted ScreenCast access "
                    f"({err_msg}; {diag}). Unlock the desktop and retry."
                ) from e
            if any(tok in err_lower for tok in ("accessdenied", "not allowed", "permissiondenied")):
                raise PermissionError(f"Mutter ScreenCast access denied: {err_msg}") from e
            raise
        finally:
            if bus is not None and session_path is not None:
                try:
                    bus.call_sync(
                        "org.gnome.Mutter.ScreenCast",
                        session_path,
                        "org.gnome.Mutter.ScreenCast.Session",
                        "Stop",
                        None,
                        None,
                        Gio.DBusCallFlags.NONE,
                        2000,
                        None,
                    )
                except Exception:
                    pass
            if ctx is not None and ctx_pushed:
                try:
                    ctx.pop_thread_default()
                except Exception:
                    pass
            if tmp_path:
                try:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
                except OSError:
                    pass


def _capture_with_gnome_screenshot() -> Tuple[int, int, bytes, str]:
    """Capture desktop via gnome-screenshot CLI."""
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        res = subprocess.run(["gnome-screenshot", "-f", tmp_path], capture_output=True, timeout=5)
        if res.returncode != 0:
            stderr = (res.stderr or b"").decode("utf-8", errors="replace").strip()
            if any(tok in stderr.lower() for tok in ("accessdenied", "not allowed", "permission")):
                raise PermissionError(f"gnome-screenshot permission denied: {stderr}")
            raise RuntimeError(f"gnome-screenshot failed (exit {res.returncode}): {stderr or 'no output'}")
        return _load_and_validate_image_file(tmp_path)
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass


def _capture_with_gnome_shell_dbus() -> Tuple[int, int, bytes, str]:
    """Capture desktop via org.gnome.Shell.Screenshot D-Bus interface."""
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        res = subprocess.run(
            [
                "gdbus",
                "call",
                "--session",
                "--dest",
                "org.gnome.Shell.Screenshot",
                "--object-path",
                "/org/gnome/Shell/Screenshot",
                "--method",
                "org.gnome.Shell.Screenshot.Screenshot",
                "false",
                "false",
                tmp_path,
            ],
            capture_output=True,
            timeout=5,
        )
        if res.returncode != 0:
            stderr = (res.stderr or b"").decode("utf-8", errors="replace").strip()
            if any(tok in stderr.lower() for tok in ("accessdenied", "not allowed", "permission")):
                raise PermissionError(f"GNOME Shell Screenshot D-Bus denied: {stderr}")
            raise RuntimeError(f"GNOME Shell Screenshot D-Bus failed (exit {res.returncode}): {stderr}")
        return _load_and_validate_image_file(tmp_path)
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass


def _capture_with_grim() -> Tuple[int, int, bytes, str]:
    """Capture desktop on wlroots Wayland compositors via grim (using PNG output to avoid disabled JPEG builds)."""
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        res = subprocess.run(["grim", tmp_path], capture_output=True, timeout=5)
        if res.returncode != 0:
            stderr = (res.stderr or b"").decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"grim failed (exit {res.returncode}): {stderr or 'unknown error'}")
        return _load_and_validate_image_file(tmp_path)
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass


def _capture_with_mss() -> Tuple[int, int, bytes, str]:
    """Capture desktop via mss and reject all-black frames (such as Xwayland root buffers on Wayland)."""
    import mss
    from PIL import Image

    mss_cls = getattr(mss, "MSS", mss.mss)
    with mss_cls() as sct:
        mon = sct.monitors[0] if len(sct.monitors) > 0 else sct.monitors[1]
        sct_img = sct.grab(mon)
        img = Image.frombytes("RGB", sct_img.size, sct_img.bgra, "raw", "BGRX")
        return _finalize_captured_image(img)


def _capture_with_scrot() -> Tuple[int, int, bytes, str]:
    """Capture desktop via scrot and reject all-black frames."""
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        res = subprocess.run(["scrot", "-z", tmp_path], capture_output=True, timeout=5)
        if res.returncode != 0:
            stderr = (res.stderr or b"").decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"scrot failed (exit {res.returncode}): {stderr or 'unknown error'}")
        return _load_and_validate_image_file(tmp_path)
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass


def _capture_desktop_screenshot_raw() -> Tuple[int, int, bytes, str]:
    """Capture desktop screenshot raw bytes and dimensions across Windows and Linux.
    
    Returns (width, height, raw_jpeg_bytes, base64_jpeg_string).
    """
    if IS_WINDOWS:
        res = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", PS_SCREENSHOT_SCRIPT],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if res.returncode != 0:
            raise RuntimeError(f"PowerShell capture failed: {res.stderr.strip()}")

        lines = res.stdout.strip().split("\n")
        if len(lines) < 2:
            raise RuntimeError("Invalid output from screen capture helper")

        dims = lines[0].strip()
        b64_data = "".join(lines[1:]).strip()
        w, h = map(int, dims.split("x"))
        raw_bytes = base64.b64decode(b64_data)
        return w, h, raw_bytes, b64_data

    wayland = _is_wayland_session()
    if wayland:
        # On Wayland, prefer Wayland-compatible capture backends first.
        # X11/Xwayland tools (mss, scrot) are tried only as validated last-resort fallbacks
        # and are strictly rejected if they return all-black frames.
        strategies = [
            ("mutter_screencast", _capture_with_mutter_screencast),
            ("gnome_screenshot", _capture_with_gnome_screenshot),
            ("gnome_shell_dbus", _capture_with_gnome_shell_dbus),
            ("grim", _capture_with_grim),
            ("mss", _capture_with_mss),
            ("scrot", _capture_with_scrot),
        ]
    else:
        strategies = [
            ("mss", _capture_with_mss),
            ("scrot", _capture_with_scrot),
            ("gnome_screenshot", _capture_with_gnome_screenshot),
            ("mutter_screencast", _capture_with_mutter_screencast),
            ("grim", _capture_with_grim),
        ]

    errors: list[str] = []
    saw_permission_error = False

    for name, fn in strategies:
        try:
            result = fn()
            logger.debug("Linux screenshot captured successfully via '%s'", name)
            return result
        except PermissionError as e:
            saw_permission_error = True
            logger.debug("Screenshot strategy '%s' permission denied: %s", name, e)
            errors.append(f"{name}: {e}")
            err_lower = str(e).lower()
            if wayland and name == "mutter_screencast" and (
                "session creation inhibited" in err_lower
                or "restricted screencast access" in err_lower
                or "desktop session is locked" in err_lower
            ):
                session_type = os.environ.get("XDG_SESSION_TYPE", "unknown")
                desktop = os.environ.get("XDG_CURRENT_DESKTOP", "unknown")
                raise PermissionError(
                    f"{e} (session={session_type}, desktop={desktop})"
                ) from e
        except Exception as e:
            err_str = str(e)
            err_lower = err_str.lower()
            if wayland and name == "mutter_screencast" and "session creation inhibited" in err_lower:
                session_type = os.environ.get("XDG_SESSION_TYPE", "unknown")
                desktop = os.environ.get("XDG_CURRENT_DESKTOP", "unknown")
                raise PermissionError(
                    f"Desktop capture is unavailable because GNOME has restricted ScreenCast access "
                    f"({err_str}). Unlock the desktop and retry. (session={session_type}, desktop={desktop})"
                ) from e
            if any(tok in err_lower for tok in ("accessdenied", "not allowed", "permission denied")):
                saw_permission_error = True
            logger.debug("Screenshot strategy '%s' failed: %s", name, e)
            errors.append(f"{name}: {e}")

    session_type = os.environ.get("XDG_SESSION_TYPE", "unknown")
    desktop = os.environ.get("XDG_CURRENT_DESKTOP", "unknown")
    detail_str = "; ".join(errors) if errors else "no capture mechanisms available"
    error_message = (
        f"No valid desktop screenshot could be captured on Linux "
        f"(session={session_type}, desktop={desktop}). "
        f"Attempted methods: {detail_str}"
    )
    if saw_permission_error:
        raise PermissionError(error_message)
    raise RuntimeError(error_message)


def execute_screenshot(params: Dict[str, Any]) -> Dict[str, Any]:
    """Capture the actual desktop screen across Windows and Linux."""
    capture_started = time.perf_counter()
    _SCREENSHOT_TIMING.jpeg_encode_seconds = 0.0
    _SCREENSHOT_TIMING.base64_encode_seconds = 0.0
    try:
        w, h, raw_bytes, b64_data = _capture_desktop_screenshot_raw()
        total_host_seconds = time.perf_counter() - capture_started
        jpeg_encode_seconds = float(getattr(_SCREENSHOT_TIMING, "jpeg_encode_seconds", 0.0))
        base64_encode_seconds = float(getattr(_SCREENSHOT_TIMING, "base64_encode_seconds", 0.0))
        capture_seconds = max(0.0, total_host_seconds - jpeg_encode_seconds - base64_encode_seconds)

        ws_path = Path(HOST_WORKSPACE_DIR)
        screenshots_dir = ws_path / "screenshots"
        screenshots_dir.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"screenshot_{timestamp}.jpg"
        host_file = screenshots_dir / filename
        host_file.write_bytes(raw_bytes)

        docker_file_path = f"/workspace/jarvis/screenshots/{filename}"

        return {
            "success": True,
            "action": "screenshot",
            "width": w,
            "height": h,
            "format": "jpeg",
            "size_bytes": len(raw_bytes),
            "capture_seconds": round(capture_seconds, 3),
            "jpeg_encode_seconds": round(jpeg_encode_seconds, 3),
            "base64_encode_seconds": round(base64_encode_seconds, 3),
            "file_path": docker_file_path,
            "host_file_path": str(host_file),
            "image_base64": b64_data,
            "message": f"Desktop screenshot captured ({w}x{h}, {len(raw_bytes)} bytes)",
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "Screen capture timed out after 10 seconds"}
    except PermissionError as e:
        return {"success": False, "error": f"Screen capture permission denied: {e}"}
    except Exception as e:
        return {"success": False, "error": f"Screen capture error: {e}"}



# ---------------------------------------------------------------------------
# GUI Input Actions (Click, Double Click, Type Text, Press Key, Scroll)
# ---------------------------------------------------------------------------

def execute_click(params: Dict[str, Any]) -> Dict[str, Any]:
    """Simulate a mouse click at specific screen coordinates (x, y)."""
    _enforce_gui_rate_limit()
    w, h = get_screen_bounds()

    x, err_x = _validate_coordinate(params.get("x"), "x", w)
    if err_x:
        return {"success": False, "error": err_x}

    y, err_y = _validate_coordinate(params.get("y"), "y", h)
    if err_y:
        return {"success": False, "error": err_y}

    if IS_WINDOWS or (hasattr(ctypes, "windll") and hasattr(ctypes.windll, "user32") and not IS_LINUX):
        try:
            user32 = ctypes.windll.user32
            user32.SetCursorPos(x, y)
            time.sleep(0.02)
            user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            time.sleep(0.02)
            user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            return {
                "success": True,
                "action": "click",
                "x": x,
                "y": y,
                "message": f"Clicked at ({x}, {y})",
            }
        except Exception as e:
            return {"success": False, "error": f"Click execution failed: {e}"}

    # Linux implementation
    try:
        res = subprocess.run(
            ["xdotool", "mousemove", str(x), str(y), "click", "1"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if res.returncode == 0:
            return {
                "success": True,
                "action": "click",
                "x": x,
                "y": y,
                "backend": "xdotool",
                "message": f"Clicked at ({x}, {y})",
            }
    except Exception as e:
        logger.debug("xdotool click failed: %s; falling back to pyautogui", e)

    try:
        import pyautogui
        pyautogui.click(x=x, y=y)
        return {
            "success": True,
            "action": "click",
            "x": x,
            "y": y,
            "backend": "pyautogui",
            "message": f"Clicked at ({x}, {y})",
        }
    except Exception as e:
        return {"success": False, "error": f"Click execution failed: {e}"}


def execute_double_click(params: Dict[str, Any]) -> Dict[str, Any]:
    """Simulate a mouse double-click at specific screen coordinates (x, y)."""
    _enforce_gui_rate_limit()
    w, h = get_screen_bounds()

    x, err_x = _validate_coordinate(params.get("x"), "x", w)
    if err_x:
        return {"success": False, "error": err_x}

    y, err_y = _validate_coordinate(params.get("y"), "y", h)
    if err_y:
        return {"success": False, "error": err_y}

    if IS_WINDOWS or (hasattr(ctypes, "windll") and hasattr(ctypes.windll, "user32") and not IS_LINUX):
        try:
            user32 = ctypes.windll.user32
            user32.SetCursorPos(x, y)
            time.sleep(0.02)
            # First click
            user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            time.sleep(0.02)
            user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            time.sleep(0.06)
            # Second click
            user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            time.sleep(0.02)
            user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            return {
                "success": True,
                "action": "double_click",
                "x": x,
                "y": y,
                "message": f"Double-clicked at ({x}, {y})",
            }
        except Exception as e:
            return {"success": False, "error": f"Double-click execution failed: {e}"}

    # Linux implementation
    try:
        res = subprocess.run(
            ["xdotool", "mousemove", str(x), str(y), "click", "--repeat", "2", "--delay", "50", "1"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if res.returncode == 0:
            return {
                "success": True,
                "action": "double_click",
                "x": x,
                "y": y,
                "backend": "xdotool",
                "message": f"Double-clicked at ({x}, {y})",
            }
    except Exception as e:
        logger.debug("xdotool double_click failed: %s; falling back to pyautogui", e)

    try:
        import pyautogui
        pyautogui.doubleClick(x=x, y=y)
        return {
            "success": True,
            "action": "double_click",
            "x": x,
            "y": y,
            "backend": "pyautogui",
            "message": f"Double-clicked at ({x}, {y})",
        }
    except Exception as e:
        return {"success": False, "error": f"Double-click execution failed: {e}"}



def execute_type_text(params: Dict[str, Any]) -> Dict[str, Any]:
    """Type text into the focused window/element with length, character, and focus guards."""
    _enforce_gui_rate_limit()
    text = params.get("text")
    if text is None or not isinstance(text, str):
        return {"success": False, "error": "Parameter 'text' is required and must be a string"}

    if len(text) == 0:
        return {"success": False, "error": "Parameter 'text' cannot be empty"}

    if len(text) > MAX_TYPE_TEXT_LENGTH:
        return {
            "success": False,
            "error": f"Text length ({len(text)}) exceeds maximum allowable limit of {MAX_TYPE_TEXT_LENGTH} characters",
        }

    # Reject dangerous control characters that could simulate commands
    for ch in text:
        code = ord(ch)
        if (code < 32 and ch not in ("\n", "\t", "\r")) or code == 127:
            return {
                "success": False,
                "error": f"Disallowed control character detected in text (ASCII code {code})",
            }

    # Check target application or HWND focus
    target_app = str(params.get("application") or params.get("app_name") or params.get("target_app") or "").strip()
    target_hwnd = params.get("hwnd")

    # If target app is specified but target_hwnd is not, look up HWND
    if not target_hwnd and target_app:
        app_key = normalize_app_name(target_app)
        entry = APPLICATION_ALLOWLIST.get(app_key) or APPLICATION_ALLOWLIST.get(target_app.lower())
        if entry:
            target_hwnd, _, _ = find_application_window(entry)

    fg_info = get_foreground_window_info()

    # Re-focus target window if target was specified
    ed_focus = {}
    gti = {}
    if target_hwnd:
        if fg_info.get("hwnd") != target_hwnd:
            logger.info("Target window (HWND %s) not in foreground (active: HWND %s, title '%s'); re-focusing...", target_hwnd, fg_info.get("hwnd"), fg_info.get("title"))
            restore_and_focus_window(target_hwnd)
            time.sleep(0.05)
            fg_info = get_foreground_window_info()

        if fg_info.get("hwnd") != target_hwnd:
            logger.warning("Target window (HWND %s) could not be brought to foreground before typing.", target_hwnd)
            return {
                "success": False,
                "action": "type_text",
                "length": len(text),
                "focused": False,
                "keyboard_focus_established": False,
                "target_hwnd": target_hwnd,
                "hwnd": target_hwnd,
                "foreground_hwnd": fg_info.get("hwnd"),
                "foreground_pid": fg_info.get("pid"),
                "focus_hwnd": None,
                "caret_hwnd": None,
                "control": None,
                "control_hwnd": None,
                "foreground_title": fg_info.get("title"),
                "error": f"Target application window (HWND {target_hwnd}) could not be brought to foreground (active: '{fg_info.get('title')}'). Keystrokes were not dispatched to prevent misdirected input.",
            }

        # Ensure keyboard/input control focus
        ed_focus = ensure_editor_focus(target_hwnd)
        gti = ed_focus.get("gti") or get_gui_thread_info_for_window(target_hwnd)
        keyboard_focus_ok = ed_focus.get("established", False)

        # Fallback assertion: check if gti.hwndFocus belongs to target window without active menu
        if not keyboard_focus_ok:
            hwnd_focus = gti.get("hwndFocus")
            if hwnd_focus and not gti.get("hwndMenuOwner"):
                user32 = ctypes.windll.user32
                if hwnd_focus == target_hwnd or (hasattr(user32, "IsChild") and user32.IsChild(target_hwnd, hwnd_focus)):
                    keyboard_focus_ok = True

        if not keyboard_focus_ok:
            logger.warning("Target window (HWND %s) does not have keyboard input focus before typing.", target_hwnd)
            return {
                "success": False,
                "action": "type_text",
                "length": len(text),
                "focused": False,
                "keyboard_focus_established": False,
                "target_hwnd": target_hwnd,
                "hwnd": target_hwnd,
                "foreground_hwnd": fg_info.get("hwnd"),
                "foreground_pid": fg_info.get("pid"),
                "focus_hwnd": gti.get("hwndFocus"),
                "caret_hwnd": gti.get("hwndCaret"),
                "control": ed_focus.get("control"),
                "control_hwnd": ed_focus.get("control_hwnd"),
                "foreground_title": fg_info.get("title"),
                "error": f"Target application window (HWND {target_hwnd}) does not have verified keyboard focus (focus HWND: {gti.get('hwndFocus')}, control: {ed_focus.get('control')}). Keystrokes were not dispatched to prevent misdirected input.",
            }
    else:
        fg_hwnd = fg_info.get("hwnd")
        gti = get_gui_thread_info_for_window(fg_hwnd) if fg_hwnd else {}

    control_hwnd = ed_focus.get("control_hwnd")
    user32 = ctypes.windll.user32
    kernel32 = getattr(ctypes.windll, "kernel32", None)
    pre_len = 0
    if control_hwnd and hasattr(user32, "SendMessageW"):
        try:
            pre_len = user32.SendMessageW(control_hwnd, 0x000E, 0, 0)  # WM_GETTEXTLENGTH
        except Exception:
            pre_len = 0

    try:
        send_input = user32.SendInput
        send_input.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
        send_input.restype = wintypes.UINT

        total_dispatched = 0
        input_size = ctypes.sizeof(INPUT)

        for ch in text:
            code = ord(ch)
            # Unicode Key Down
            inp_down = INPUT(type=INPUT_KEYBOARD)
            inp_down.ki.wVk = 0
            inp_down.ki.wScan = code
            inp_down.ki.dwFlags = KEYEVENTF_UNICODE
            inp_down.ki.time = 0
            inp_down.ki.dwExtraInfo = 0

            # Unicode Key Up
            inp_up = INPUT(type=INPUT_KEYBOARD)
            inp_up.ki.wVk = 0
            inp_up.ki.wScan = code
            inp_up.ki.dwFlags = KEYEVENTF_UNICODE | KEYEVENTF_KEYUP
            inp_up.ki.time = 0
            inp_up.ki.dwExtraInfo = 0

            inputs = (INPUT * 2)(inp_down, inp_up)
            if kernel32 and hasattr(kernel32, "SetLastError"):
                kernel32.SetLastError(0)
            ret = send_input(2, inputs, input_size)
            if ret == 2:
                total_dispatched += 1
            else:
                last_err = kernel32.GetLastError() if kernel32 and hasattr(kernel32, "GetLastError") else 0
                logger.warning("SendInput failed for character '%s': ret=%s, err=%s", ch, ret, last_err)
            time.sleep(0.005)

        delivered_hwnd = target_hwnd or fg_info.get("hwnd")
        delivered_title = fg_info.get("title") or target_app or "focused window"

        if total_dispatched == 0 and len(text) > 0:
            return {
                "success": False,
                "action": "type_text",
                "length": len(text),
                "dispatched": 0,
                "delivered": False,
                "delivery_verified": False,
                "focused": True,
                "keyboard_focus_established": True,
                "target_hwnd": target_hwnd,
                "hwnd": delivered_hwnd,
                "foreground_hwnd": fg_info.get("hwnd"),
                "foreground_pid": fg_info.get("pid"),
                "error": f"SendInput failed to dispatch keystrokes into foreground window (0/{len(text)} dispatched).",
            }

        # Lightweight deterministic delivery verification
        time.sleep(0.05)
        delivery_verified = False
        delivery_method = "none"

        # Check 1: Editor text length or content check via WM_GETTEXT
        if control_hwnd and hasattr(user32, "SendMessageW"):
            try:
                post_len = user32.SendMessageW(control_hwnd, 0x000E, 0, 0)  # WM_GETTEXTLENGTH
                if post_len >= pre_len + len(text) or post_len > pre_len:
                    delivery_verified = True
                    delivery_method = "wm_gettextlength"
                else:
                    buf = ctypes.create_unicode_buffer(1024)
                    user32.SendMessageW(control_hwnd, 0x000D, 1024, buf)
                    if text in buf.value:
                        delivery_verified = True
                        delivery_method = "wm_gettext"
            except Exception as e:
                logger.debug("WM_GETTEXT check failed: %s", e)

        # Check 2: Caret and focus check if control does not support WM_GETTEXT
        if not delivery_verified:
            post_gti = get_gui_thread_info_for_window(delivered_hwnd) if delivered_hwnd else {}
            post_fg = user32.GetForegroundWindow() if hasattr(user32, "GetForegroundWindow") else None
            if post_fg == delivered_hwnd and (post_gti.get("hwndFocus") or post_gti.get("hwndCaret")):
                delivery_verified = True
                delivery_method = "caret_focus_confirmed"

        return {
            "success": True,
            "action": "type_text",
            "length": len(text),
            "dispatched": total_dispatched,
            "delivered": delivery_verified or (total_dispatched == len(text)),
            "delivery_verified": delivery_verified,
            "delivery_method": delivery_method,
            "focused": True,
            "keyboard_focus_established": True,
            "target_hwnd": target_hwnd,
            "hwnd": delivered_hwnd,
            "foreground_hwnd": fg_info.get("hwnd"),
            "foreground_pid": fg_info.get("pid"),
            "focus_hwnd": gti.get("hwndFocus"),
            "caret_hwnd": gti.get("hwndCaret"),
            "control": ed_focus.get("control"),
            "control_hwnd": ed_focus.get("control_hwnd"),
            "target": target_app or None,
            "foreground_title": delivered_title,
            "message": f"Successfully typed {total_dispatched} characters into '{delivered_title}' (HWND {delivered_hwnd}, Control: {ed_focus.get('control')}, verified via {delivery_method})",
        }
    except Exception as e:
        return {"success": False, "error": f"Type text execution failed: {e}"}


def execute_press_key(params: Dict[str, Any]) -> Dict[str, Any]:
    """Press a single keyboard key from the strict allowlist."""
    _enforce_gui_rate_limit()
    key_raw = params.get("key")
    if not key_raw or not isinstance(key_raw, str):
        return {"success": False, "error": "Parameter 'key' is required and must be a string"}

    key_normalized = key_raw.strip().upper()
    vk_code = KEY_ALLOWLIST.get(key_normalized)
    if not vk_code:
        allowed = sorted(KEY_ALLOWLIST.keys())
        return {
            "success": False,
            "error": f"Key '{key_raw}' is not allowed. Allowed keys: {', '.join(allowed)}",
        }

    if IS_WINDOWS or (hasattr(ctypes, "windll") and hasattr(ctypes.windll, "user32") and not IS_LINUX):
        try:
            user32 = ctypes.windll.user32
            # Key down
            user32.keybd_event(vk_code, 0, 0, 0)
            time.sleep(0.03)
            # Key up
            user32.keybd_event(vk_code, 0, KEYEVENTF_KEYUP, 0)
            return {
                "success": True,
                "action": "press_key",
                "key": key_normalized,
                "message": f"Pressed key '{key_normalized}'",
            }
        except Exception as e:
            return {"success": False, "error": f"Key press execution failed: {e}"}

    # Linux implementation
    x_key = LINUX_KEY_NAME_MAP.get(key_normalized)
    if x_key:
        try:
            res = subprocess.run(["xdotool", "key", x_key], capture_output=True, text=True, timeout=3)
            if res.returncode == 0:
                return {
                    "success": True,
                    "action": "press_key",
                    "key": key_normalized,
                    "backend": "xdotool",
                    "message": f"Pressed key '{key_normalized}'",
                }
        except Exception as e:
            logger.debug("xdotool key press failed: %s", e)

    py_key = PYAUTOGUI_KEY_MAP.get(key_normalized, key_normalized.lower())
    try:
        import pyautogui
        pyautogui.press(py_key)
        return {
            "success": True,
            "action": "press_key",
            "key": key_normalized,
            "backend": "pyautogui",
            "message": f"Pressed key '{key_normalized}'",
        }
    except Exception as e:
        return {"success": False, "error": f"Key press execution failed on Linux: {e}"}


def execute_scroll(params: Dict[str, Any]) -> Dict[str, Any]:
    """Scroll mouse wheel up or down by a bounded amount."""
    _enforce_gui_rate_limit()
    direction = str(params.get("direction") or "").strip().lower()
    if direction not in ("up", "down"):
        return {"success": False, "error": f"Invalid scroll direction '{direction}'. Must be 'up' or 'down'."}

    raw_amount = params.get("amount", DEFAULT_SCROLL_AMOUNT)
    if isinstance(raw_amount, bool) or raw_amount is None:
        return {"success": False, "error": "Scroll amount must be an integer"}

    try:
        amount = int(raw_amount)
    except (ValueError, TypeError):
        return {"success": False, "error": f"Scroll amount '{raw_amount}' is not a valid integer"}

    if amount < MIN_SCROLL_AMOUNT:
        return {"success": False, "error": f"Scroll amount ({amount}) must be at least {MIN_SCROLL_AMOUNT}"}

    if amount > MAX_SCROLL_AMOUNT:
        return {"success": False, "error": f"Scroll amount ({amount}) exceeds maximum allowed of {MAX_SCROLL_AMOUNT}"}

    if IS_WINDOWS or (hasattr(ctypes, "windll") and hasattr(ctypes.windll, "user32") and not IS_LINUX):
        try:
            user32 = ctypes.windll.user32
            wheel_delta = WHEEL_DELTA * amount if direction == "up" else -WHEEL_DELTA * amount
            user32.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, wheel_delta, 0)
            return {
                "success": True,
                "action": "scroll",
                "direction": direction,
                "amount": amount,
                "message": f"Scrolled {direction} by {amount} steps",
            }
        except Exception as e:
            return {"success": False, "error": f"Scroll execution failed: {e}"}

    # Linux implementation
    button = "4" if direction == "up" else "5"
    try:
        res = subprocess.run(
            ["xdotool", "click", "--repeat", str(amount), button],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if res.returncode == 0:
            return {
                "success": True,
                "action": "scroll",
                "direction": direction,
                "amount": amount,
                "backend": "xdotool",
                "message": f"Scrolled {direction} by {amount} steps",
            }
    except Exception as e:
        logger.debug("xdotool scroll failed: %s; falling back to pyautogui", e)

    try:
        import pyautogui
        clicks = amount if direction == "up" else -amount
        pyautogui.scroll(clicks)
        return {
            "success": True,
            "action": "scroll",
            "direction": direction,
            "amount": amount,
            "backend": "pyautogui",
            "message": f"Scrolled {direction} by {amount} steps",
        }
    except Exception as e:
        return {"success": False, "error": f"Scroll execution failed on Linux: {e}"}



# ---------------------------------------------------------------------------
# Registry of available actions
# ---------------------------------------------------------------------------
ACTION_HANDLERS = {
    "open_application": execute_open_application,
    "open_url": execute_open_url,
    "screenshot": execute_screenshot,
    "click": execute_click,
    "double_click": execute_double_click,
    "type_text": execute_type_text,
    "press_key": execute_press_key,
    "scroll": execute_scroll,
}
