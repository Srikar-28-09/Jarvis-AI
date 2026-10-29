"""
host_bridge/hotkey.py

Windows Global Hotkey Listener using pure Win32 RegisterHotKey via ctypes.
Runs as a lightweight background thread inside the Windows Host Bridge.
Brings the existing Jarvis UI window to the foreground or launches it in App Mode.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import logging
import threading
import time
from typing import Callable, Dict, Optional, Tuple

logger = logging.getLogger("host_bridge.hotkey")

# Win32 Constants
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012

# Virtual Key Mappings
_VK_NAME_MAP: Dict[str, int] = {
    "SPACE": 0x20,
    "RETURN": 0x0D,
    "ENTER": 0x0D,
    "TAB": 0x09,
    "ESCAPE": 0x1B,
    "ESC": 0x1B,
    "BACKSPACE": 0x08,
    "BACK": 0x08,
    "DELETE": 0x2E,
    "DEL": 0x2E,
    "INSERT": 0x2D,
    "INS": 0x2D,
    "HOME": 0x24,
    "END": 0x23,
    "PAGEUP": 0x21,
    "PAGE_UP": 0x21,
    "PAGEDOWN": 0x22,
    "PAGE_DOWN": 0x22,
    "UP": 0x26,
    "DOWN": 0x28,
    "LEFT": 0x25,
    "RIGHT": 0x27,
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


def parse_hotkey_string(combo_str: str) -> Tuple[int, int]:
    """Parse a shortcut string like 'Ctrl+Alt+Space' or 'Win+Alt+J' into (modifiers, vk_code).
    
    Raises ValueError on unrecognized modifiers or key names.
    """
    if not combo_str or not combo_str.strip():
        raise ValueError("Hotkey combination string cannot be empty")

    parts = [p.strip().upper() for p in combo_str.replace("-", "+").split("+") if p.strip()]
    if not parts:
        raise ValueError(f"Invalid hotkey string: '{combo_str}'")

    modifiers = 0
    vk = None

    for part in parts:
        if part in ("CTRL", "CONTROL"):
            modifiers |= MOD_CONTROL
        elif part == "ALT":
            modifiers |= MOD_ALT
        elif part == "SHIFT":
            modifiers |= MOD_SHIFT
        elif part in ("WIN", "WINDOWS", "SUPER", "META"):
            modifiers |= MOD_WIN
        else:
            if vk is not None:
                raise ValueError(f"Multiple non-modifier keys in hotkey string '{combo_str}': '{part}' and previously matched key")
            if part in _VK_NAME_MAP:
                vk = _VK_NAME_MAP[part]
            elif len(part) == 1 and (part.isalnum()):
                vk = ord(part)
            else:
                raise ValueError(f"Unrecognized key in hotkey string: '{part}'")

    if vk is None:
        raise ValueError(f"No valid trigger key found in hotkey string '{combo_str}'")

    return modifiers, vk


class Win32GlobalHotkey:
    """Manages registration and message dispatch for a global Windows hotkey."""

    def __init__(
        self,
        hotkey_str: str = "Ctrl+Alt+Space",
        callback: Optional[Callable[[], None]] = None,
        hotkey_id: int = 1001,
    ):
        self.hotkey_str = hotkey_str
        self.callback = callback
        self.hotkey_id = hotkey_id
        self._thread: Optional[threading.Thread] = None
        self._thread_id: Optional[int] = None
        self._running = False
        self._registered = False
        self._init_event = threading.Event()
        self._reg_error: Optional[str] = None

    @property
    def is_running(self) -> bool:
        return self._running and self._registered

    def start(self) -> bool:
        """Start the global hotkey message loop thread.
        
        Returns True if registration succeeded, False otherwise.
        """
        if not hasattr(ctypes, "windll") or not hasattr(ctypes.windll, "user32"):
            logger.warning("Win32 ctypes.windll.user32 not available; global hotkey disabled.")
            return False

        if self._running:
            return self._registered

        self._running = True
        self._init_event.clear()
        self._reg_error = None

        self._thread = threading.Thread(
            target=self._message_loop,
            name="JarvisGlobalHotkeyThread",
            daemon=True,
        )
        self._thread.start()

        # Wait for thread to initialize and attempt registration
        self._init_event.wait(timeout=2.0)
        if self._reg_error:
            logger.warning("Failed to register global hotkey '%s': %s", self.hotkey_str, self._reg_error)
            return False

        return self._registered

    def stop(self) -> None:
        """Unregister the global hotkey and terminate the message loop thread."""
        if not self._running:
            return

        self._running = False
        if hasattr(ctypes, "windll") and hasattr(ctypes.windll, "user32"):
            user32 = ctypes.windll.user32
            try:
                if self._registered:
                    user32.UnregisterHotKey(None, self.hotkey_id)
                    self._registered = False
                if self._thread_id:
                    user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
            except Exception as e:
                logger.debug("Error during hotkey unregistration: %s", e)

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
        self._thread = None
        self._thread_id = None
        logger.info("Global hotkey listener stopped.")

    def _message_loop(self) -> None:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        self._thread_id = kernel32.GetCurrentThreadId()

        try:
            modifiers, vk = parse_hotkey_string(self.hotkey_str)
        except Exception as e:
            self._reg_error = str(e)
            self._init_event.set()
            return

        # Attempt registration with MOD_NOREPEAT first, then fallback without it
        success = False
        try:
            success = bool(user32.RegisterHotKey(None, self.hotkey_id, modifiers | MOD_NOREPEAT, vk))
        except Exception:
            pass

        if not success:
            try:
                success = bool(user32.RegisterHotKey(None, self.hotkey_id, modifiers, vk))
            except Exception as e:
                self._reg_error = f"RegisterHotKey exception: {e}"

        if not success:
            err_code = kernel32.GetLastError()
            self._reg_error = f"Win32 error code {err_code} (key combination may already be reserved by another app)"
            self._registered = False
            self._init_event.set()
            return

        self._registered = True
        logger.info("Global hotkey successfully registered: '%s' (ID: %d)", self.hotkey_str, self.hotkey_id)
        self._init_event.set()

        msg = wintypes.MSG()
        while self._running:
            # GetMessage blocks until a window message arrives
            ret = user32.GetMessageW(ctypes.byref(msg), 0, 0, 0)
            if ret == 0 or ret == -1:  # WM_QUIT or error
                break

            if msg.message == WM_HOTKEY and msg.wParam == self.hotkey_id:
                logger.info("Global hotkey '%s' triggered!", self.hotkey_str)
                if self.callback:
                    try:
                        self.callback()
                    except Exception as cb_err:
                        logger.error("Error executing hotkey callback: %s", cb_err, exc_info=True)

            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        # Cleanup on thread exit
        try:
            if self._registered:
                user32.UnregisterHotKey(None, self.hotkey_id)
                self._registered = False
        except Exception:
            pass


class LinuxGlobalHotkey:
    """Global hotkey listener adapter for Linux desktops.

    Under GNOME Wayland, compositors prevent background applications from
    capturing global keystrokes for security reasons. This adapter manages
    lifecycle status and documents the GNOME shortcut binding.
    """

    def __init__(
        self,
        hotkey_str: str = "Ctrl+Alt+Space",
        callback: Optional[Callable[[], None]] = None,
        hotkey_id: int = 1001,
    ):
        self.hotkey_str = hotkey_str
        self.callback = callback
        self.hotkey_id = hotkey_id
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running

    def start(self) -> bool:
        self._running = True
        logger.info(
            "Linux Global Hotkey handler initialized for [%s]. "
            "Under GNOME Wayland, configure a system shortcut in GNOME Settings -> Keyboard -> "
            "Custom Shortcuts to run: python3 <path>/run_host_bridge.py --focus",
            self.hotkey_str,
        )
        return True

    def stop(self) -> None:
        self._running = False
        logger.info("Linux global hotkey listener stopped.")


def create_global_hotkey_listener(
    hotkey_str: str = "Ctrl+Alt+Space",
    callback: Optional[Callable[[], None]] = None,
    hotkey_id: int = 1001,
):
    """Instantiate the platform-appropriate global hotkey listener."""
    import sys
    if sys.platform.startswith("win"):
        return Win32GlobalHotkey(hotkey_str=hotkey_str, callback=callback, hotkey_id=hotkey_id)
    return LinuxGlobalHotkey(hotkey_str=hotkey_str, callback=callback, hotkey_id=hotkey_id)

