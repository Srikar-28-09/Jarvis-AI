"""
src/agent_tools/computer_control_tools.py

Safe, narrowly scoped tools for filesystem control (list_directory,
create_directory), application launching (open_application), URL opening
(open_url), and screenshot capture (take_screenshot).

Security boundaries:
- Filesystem ops confined to /workspace/jarvis (the Jarvis workspace mount)
- Path traversal (../) and symlink escapes rejected
- Application launches via fixed allowlist only
- URL schemes restricted to http/https
- Screenshot reports Docker limitation honestly
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any, Dict, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Jarvis workspace root — the ONLY directory these tools may touch.
# Maps to D:\JarvisWorkspace on the Windows host via Docker volume mount.
# ---------------------------------------------------------------------------
JARVIS_WORKSPACE = "/workspace/jarvis"

# Maximum content size for write operations (1 MB)
MAX_WRITE_BYTES = 1_048_576

# ---------------------------------------------------------------------------
# Application allowlist  (name -> description for structured output)
# Since Odysseus runs in Docker, it cannot directly launch Windows GUI apps.
# These entries document the *intent*; actual launch goes through the host
# bridge (ntfy / webhook) or returns a clear "Docker limitation" message.
# ---------------------------------------------------------------------------
_APPLICATION_ALLOWLIST: Dict[str, Dict[str, str]] = {
    "vscode":           {"name": "VS Code",          "command": "code"},
    "vs code":          {"name": "VS Code",          "command": "code"},
    "visual studio code": {"name": "VS Code",        "command": "code"},
    "code":             {"name": "VS Code",          "command": "code"},
    "chrome":           {"name": "Google Chrome",    "command": "chrome"},
    "google chrome":    {"name": "Google Chrome",    "command": "chrome"},
    "terminal":         {"name": "Windows Terminal", "command": "wt"},
    "windows terminal": {"name": "Windows Terminal", "command": "wt"},
    "wt":               {"name": "Windows Terminal", "command": "wt"},
    "explorer":         {"name": "File Explorer",    "command": "explorer"},
    "file explorer":    {"name": "File Explorer",    "command": "explorer"},
    "notepad":          {"name": "Notepad",          "command": "notepad"},
    "calculator":       {"name": "Calculator",       "command": "calc"},
    "calc":             {"name": "Calculator",       "command": "calc"},
    "firefox":          {"name": "Firefox",          "command": "firefox"},
    "edge":             {"name": "Microsoft Edge",   "command": "msedge"},
    "microsoft edge":   {"name": "Microsoft Edge",   "command": "msedge"},
}

# Safe URL schemes
_SAFE_URL_SCHEMES = frozenset({"http", "https"})

# Dangerous URL schemes to reject explicitly
_DANGEROUS_URL_SCHEMES = frozenset({
    "file", "javascript", "data", "vbscript", "powershell",
    "shell", "cmd", "ftp", "smb", "telnet", "ssh",
})


# ---------------------------------------------------------------------------
# Path security helpers
# ---------------------------------------------------------------------------
def _resolve_jarvis_path(raw_path: str) -> str:
    """Resolve and confine a path to the Jarvis workspace.

    Raises ValueError on:
    - Empty path
    - Path traversal attempts (../)
    - Symlink escape outside workspace
    - Paths outside /workspace/jarvis
    - Sensitive file patterns
    """
    if not raw_path or not str(raw_path).strip():
        raise ValueError("path is required")

    raw = str(raw_path).strip()

    # Reject obvious traversal attempts before any resolution
    normalized = raw.replace("\\", "/")
    if ".." in normalized.split("/"):
        raise ValueError(
            f"path '{raw_path}' contains path traversal (../), which is not allowed"
        )

    # Resolve relative paths under the workspace
    if os.path.isabs(raw):
        candidate = raw
    else:
        candidate = os.path.join(JARVIS_WORKSPACE, raw)

    # Resolve symlinks and normalize
    resolved = os.path.realpath(candidate)
    workspace_real = os.path.realpath(JARVIS_WORKSPACE)

    # Check containment
    resolved_norm = os.path.normcase(resolved)
    workspace_norm = os.path.normcase(workspace_real)

    if resolved_norm != workspace_norm:
        try:
            common = os.path.commonpath([resolved_norm, workspace_norm])
        except ValueError:
            raise ValueError(
                f"path '{raw_path}' is outside the Jarvis workspace ({JARVIS_WORKSPACE})"
            )
        if common != workspace_norm:
            raise ValueError(
                f"path '{raw_path}' is outside the Jarvis workspace ({JARVIS_WORKSPACE})"
            )

    # Check for sensitive patterns
    from src.tool_execution import _is_sensitive_path
    if _is_sensitive_path(resolved):
        raise ValueError(
            f"path '{raw_path}' targets a sensitive file or directory"
        )

    return resolved


# ---------------------------------------------------------------------------
# ListDirectoryTool
# ---------------------------------------------------------------------------
class ListDirectoryTool:
    """List contents of a directory inside the Jarvis workspace."""

    async def execute(self, content: str, ctx: Optional[dict] = None) -> dict:
        raw = (content or "").strip()
        path = ""

        if raw.startswith("{"):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    path = str(parsed.get("path") or "").strip()
            except (json.JSONDecodeError, TypeError):
                pass
        else:
            path = raw.split("\n", 1)[0].strip()

        # Default to workspace root
        if not path:
            path = JARVIS_WORKSPACE

        try:
            resolved = _resolve_jarvis_path(path)
        except ValueError as e:
            return {"error": f"list_directory: {e}", "exit_code": 1}

        def _ls():
            if not os.path.exists(resolved):
                return None, f"list_directory: '{path}' does not exist"
            if not os.path.isdir(resolved):
                return None, f"list_directory: '{path}' is not a directory"

            entries = []
            try:
                with os.scandir(resolved) as it:
                    for entry in it:
                        try:
                            is_dir = entry.is_dir(follow_symlinks=False)
                            stat = entry.stat(follow_symlinks=False)
                            size = stat.st_size if not is_dir else 0
                            entries.append({
                                "name": entry.name,
                                "type": "directory" if is_dir else "file",
                                "size": size,
                            })
                        except OSError:
                            continue
            except (PermissionError, OSError) as e:
                return None, f"list_directory: {e}"

            # Sort: directories first, then files, alphabetical
            entries.sort(key=lambda e: (e["type"] != "directory", e["name"].lower()))

            lines = [f"{resolved}:"]
            for entry in entries:
                if entry["type"] == "directory":
                    lines.append(f"  {entry['name']}/")
                else:
                    lines.append(f"  {entry['name']}  ({entry['size']} B)")

            if not entries:
                lines.append("  (empty)")

            return "\n".join(lines), None

        out, err = await asyncio.to_thread(_ls)
        if err:
            return {"error": err, "exit_code": 1}

        return {
            "output": out,
            "exit_code": 0,
            "resolved_path": resolved,
            "entry_count": out.count("\n"),
        }


# ---------------------------------------------------------------------------
# CreateDirectoryTool
# ---------------------------------------------------------------------------
class CreateDirectoryTool:
    """Create a directory inside the Jarvis workspace."""

    async def execute(self, content: str, ctx: Optional[dict] = None) -> dict:
        raw = (content or "").strip()
        path = ""

        if raw.startswith("{"):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    path = str(parsed.get("path") or "").strip()
            except (json.JSONDecodeError, TypeError):
                pass
        else:
            path = raw.split("\n", 1)[0].strip()

        if not path:
            return {"error": "create_directory: path is required", "exit_code": 1}

        try:
            resolved = _resolve_jarvis_path(path)
        except ValueError as e:
            return {"error": f"create_directory: {e}", "exit_code": 1}

        def _mkdir():
            if os.path.exists(resolved):
                if os.path.isdir(resolved):
                    return f"Directory already exists: {resolved}", "exists"
                else:
                    return None, f"create_directory: '{path}' exists but is not a directory"
            os.makedirs(resolved, exist_ok=True)
            return f"Created directory: {resolved}", "created"

        try:
            msg, status = await asyncio.to_thread(_mkdir)
        except PermissionError:
            return {"error": f"create_directory: permission denied for '{path}'", "exit_code": 1}
        except OSError as e:
            return {"error": f"create_directory: {e}", "exit_code": 1}

        if status == "exists":
            return {"output": msg, "exit_code": 0, "resolved_path": resolved, "created": False}
        elif msg is None:
            return {"error": status, "exit_code": 1}

        return {"output": msg, "exit_code": 0, "resolved_path": resolved, "created": True}


# ---------------------------------------------------------------------------
# OpenApplicationTool
# ---------------------------------------------------------------------------
class OpenApplicationTool:
    """Open a Windows application from a fixed allowlist via the Windows Host Bridge.

    When the Host Bridge is running on the Windows host, the application is launched
    directly on the user's desktop. When unavailable, reports status gracefully.
    """

    async def execute(self, content: str, ctx: Optional[dict] = None) -> dict:
        raw = (content or "").strip()
        app_name = ""

        if raw.startswith("{"):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    app_name = str(parsed.get("app_name") or parsed.get("application") or parsed.get("app") or parsed.get("name") or "").strip()
            except (json.JSONDecodeError, TypeError):
                pass
        else:
            app_name = raw.split("\n", 1)[0].strip()

        if not app_name:
            return {"error": "open_application: application name is required", "exit_code": 1}

        # Normalize and look up in client allowlist
        key = app_name.lower().strip()
        if key.endswith(".exe"):
            key = key[:-4].strip()
        entry = _APPLICATION_ALLOWLIST.get(key)
        if not entry:
            entry = _APPLICATION_ALLOWLIST.get(app_name.lower().strip())

        if not entry:
            allowed = sorted(set(v["name"] for v in _APPLICATION_ALLOWLIST.values()))
            return {
                "error": (
                    f"open_application: '{app_name}' is not in the allowed application list. "
                    f"Allowed applications: {', '.join(allowed)}. "
                    f"Arbitrary executable paths are not permitted for security."
                ),
                "exit_code": 1,
            }

        # Attempt execution via Host Bridge
        from src.host_bridge_client import execute_host_action
        bridge_res = await execute_host_action("open_application", {"application": entry["name"]})

        if bridge_res.get("success"):
            output_msg = bridge_res.get("message") or f"Successfully launched {entry['name']} on your Windows desktop."
            return {
                "success": bridge_res.get("success", True),
                "output": output_msg,
                "exit_code": 0,
                "application": entry["name"],
                "command": entry["command"],
                "already_running": bridge_res.get("already_running", False),
                "focused": bridge_res.get("focused", False),
                "hwnd": bridge_res.get("hwnd"),
                "pid": bridge_res.get("pid"),
                "title": bridge_res.get("title"),
                "execution_status_uncertain": bridge_res.get("execution_status_uncertain", False),
            }

        # Bridge returned a specific error or was unavailable
        if bridge_res.get("bridge_available") is False:
            return {
                "error": (
                    f"open_application: Windows host bridge unavailable. "
                    f"Start 'python run_host_bridge.py' on the Windows host to enable desktop control."
                ),
                "exit_code": 1,
                "bridge_available": False,
                "execution_status_uncertain": bridge_res.get("execution_status_uncertain", False),
            }

        return {
            "error": f"open_application: {bridge_res.get('error', 'Failed to launch application')}",
            "exit_code": 1,
            "execution_status_uncertain": bridge_res.get("execution_status_uncertain", False),
        }


# ---------------------------------------------------------------------------
# OpenUrlTool
# ---------------------------------------------------------------------------
class OpenUrlTool:
    """Open a URL in the user's default browser via the Windows Host Bridge.

    Only http:// and https:// URLs are allowed. Dangerous schemes
    (file://, javascript:, data:, etc.) are rejected.
    """

    async def execute(self, content: str, ctx: Optional[dict] = None) -> dict:
        raw = (content or "").strip()
        url = ""

        if raw.startswith("{"):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    url = str(parsed.get("url") or "").strip()
            except (json.JSONDecodeError, TypeError):
                pass
        else:
            url = raw.split("\n", 1)[0].strip()

        if not url:
            return {"error": "open_url: URL is required", "exit_code": 1}

        # If no scheme provided, prefix https://
        if "://" not in url and not any(url.lower().startswith(s + ":") for s in (_SAFE_URL_SCHEMES | _DANGEROUS_URL_SCHEMES)):
            url = "https://" + url

        # Parse and validate scheme
        try:
            parsed_url = urlparse(url)
        except Exception:
            return {"error": f"open_url: invalid URL '{url}'", "exit_code": 1}

        scheme = (parsed_url.scheme or "").lower()

        if scheme in _DANGEROUS_URL_SCHEMES:
            return {
                "error": (
                    f"open_url: scheme '{scheme}://' is not allowed for security reasons. "
                    f"Only http:// and https:// URLs are permitted."
                ),
                "exit_code": 1,
            }

        if scheme not in _SAFE_URL_SCHEMES:
            return {
                "error": (
                    f"open_url: unsupported URL scheme '{scheme}://'. "
                    f"Only http:// and https:// are allowed."
                ),
                "exit_code": 1,
            }

        if not parsed_url.netloc:
            return {"error": f"open_url: URL '{url}' has no hostname", "exit_code": 1}

        # Attempt opening via Host Bridge
        from src.host_bridge_client import execute_host_action
        bridge_res = await execute_host_action("open_url", {"url": url})

        if bridge_res.get("success"):
            return {
                "success": bridge_res.get("success", True),
                "output": f"Successfully opened {url} in your Windows browser.",
                "exit_code": 0,
                "url": url,
                "scheme": scheme,
                "host": parsed_url.netloc,
                "focused": bridge_res.get("focused"),
                "hwnd": bridge_res.get("hwnd"),
                "pid": bridge_res.get("pid"),
                "execution_status_uncertain": bridge_res.get("execution_status_uncertain", False),
            }

        if bridge_res.get("bridge_available") is False:
            return {
                "error": (
                    f"open_url: Windows host bridge unavailable. "
                    f"Start 'python run_host_bridge.py' on the Windows host to enable opening URLs in host browser."
                ),
                "exit_code": 1,
                "bridge_available": False,
                "execution_status_uncertain": bridge_res.get("execution_status_uncertain", False),
            }

        return {
            "error": f"open_url: {bridge_res.get('error', 'Failed to open URL')}",
            "exit_code": 1,
            "execution_status_uncertain": bridge_res.get("execution_status_uncertain", False),
        }


# ---------------------------------------------------------------------------
# TakeScreenshotTool
# ---------------------------------------------------------------------------
class TakeScreenshotTool:
    """Capture a real screenshot of the desktop via the Host Bridge.

    Retrieves actual screen dimensions and image file saved in the workspace,
    without invoking a vision model. The caller owns any requested analysis.
    """

    async def execute(self, content: str, ctx: Optional[dict] = None) -> dict:
        import time
        from src.host_bridge_client import execute_host_action
        t0 = time.perf_counter()
        bridge_res = await execute_host_action("screenshot", {})
        bridge_elapsed = time.perf_counter() - t0

        if bridge_res.get("success"):
            file_path = bridge_res.get("file_path", "")
            width = bridge_res.get("width", 0)
            height = bridge_res.get("height", 0)
            size_bytes = bridge_res.get("size_bytes", 0)
            image_b64 = bridge_res.get("image_base64", "")

            if not image_b64:
                return {"error": "take_screenshot: Host Bridge returned no screenshot image data", "exit_code": 1}

            msg = f"Captured desktop screenshot ({width}x{height}, {size_bytes} bytes)."
            if file_path:
                msg += f" Saved to: {file_path}"

            return {
                "output": msg,
                "exit_code": 0,
                "file_path": file_path,
                "width": width,
                "height": height,
                "format": bridge_res.get("format", "jpeg"),
                "size_bytes": size_bytes,
                "image_base64": image_b64,
                "image_bytes": len(image_b64) * 3 // 4,
                "host_capture_seconds": bridge_res.get("capture_seconds"),
                "jpeg_encode_seconds": bridge_res.get("jpeg_encode_seconds"),
                "base64_encode_seconds": bridge_res.get("base64_encode_seconds"),
                "bridge_round_trip_seconds": round(bridge_elapsed, 3),
            }

        if bridge_res.get("bridge_available") is False:
            err_msg = bridge_res.get("error") or (
                "take_screenshot: Windows host bridge unavailable. "
                "Start 'python run_host_bridge.py' on the host to enable desktop screenshots."
            )
            return {
                "error": err_msg,
                "exit_code": 1,
                "reason": "host_bridge_unavailable",
                "available": False,
            }

        return {
            "error": f"take_screenshot error: {bridge_res.get('error', 'Failed to capture screenshot')}",
            "exit_code": 1,
            "available": False,
        }


# ---------------------------------------------------------------------------
# ClickTool
# ---------------------------------------------------------------------------
class ClickTool:
    """Simulate a mouse click at screen coordinates (x, y) via Windows Host Bridge."""

    async def execute(self, content: str, ctx: Optional[dict] = None) -> dict:
        raw = (content or "").strip()
        x: Optional[int] = None
        y: Optional[int] = None

        if raw.startswith("{"):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    x = parsed.get("x")
                    y = parsed.get("y")
            except (json.JSONDecodeError, TypeError):
                pass
        else:
            # Handle "x, y" or "x y"
            parts = re.split(r"[\s,]+", raw)
            if len(parts) >= 2:
                try:
                    x = int(parts[0])
                    y = int(parts[1])
                except ValueError:
                    pass

        if x is None or y is None:
            return {
                "error": "click: coordinates 'x' and 'y' are required (e.g. {\"x\": 500, \"y\": 300})",
                "exit_code": 1,
            }

        from src.host_bridge_client import execute_host_action
        bridge_res = await execute_host_action("click", {"x": x, "y": y})

        if bridge_res.get("success"):
            return {
                "output": bridge_res.get("message", f"Clicked at ({x}, {y})"),
                "exit_code": 0,
                "x": x,
                "y": y,
                "action": "click",
            }

        if bridge_res.get("bridge_available") is False:
            return {
                "error": (
                    "click: Windows host bridge unavailable. "
                    "Start 'python run_host_bridge.py' on the Windows host to enable desktop control."
                ),
                "exit_code": 1,
                "bridge_available": False,
            }

        return {
            "error": f"click error: {bridge_res.get('error', 'Failed to execute click')}",
            "exit_code": 1,
        }


# ---------------------------------------------------------------------------
# DoubleClickTool
# ---------------------------------------------------------------------------
class DoubleClickTool:
    """Simulate a mouse double-click at screen coordinates (x, y) via Windows Host Bridge."""

    async def execute(self, content: str, ctx: Optional[dict] = None) -> dict:
        raw = (content or "").strip()
        x: Optional[int] = None
        y: Optional[int] = None

        if raw.startswith("{"):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    x = parsed.get("x")
                    y = parsed.get("y")
            except (json.JSONDecodeError, TypeError):
                pass
        else:
            parts = re.split(r"[\s,]+", raw)
            if len(parts) >= 2:
                try:
                    x = int(parts[0])
                    y = int(parts[1])
                except ValueError:
                    pass

        if x is None or y is None:
            return {
                "error": "double_click: coordinates 'x' and 'y' are required (e.g. {\"x\": 500, \"y\": 300})",
                "exit_code": 1,
            }

        from src.host_bridge_client import execute_host_action
        bridge_res = await execute_host_action("double_click", {"x": x, "y": y})

        if bridge_res.get("success"):
            return {
                "output": bridge_res.get("message", f"Double-clicked at ({x}, {y})"),
                "exit_code": 0,
                "x": x,
                "y": y,
                "action": "double_click",
            }

        if bridge_res.get("bridge_available") is False:
            return {
                "error": (
                    "double_click: Windows host bridge unavailable. "
                    "Start 'python run_host_bridge.py' on the Windows host to enable desktop control."
                ),
                "exit_code": 1,
                "bridge_available": False,
            }

        return {
            "error": f"double_click error: {bridge_res.get('error', 'Failed to execute double click')}",
            "exit_code": 1,
        }


# ---------------------------------------------------------------------------
# TypeTextTool
# ---------------------------------------------------------------------------
class TypeTextTool:
    """Type text into the focused window/element via Windows Host Bridge."""

    async def execute(self, content: str, ctx: Optional[dict] = None) -> dict:
        raw = (content or "").strip()
        text = ""

        if raw.startswith("{"):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    text = str(parsed.get("text") or "")
            except (json.JSONDecodeError, TypeError):
                pass
        else:
            text = content

        if not text:
            return {"error": "type_text: 'text' parameter is required", "exit_code": 1}

        params = {"text": text}
        if isinstance(parsed, dict):
            for k in ("application", "app_name", "target_app", "hwnd"):
                if parsed.get(k):
                    params[k] = parsed[k]

        from src.host_bridge_client import execute_host_action
        bridge_res = await execute_host_action("type_text", params)

        if bridge_res.get("success"):
            return {
                "output": bridge_res.get("message", f"Successfully typed {len(text)} characters"),
                "exit_code": 0,
                "length": len(text),
                "action": "type_text",
                "dispatched": bridge_res.get("dispatched", len(text)),
                "delivered": bridge_res.get("delivered", True),
                "delivery_verified": bridge_res.get("delivery_verified", False),
                "delivery_method": bridge_res.get("delivery_method"),
                "focused": bridge_res.get("focused", True),
                "keyboard_focus_established": bridge_res.get("keyboard_focus_established", True),
                "target_hwnd": bridge_res.get("target_hwnd"),
                "hwnd": bridge_res.get("hwnd"),
                "foreground_title": bridge_res.get("foreground_title"),
                "foreground_hwnd": bridge_res.get("foreground_hwnd"),
                "foreground_pid": bridge_res.get("foreground_pid"),
                "focus_hwnd": bridge_res.get("focus_hwnd"),
                "caret_hwnd": bridge_res.get("caret_hwnd"),
                "control": bridge_res.get("control"),
                "control_hwnd": bridge_res.get("control_hwnd"),
            }

        if bridge_res.get("bridge_available") is False:
            return {
                "error": (
                    "type_text: Windows host bridge unavailable. "
                    "Start 'python run_host_bridge.py' on the Windows host to enable desktop typing."
                ),
                "exit_code": 1,
                "bridge_available": False,
                "focused": False,
                "keyboard_focus_established": False,
            }

        return {
            "error": f"type_text error: {bridge_res.get('error', 'Failed to type text')}",
            "exit_code": 1,
            "dispatched": bridge_res.get("dispatched", 0),
            "delivered": bridge_res.get("delivered", False),
            "delivery_verified": bridge_res.get("delivery_verified", False),
            "focused": bridge_res.get("focused", False),
            "keyboard_focus_established": bridge_res.get("keyboard_focus_established", False),
            "target_hwnd": bridge_res.get("target_hwnd"),
            "hwnd": bridge_res.get("hwnd"),
            "foreground_hwnd": bridge_res.get("foreground_hwnd"),
            "foreground_pid": bridge_res.get("foreground_pid"),
            "focus_hwnd": bridge_res.get("focus_hwnd"),
            "caret_hwnd": bridge_res.get("caret_hwnd"),
            "control": bridge_res.get("control"),
            "control_hwnd": bridge_res.get("control_hwnd"),
        }


# ---------------------------------------------------------------------------
# PressKeyTool
# ---------------------------------------------------------------------------
class PressKeyTool:
    """Press a special keyboard key from the allowlist via Windows Host Bridge."""

    async def execute(self, content: str, ctx: Optional[dict] = None) -> dict:
        raw = (content or "").strip()
        key = ""

        if raw.startswith("{"):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    key = str(parsed.get("key") or "").strip()
            except (json.JSONDecodeError, TypeError):
                pass
        else:
            key = raw.split("\n", 1)[0].strip()

        if not key:
            return {"error": "press_key: 'key' parameter is required (e.g. {\"key\": \"ENTER\"})", "exit_code": 1}

        from src.host_bridge_client import execute_host_action
        bridge_res = await execute_host_action("press_key", {"key": key})

        if bridge_res.get("success"):
            return {
                "output": bridge_res.get("message", f"Pressed key '{key}'"),
                "exit_code": 0,
                "key": bridge_res.get("key", key),
                "action": "press_key",
            }

        if bridge_res.get("bridge_available") is False:
            return {
                "error": (
                    "press_key: Windows host bridge unavailable. "
                    "Start 'python run_host_bridge.py' on the Windows host to enable desktop key press."
                ),
                "exit_code": 1,
                "bridge_available": False,
            }

        return {
            "error": f"press_key error: {bridge_res.get('error', 'Failed to press key')}",
            "exit_code": 1,
        }


# ---------------------------------------------------------------------------
# ScrollTool
# ---------------------------------------------------------------------------
class ScrollTool:
    """Scroll mouse wheel up or down via Windows Host Bridge."""

    async def execute(self, content: str, ctx: Optional[dict] = None) -> dict:
        raw = (content or "").strip()
        direction = "down"
        amount = 5

        if raw.startswith("{"):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    direction = str(parsed.get("direction") or "down").strip().lower()
                    if "amount" in parsed:
                        amount = int(parsed["amount"])
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
        else:
            parts = raw.split()
            if len(parts) >= 1 and parts[0].lower() in ("up", "down"):
                direction = parts[0].lower()
            if len(parts) >= 2:
                try:
                    amount = int(parts[1])
                except ValueError:
                    pass

        from src.host_bridge_client import execute_host_action
        bridge_res = await execute_host_action("scroll", {"direction": direction, "amount": amount})

        if bridge_res.get("success"):
            return {
                "output": bridge_res.get("message", f"Scrolled {direction} by {amount} steps"),
                "exit_code": 0,
                "direction": direction,
                "amount": amount,
                "action": "scroll",
            }

        if bridge_res.get("bridge_available") is False:
            return {
                "error": (
                    "scroll: Windows host bridge unavailable. "
                    "Start 'python run_host_bridge.py' on the Windows host to enable desktop scrolling."
                ),
                "exit_code": 1,
                "bridge_available": False,
            }

        return {
            "error": f"scroll error: {bridge_res.get('error', 'Failed to scroll')}",
            "exit_code": 1,
        }


# ---------------------------------------------------------------------------
# ComputerUseTool (High-level closed-loop GUI agent tool)
# ---------------------------------------------------------------------------
_TOOL_COMPUTER_USE_LOCK = asyncio.Lock()


class ComputerUseTool:
    """Execute a high-level user goal using the safe closed-loop ComputerUseController."""

    async def execute(self, content: str, ctx: Optional[dict] = None) -> dict:
        raw = (content or "").strip()
        goal = ""

        if raw.startswith("{"):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    goal = str(parsed.get("goal") or parsed.get("task") or parsed.get("instruction") or "").strip()
            except (json.JSONDecodeError, TypeError):
                goal = ""
        else:
            goal = raw

        if not goal:
            err_msg = "computer_use: Goal cannot be empty. Please specify what desktop task to accomplish."
            return {
                "status": "failed",
                "error": err_msg,
                "output": err_msg,
                "exit_code": 1,
            }

        progress_cb = ctx.get("progress_cb") if isinstance(ctx, dict) else None
        plan_mode = bool(ctx.get("plan_mode")) if isinstance(ctx, dict) else False

        from src.computer_use_controller import (
            ComputerUseConfig,
            ComputerUseController,
            is_computer_use_enabled,
        )

        if not is_computer_use_enabled():
            err_msg = "computer_use is disabled via the COMPUTER_USE_ENABLED configuration/flag."
            return {
                "status": "failed",
                "error": err_msg,
                "output": err_msg,
                "exit_code": 1,
            }

        if _TOOL_COMPUTER_USE_LOCK.locked():
            err_msg = "Computer Use is already running in another active task. Please wait for it to complete."
            return {
                "status": "failed",
                "error": err_msg,
                "output": err_msg,
                "exit_code": 1,
            }

        cfg = ComputerUseConfig(plan_mode=plan_mode)
        request_security_context = ctx.get("request_security_context") if isinstance(ctx, dict) else None
        controller = ComputerUseController(
            config=cfg,
            progress_callback=progress_cb,
            navigation_guard=request_security_context,
        )

        async with _TOOL_COMPUTER_USE_LOCK:
            try:
                state = await controller.run(goal=goal)
            except Exception as e:
                logger.exception("ComputerUseController unhandled exception: %s", e)
                return {
                    "status": "failed",
                    "error": f"Computer use execution failed: {e}",
                    "output": f"Computer use execution failed: {e}",
                    "exit_code": 1,
                }

        # Check if awaiting confirmation
        if state.status == "AWAITING_CONFIRMATION":
            pending = state.pending_confirmation or {}
            action = pending.get("action", "action")
            params = pending.get("params", {})
            reason = pending.get("reason", "Sensitive action detected")

            question = (
                f"Jarvis requires your confirmation to perform a sensitive computer operation.\n"
                f"Action: `{action}`\n"
                f"Details: {json.dumps(params)}\n"
                f"Reason: {reason}\n"
                f"Do you approve this action?"
            )
            options = [
                {"label": "Approve", "description": f"Allow Jarvis to execute '{action}'"},
                {"label": "Reject", "description": "Cancel this operation and do not execute"},
            ]
            return {
                "status": "awaiting_confirmation",
                "ask_user": {
                    "question": question,
                    "options": options,
                    "multi": False,
                },
                "pending_confirmation": pending,
                "summary": f"Awaiting confirmation for sensitive operation: {reason}",
                "output": f"Awaiting confirmation for sensitive operation: {reason}",
                "actions_executed": state.action_count,
                "iterations": state.iteration,
                "metrics": state.metrics,
                "exit_code": 0,
            }

        if state.status == "COMPLETED":
            summary = state.summary or f"Successfully completed goal: {goal}"
            return {
                "status": "completed",
                "summary": summary,
                "output": summary,
                "actions_executed": state.action_count,
                "iterations": state.iteration,
                "metrics": state.metrics,
                "exit_code": 0,
            }

        status_map = {
            "STOPPED_BY_BUDGET": "budget_exhausted",
            "STOPPED_BY_TIMEOUT": "timeout",
            "VERIFICATION_FAILED": "verification_failed",
            "STOPPED_BY_PLAN_MODE": "plan_mode_blocked",
            "SENSITIVE_ACTION_BLOCKED": "sensitive_action_blocked",
            "FAILED": "failed",
        }
        mapped_status = status_map.get(state.status, "failed")
        err_text = state.error or f"Computer use ended with status '{state.status}'"
        return {
            "status": mapped_status,
            "error": err_text,
            "output": f"Computer use {mapped_status}: {err_text}",
            "actions_executed": state.action_count,
            "iterations": state.iteration,
            "metrics": state.metrics,
            "exit_code": 1,
        }


