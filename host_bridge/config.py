"""
host_bridge/config.py

Configuration and security defaults for the Jarvis Host Bridge (Linux and Windows).
"""

import os
from pathlib import Path
import secrets
import sys

# Platform detection
IS_WINDOWS = sys.platform.startswith("win")
IS_LINUX = sys.platform.startswith("linux")

# Network Configuration - Bind to localhost by default
HOST_BRIDGE_BIND = os.environ.get("HOST_BRIDGE_BIND", "127.0.0.1")
HOST_BRIDGE_PORT = int(os.environ.get("HOST_BRIDGE_PORT", "8096"))

# Global Hotkey Configuration
JARVIS_HOTKEY = os.environ.get("JARVIS_HOTKEY", "Ctrl+Alt+Space")
JARVIS_UI_URL = os.environ.get("JARVIS_UI_URL", "http://localhost:7000")
JARVIS_HOTKEY_ENABLED = os.environ.get("JARVIS_HOTKEY_ENABLED", "true").strip().lower() in ("true", "1", "yes", "on", "enabled")

# Workspace Path on Host (mounted inside Docker as /workspace/jarvis)
_DEFAULT_WORKSPACE = r"D:\JarvisWorkspace" if IS_WINDOWS else str(Path.home() / "Projects" / "JarvisWorkspace")
HOST_WORKSPACE_DIR = os.environ.get("HOST_WORKSPACE_DIR", os.environ.get("JARVIS_WORKSPACE_DIR", _DEFAULT_WORKSPACE))
TOKEN_FILENAME = ".host_bridge_token"

# Request Limits
MAX_REQUEST_BYTES = 1_048_576  # 1 MB
REQUEST_TIMEOUT_SECONDS = 15.0

# Fixed Application Allowlist on Windows Host
WINDOWS_APPLICATION_ALLOWLIST = {
    "vscode": {
        "name": "VS Code",
        "command": "code",
        "args": [],
    },
    "vs code": {
        "name": "VS Code",
        "command": "code",
        "args": [],
    },
    "visual studio code": {
        "name": "VS Code",
        "command": "code",
        "args": [],
    },
    "code": {
        "name": "VS Code",
        "command": "code",
        "args": [],
    },
    "chrome": {
        "name": "Google Chrome",
        "command": "chrome",
        "process_name": "chrome.exe",
        "window_titles": ["google chrome", "chrome"],
        "single_instance": True,
        "args": [],
    },
    "google chrome": {
        "name": "Google Chrome",
        "command": "chrome",
        "process_name": "chrome.exe",
        "window_titles": ["google chrome", "chrome"],
        "single_instance": True,
        "args": [],
    },
    "firefox": {
        "name": "Mozilla Firefox",
        "command": "firefox",
        "args": [],
    },
    "edge": {
        "name": "Microsoft Edge",
        "command": "msedge",
        "args": [],
    },
    "microsoft edge": {
        "name": "Microsoft Edge",
        "command": "msedge",
        "args": [],
    },
    "windows_terminal": {
        "name": "Windows Terminal",
        "command": "wt",
        "args": [],
    },
    "terminal": {
        "name": "Windows Terminal",
        "command": "wt",
        "args": [],
    },
    "wt": {
        "name": "Windows Terminal",
        "command": "wt",
        "args": [],
    },
    "explorer": {
        "name": "File Explorer",
        "command": "explorer",
        "args": [],
    },
    "file explorer": {
        "name": "File Explorer",
        "command": "explorer",
        "args": [],
    },
    "notepad": {
        "name": "Notepad",
        "command": "notepad",
        "process_name": "notepad.exe",
        "window_titles": ["notepad", "untitled - notepad"],
        "single_instance": True,
        "args": [],
    },
    "calculator": {
        "name": "Calculator",
        "command": "calc",
        "process_name": "calc.exe",
        "window_titles": ["calculator"],
        "single_instance": True,
        "args": [],
    },
    "calc": {
        "name": "Calculator",
        "command": "calc",
        "process_name": "calc.exe",
        "window_titles": ["calculator"],
        "single_instance": True,
        "args": [],
    },
}

# Fixed Application Allowlist on Linux Host (Ubuntu GNOME)
LINUX_APPLICATION_ALLOWLIST = {
    "vscode": {
        "name": "VS Code",
        "command": "code",
        "process_name": "code",
        "window_titles": ["visual studio code", "code"],
        "args": [],
    },
    "vs code": {
        "name": "VS Code",
        "command": "code",
        "process_name": "code",
        "window_titles": ["visual studio code", "code"],
        "args": [],
    },
    "visual studio code": {
        "name": "VS Code",
        "command": "code",
        "process_name": "code",
        "window_titles": ["visual studio code", "code"],
        "args": [],
    },
    "code": {
        "name": "VS Code",
        "command": "code",
        "process_name": "code",
        "window_titles": ["visual studio code", "code"],
        "args": [],
    },
    "firefox": {
        "name": "Mozilla Firefox",
        "command": "firefox",
        "process_name": "firefox",
        "window_titles": ["mozilla firefox", "firefox"],
        "args": [],
    },
    "browser": {
        "name": "Mozilla Firefox",
        "command": "firefox",
        "process_name": "firefox",
        "window_titles": ["mozilla firefox", "firefox"],
        "args": [],
    },
    "chrome": {
        "name": "Google Chrome",
        "command": "google-chrome",
        "process_name": "chrome",
        "window_titles": ["google chrome", "chrome"],
        "single_instance": True,
        "args": [],
    },
    "google chrome": {
        "name": "Google Chrome",
        "command": "google-chrome",
        "process_name": "chrome",
        "window_titles": ["google chrome", "chrome"],
        "single_instance": True,
        "args": [],
    },
    "terminal": {
        "name": "Terminal",
        "command": "x-terminal-emulator",
        "process_name": "ptyxis",
        "window_titles": ["terminal", "ptyxis"],
        "args": [],
    },
    "ptyxis": {
        "name": "Terminal",
        "command": "ptyxis",
        "process_name": "ptyxis",
        "window_titles": ["terminal", "ptyxis"],
        "args": [],
    },
    "gnome-terminal": {
        "name": "Terminal",
        "command": "x-terminal-emulator",
        "process_name": "ptyxis",
        "window_titles": ["terminal", "ptyxis"],
        "args": [],
    },
    "nautilus": {
        "name": "File Manager",
        "command": "nautilus",
        "process_name": "nautilus",
        "window_titles": ["files", "nautilus"],
        "args": [],
    },
    "files": {
        "name": "File Manager",
        "command": "nautilus",
        "process_name": "nautilus",
        "window_titles": ["files", "nautilus"],
        "args": [],
    },
    "file manager": {
        "name": "File Manager",
        "command": "nautilus",
        "process_name": "nautilus",
        "window_titles": ["files", "nautilus"],
        "args": [],
    },
    "explorer": {
        "name": "File Manager",
        "command": "nautilus",
        "process_name": "nautilus",
        "window_titles": ["files", "nautilus"],
        "args": [],
    },
    "notepad": {
        "name": "Notepad",
        "command": "gnome-text-editor",
        "process_name": "gnome-text-editor",
        "window_titles": ["text editor", "gedit", "notepad"],
        "single_instance": True,
        "args": [],
    },
    "text editor": {
        "name": "Text Editor",
        "command": "gnome-text-editor",
        "process_name": "gnome-text-editor",
        "window_titles": ["text editor", "gedit"],
        "single_instance": True,
        "args": [],
    },
    "calculator": {
        "name": "Calculator",
        "command": "gnome-calculator",
        "process_name": "gnome-calculator",
        "window_titles": ["calculator"],
        "single_instance": True,
        "args": [],
    },
    "calc": {
        "name": "Calculator",
        "command": "gnome-calculator",
        "process_name": "gnome-calculator",
        "window_titles": ["calculator"],
        "single_instance": True,
        "args": [],
    },
}

APPLICATION_ALLOWLIST = WINDOWS_APPLICATION_ALLOWLIST if IS_WINDOWS else LINUX_APPLICATION_ALLOWLIST

# Safe URL Schemes
SAFE_URL_SCHEMES = frozenset({"http", "https"})

# Explicitly Dangerous Schemes
DANGEROUS_URL_SCHEMES = frozenset({
    "file", "javascript", "data", "vbscript", "powershell",
    "shell", "cmd", "ftp", "smb", "telnet", "ssh", "ms-settings",
})


def get_or_create_bridge_token() -> str:
    """Retrieve or generate the shared host bridge authentication token.

    Priority:
    1. JARVIS_HOST_BRIDGE_TOKEN environment variable
    2. HOST_WORKSPACE_DIR / .host_bridge_token file (shared with Docker container)
    3. Auto-generated cryptographic random token written to token file
    """
    token = os.environ.get("JARVIS_HOST_BRIDGE_TOKEN", "").strip()
    if token:
        return token

    # Check shared workspace directory
    ws_path = Path(HOST_WORKSPACE_DIR)
    try:
        ws_path.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    if ws_path.exists() and ws_path.is_dir():
        token_file = ws_path / TOKEN_FILENAME
        try:
            if token_file.exists():
                existing = token_file.read_text(encoding="utf-8").strip()
                if existing:
                    return existing
            # Generate new token and write with restricted permissions (0600 on POSIX)
            new_token = secrets.token_hex(32)
            try:
                flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
                mode = 0o600
                fd = os.open(str(token_file), flags, mode)
                with open(fd, "w", encoding="utf-8") as f:
                    f.write(new_token)
            except Exception:
                token_file.write_text(new_token, encoding="utf-8")
            return new_token
        except Exception:
            pass

    # Fallback to in-memory random token
    return secrets.token_hex(32)

