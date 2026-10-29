"""
tests/test_host_bridge.py

Comprehensive tests for the Jarvis Secure Windows Host Bridge:
- Broker startup and localhost-only binding
- Authentication (valid token, invalid token, missing token)
- Disallowed shell/exec endpoints (reject /shell, /exec, /powershell, etc.)
- Action allowlist enforcement (open_application, open_url, screenshot)
- Arbitrary executable rejection
- URL scheme safety (allow http/https, reject file, javascript, data, powershell, etc.)
- Screenshot execution & response structure
- Malformed & oversized request handling
- Client library & tool integration with graceful fallback
"""

import asyncio
import json
import os
import secrets
import socket
import threading
import time
import urllib.error
import urllib.request
import pytest

from host_bridge.config import (
    APPLICATION_ALLOWLIST,
    DANGEROUS_URL_SCHEMES,
    HOST_BRIDGE_BIND,
    HOST_BRIDGE_PORT,
    MAX_REQUEST_BYTES,
    SAFE_URL_SCHEMES,
)
from host_bridge.server import create_host_bridge_server
from host_bridge.actions import (
    execute_open_application,
    execute_open_url,
    execute_screenshot,
)
from src.host_bridge_client import (
    check_host_bridge_health_sync,
    execute_host_action_sync,
    execute_host_action,
)
from src.agent_tools.computer_control_tools import (
    OpenApplicationTool,
    OpenUrlTool,
    TakeScreenshotTool,
)


@pytest.fixture(scope="module")
def bridge_test_server():
    """Spin up a live test instance of the Host Bridge on a random free port."""
    # Find a free ephemeral port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    test_token = f"test_token_{secrets.token_hex(16)}"
    server = create_host_bridge_server(
        bind_address="127.0.0.1",
        port=port,
        auth_token=test_token,
    )

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.1)

    server_info = {
        "port": port,
        "token": test_token,
        "url": f"http://127.0.0.1:{port}",
    }

    yield server_info

    server.shutdown()
    server.server_close()


# ===========================================================================
# 1. Localhost Binding & Server Security
# ===========================================================================

def test_localhost_only_binding_enforced():
    # Attempting to bind to 0.0.0.0 or LAN IP must raise ValueError
    with pytest.raises(ValueError, match="Security violation"):
        create_host_bridge_server(bind_address="0.0.0.0", port=8099, auth_token="tok")

    with pytest.raises(ValueError, match="Security violation"):
        create_host_bridge_server(bind_address="192.168.1.50", port=8099, auth_token="tok")


def test_no_shell_endpoints_exist(bridge_test_server):
    url = bridge_test_server["url"]
    token = bridge_test_server["token"]

    for bad_endpoint in ["/shell", "/exec", "/cmd", "/powershell", "/terminal", "/bash"]:
        req = urllib.request.Request(
            f"{url}{bad_endpoint}",
            data=b'{"command": "dir"}',
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {token}",
            },
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req)
        assert exc_info.value.code in (403, 404), f"Endpoint {bad_endpoint} should be 403/404"


# ===========================================================================
# 2. Authentication Tests
# ===========================================================================

def test_auth_success(bridge_test_server):
    url = bridge_test_server["url"]
    token = bridge_test_server["token"]

    req = urllib.request.Request(
        f"{url}/health",
        headers={"Authorization": f"Bearer {token}"},
        method="GET",
    )
    with urllib.request.urlopen(req) as resp:
        assert resp.status == 200
        data = json.loads(resp.read().decode())
        assert data["status"] == "healthy"
        assert "open_application" in data["allowed_actions"]


def test_auth_failure_missing_token(bridge_test_server):
    url = bridge_test_server["url"]
    req = urllib.request.Request(f"{url}/health", method="GET")
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req)
    assert exc_info.value.code == 401


def test_auth_failure_invalid_token(bridge_test_server):
    url = bridge_test_server["url"]
    req = urllib.request.Request(
        f"{url}/health",
        headers={"Authorization": "Bearer invalid_bad_token_123"},
        method="GET",
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req)
    assert exc_info.value.code == 401


# ===========================================================================
# 3. Action Allowlist & Execution Tests
# ===========================================================================

def test_open_application_allowlist_direct(monkeypatch):
    from unittest.mock import MagicMock
    mock_popen = MagicMock()
    monkeypatch.setattr("subprocess.Popen", mock_popen)
    monkeypatch.setattr("host_bridge.actions.is_application_running", lambda entry: (False, None))

    # Valid allowlisted app
    res = execute_open_application({"application": "notepad"})
    assert res["success"] is True
    assert res["application"] == "Notepad"
    assert res["already_running"] is False
    assert mock_popen.call_count == 1

    # Blocked unapproved binary / path
    res = execute_open_application({"application": "cmd.exe"})
    assert res["success"] is False
    assert "not allowed" in res["error"]

    res = execute_open_application({"application": "C:\\Windows\\System32\\cmd.exe"})
    assert res["success"] is False
    assert "not allowed" in res["error"]


def test_open_application_when_already_running_does_not_spawn(monkeypatch):
    from unittest.mock import MagicMock
    mock_popen = MagicMock()
    monkeypatch.setattr("subprocess.Popen", mock_popen)
    monkeypatch.setattr("host_bridge.actions.is_application_running", lambda entry: (True, "Notepad - Test"))
    monkeypatch.setattr("host_bridge.actions.focus_application_window", lambda entry: True)

    res = execute_open_application({"application": "notepad"})
    assert res["success"] is True
    assert res["application"] == "Notepad"
    assert res["already_running"] is True
    assert res["focused"] is True
    assert "already running" in res["message"]
    # Popen must NOT have been called!
    mock_popen.assert_not_called()


def test_open_application_when_not_running_spawns_once(monkeypatch):
    from unittest.mock import MagicMock
    mock_popen = MagicMock()
    monkeypatch.setattr("subprocess.Popen", mock_popen)
    monkeypatch.setattr("host_bridge.actions.is_application_running", lambda entry: (False, None))

    res = execute_open_application({"application": "notepad"})
    assert res["success"] is True
    assert res["application"] == "Notepad"
    assert res["already_running"] is False
    assert mock_popen.call_count == 1


def test_open_application_name_normalization(monkeypatch):
    from unittest.mock import MagicMock
    mock_popen = MagicMock()
    monkeypatch.setattr("subprocess.Popen", mock_popen)
    monkeypatch.setattr("host_bridge.actions.is_application_running", lambda entry: (False, None))

    for variant in ["notepad", "Notepad", "NOTEPAD", "notepad.exe", "Notepad.exe", "  notepad.exe  "]:
        res = execute_open_application({"application": variant})
        assert res["success"] is True
        assert res["application"] == "Notepad"

    for calc_var in ["calculator", "Calculator", "calc", "calc.exe", "CALC.EXE"]:
        res = execute_open_application({"application": calc_var})
        assert res["success"] is True
        assert res["application"] == "Calculator"


def test_open_url_safety_direct(monkeypatch):
    monkeypatch.setattr("host_bridge.actions.webbrowser.open", lambda url: True)
    # Safe URL
    res = execute_open_url({"url": "https://github.com"})
    assert res["success"] is True
    assert res["host"] == "github.com"

    # Dangerous URL rejected
    for dangerous in ["file:///etc/passwd", "javascript:alert(1)", "data:text/html,test", "powershell://calc"]:
        res = execute_open_url({"url": dangerous})
        assert res["success"] is False
        assert "rejected for security" in res["error"] or "Unsupported URL scheme" in res["error"]


def test_open_url_does_not_fallback_after_browser_reports_uncertain_failure(monkeypatch):
    monkeypatch.setattr("host_bridge.actions.webbrowser.open", lambda url: False)
    monkeypatch.setattr("host_bridge.actions.subprocess.Popen", lambda *args, **kwargs: pytest.fail("must not retry through xdg-open"))
    monkeypatch.setattr("host_bridge.actions.os.startfile", lambda url: pytest.fail("must not retry through startfile"), raising=False)

    result = execute_open_url({"url": "https://github.com"})

    assert result["success"] is False
    assert result["execution_status_uncertain"] is True
    assert "no fallback" in result["error"]


def test_screenshot_execution_direct():
    res = execute_screenshot({})
    assert res["success"] is True
    assert res["action"] == "screenshot"
    assert res["width"] > 0
    assert res["height"] > 0
    assert res["format"] == "jpeg"
    assert "image_base64" in res
    assert len(res["image_base64"]) > 0


# ===========================================================================
# 4. HTTP API & Protocol Edge Cases
# ===========================================================================

def test_http_action_unknown(bridge_test_server):
    url = bridge_test_server["url"]
    token = bridge_test_server["token"]

    payload = json.dumps({"action": "delete_all_files"}).encode()
    req = urllib.request.Request(
        f"{url}/v1/action",
        data=payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req)
    assert exc_info.value.code == 400
    err = json.loads(exc_info.value.read().decode())
    assert "Unknown action" in err["error"]


def test_http_action_malformed_json(bridge_test_server):
    url = bridge_test_server["url"]
    token = bridge_test_server["token"]

    req = urllib.request.Request(
        f"{url}/v1/action",
        data=b"not a valid json {{{{",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req)
    assert exc_info.value.code == 400


def test_http_action_oversized_payload(bridge_test_server):
    url = bridge_test_server["url"]
    token = bridge_test_server["token"]

    huge_payload = b"x" * (MAX_REQUEST_BYTES + 1024)
    req = urllib.request.Request(
        f"{url}/v1/action",
        data=huge_payload,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(req)
    assert exc_info.value.code == 413


# ===========================================================================
# 5. Client Library & Agent Tool Integration
# ===========================================================================

def test_tool_execution_with_bridge(monkeypatch, bridge_test_server):
    # Configure client to point to test server
    monkeypatch.setenv("JARVIS_HOST_BRIDGE_URL", bridge_test_server["url"])
    monkeypatch.setenv("JARVIS_HOST_BRIDGE_TOKEN", bridge_test_server["token"])

    async def _run():
        app_tool = OpenApplicationTool()
        url_tool = OpenUrlTool()
        ss_tool = TakeScreenshotTool()

        # 1. Open Application via Bridge
        res = await app_tool.execute("notepad")
        assert res["exit_code"] == 0
        assert "Successfully launched Notepad" in res["output"] or "already running" in res["output"]

        # 2. Open URL via Bridge
        res = await url_tool.execute("https://github.com/google")
        assert res["exit_code"] == 0
        assert "Successfully opened" in res["output"]

        # 3. Take Screenshot via Bridge
        res = await ss_tool.execute("")
        assert res["exit_code"] == 0
        assert "Captured desktop screenshot" in res["output"]
        assert res["width"] > 0
        assert res["height"] > 0

    asyncio.run(_run())


def test_tool_execution_bridge_unavailable_fallback(monkeypatch):
    # Point to non-existent port
    monkeypatch.setenv("JARVIS_HOST_BRIDGE_URL", "http://127.0.0.1:59999")
    monkeypatch.setenv("JARVIS_HOST_BRIDGE_TOKEN", "mock_tok")

    async def _run():
        app_tool = OpenApplicationTool()
        url_tool = OpenUrlTool()
        ss_tool = TakeScreenshotTool()

        # Graceful fallback reporting bridge unavailable
        res = await app_tool.execute("notepad")
        assert res["exit_code"] == 1
        assert "Windows host bridge unavailable" in res["error"]

        res = await url_tool.execute("https://github.com")
        assert res["exit_code"] == 1
        assert "Windows host bridge unavailable" in res["error"]

        res = await ss_tool.execute("")
        assert res["exit_code"] == 1
        assert "Windows host bridge unavailable" in res["error"]

    asyncio.run(_run())


# ===========================================================================
# 6. Linux Wayland Screenshot Capture, Validation, Fallback & Windows Parity
# ===========================================================================

def _make_sample_jpeg_bytes(width: int = 320, height: int = 200, color=(40, 90, 160)) -> tuple[bytes, str]:
    import base64
    import io
    from PIL import Image

    img = Image.new("RGB", (width, height), color=color)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    raw = buf.getvalue()
    b64 = base64.b64encode(raw).decode("ascii")
    return raw, b64


def test_linux_screenshot_capture_success(monkeypatch, tmp_path):
    from PIL import Image, ImageStat
    import base64
    import io
    import host_bridge.actions as actions

    monkeypatch.setattr(actions, "IS_WINDOWS", False)
    monkeypatch.setattr(actions, "HOST_WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "ubuntu:GNOME")

    raw_jpeg, b64_jpeg = _make_sample_jpeg_bytes(640, 360, color=(48, 96, 180))
    calls = []

    def fake_mutter():
        calls.append("mutter_screencast")
        return 640, 360, raw_jpeg, b64_jpeg

    def fake_mss():
        calls.append("mss")
        raise AssertionError("mss should not be called when primary Wayland capture succeeds")

    monkeypatch.setattr(actions, "_capture_with_mutter_screencast", fake_mutter)
    monkeypatch.setattr(actions, "_capture_with_mss", fake_mss)

    res = actions.execute_screenshot({})
    assert res["success"] is True
    assert res["action"] == "screenshot"
    assert res["width"] == 640
    assert res["height"] == 360
    assert res["format"] == "jpeg"
    assert res["size_bytes"] == len(raw_jpeg)
    assert res["image_base64"] == b64_jpeg
    assert calls == ["mutter_screencast"]

    decoded = Image.open(io.BytesIO(base64.b64decode(res["image_base64"])))
    stat = ImageStat.Stat(decoded)
    assert decoded.size == (640, 360)
    assert max(hi for _, hi in stat.extrema) > 0


def test_screenshot_detects_and_rejects_all_black_or_invalid_capture(monkeypatch):
    import sys
    from types import SimpleNamespace
    from PIL import Image
    import host_bridge.actions as actions

    # 1. Pure black PIL image must be flagged as all-black and rejected by _finalize_captured_image
    black_img = Image.new("RGB", (1920, 1080), color=(0, 0, 0))
    assert actions._is_image_all_black(black_img) is True
    with pytest.raises(ValueError, match="entirely black"):
        actions._finalize_captured_image(black_img)

    # 2. Non-black PIL image must pass validation
    valid_img = Image.new("RGB", (1920, 1080), color=(35, 40, 45))
    assert actions._is_image_all_black(valid_img) is False
    w, h, raw_bytes, b64_data = actions._finalize_captured_image(valid_img)
    assert (w, h) == (1920, 1080)
    assert len(raw_bytes) > 0
    assert len(b64_data) > 0

    # 3. _capture_with_mss must reject an all-black Xwayland buffer even when mss raises no exception
    class FakeBlackMSS:
        monitors = [{"left": 0, "top": 0, "width": 1920, "height": 1080}]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def grab(self, mon):
            return SimpleNamespace(
                size=(1920, 1080),
                bgra=b"\x00" * (1920 * 1080 * 4),
            )

    fake_mss_mod = SimpleNamespace(mss=FakeBlackMSS, MSS=FakeBlackMSS)
    monkeypatch.setitem(sys.modules, "mss", fake_mss_mod)

    with pytest.raises(ValueError, match="entirely black"):
        actions._capture_with_mss()


def test_linux_screenshot_fallback_when_primary_returns_black_or_fails(monkeypatch):
    from PIL import Image
    import host_bridge.actions as actions

    monkeypatch.setattr(actions, "IS_WINDOWS", False)
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "ubuntu:GNOME")

    raw_jpeg, b64_jpeg = _make_sample_jpeg_bytes(1280, 720, color=(80, 120, 200))
    attempted = []

    def fail_mutter():
        attempted.append("mutter_screencast")
        raise RuntimeError("PipeWire stream unavailable")

    def black_gnome_screenshot():
        attempted.append("gnome_screenshot")
        black_img = Image.new("RGB", (1920, 1080), color=(0, 0, 0))
        return actions._finalize_captured_image(black_img)

    def fail_gnome_dbus():
        attempted.append("gnome_shell_dbus")
        raise PermissionError("AccessDenied: Screenshot is not allowed")

    def succeed_grim():
        attempted.append("grim")
        return 1280, 720, raw_jpeg, b64_jpeg

    def black_mss():
        attempted.append("mss")
        black_img = Image.new("RGB", (1920, 1080), color=(0, 0, 0))
        return actions._finalize_captured_image(black_img)

    monkeypatch.setattr(actions, "_capture_with_mutter_screencast", fail_mutter)
    monkeypatch.setattr(actions, "_capture_with_gnome_screenshot", black_gnome_screenshot)
    monkeypatch.setattr(actions, "_capture_with_gnome_shell_dbus", fail_gnome_dbus)
    monkeypatch.setattr(actions, "_capture_with_grim", succeed_grim)
    monkeypatch.setattr(actions, "_capture_with_mss", black_mss)

    w, h, out_raw, out_b64 = actions._capture_desktop_screenshot_raw()
    assert (w, h) == (1280, 720)
    assert out_raw == raw_jpeg
    assert out_b64 == b64_jpeg
    assert attempted == ["mutter_screencast", "gnome_screenshot", "gnome_shell_dbus", "grim"]


def test_linux_screenshot_clear_failure_when_all_methods_fail(monkeypatch):
    from PIL import Image
    import host_bridge.actions as actions

    monkeypatch.setattr(actions, "IS_WINDOWS", False)
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "ubuntu:GNOME")

    def fail_mutter():
        raise PermissionError("Mutter ScreenCast access denied: portal restriction")

    def fail_gnome_screenshot():
        raise FileNotFoundError("gnome-screenshot not installed")

    def fail_gnome_dbus():
        raise PermissionError("GNOME Shell Screenshot D-Bus denied: AccessDenied: Screenshot is not allowed")

    def fail_grim():
        raise RuntimeError("grim failed (exit 1): compositor doesn't support wlr-screencopy-unstable-v1")

    def black_mss():
        return actions._finalize_captured_image(Image.new("RGB", (1920, 1080), color=(0, 0, 0)))

    def black_scrot():
        return actions._finalize_captured_image(Image.new("RGB", (1920, 1080), color=(0, 0, 0)))

    monkeypatch.setattr(actions, "_capture_with_mutter_screencast", fail_mutter)
    monkeypatch.setattr(actions, "_capture_with_gnome_screenshot", fail_gnome_screenshot)
    monkeypatch.setattr(actions, "_capture_with_gnome_shell_dbus", fail_gnome_dbus)
    monkeypatch.setattr(actions, "_capture_with_grim", fail_grim)
    monkeypatch.setattr(actions, "_capture_with_mss", black_mss)
    monkeypatch.setattr(actions, "_capture_with_scrot", black_scrot)

    res = actions.execute_screenshot({})
    assert res["success"] is False
    assert "Screen capture permission denied" in res["error"]
    assert "session=wayland" in res["error"]
    assert "desktop=ubuntu:GNOME" in res["error"]
    assert "entirely black" in res["error"]
    assert "wlr-screencopy-unstable-v1" in res["error"]


def test_windows_screenshot_behavior_unchanged(monkeypatch, tmp_path):
    from types import SimpleNamespace
    import host_bridge.actions as actions

    monkeypatch.setattr(actions, "IS_WINDOWS", True)
    monkeypatch.setattr(actions, "HOST_WORKSPACE_DIR", str(tmp_path))

    raw_jpeg, b64_jpeg = _make_sample_jpeg_bytes(1920, 1080, color=(20, 60, 100))
    recorded_cmds = []

    def fake_subprocess_run(cmd, **kwargs):
        recorded_cmds.append(cmd)
        return SimpleNamespace(
            returncode=0,
            stdout=f"1920x1080\n{b64_jpeg}\n",
            stderr="",
        )

    monkeypatch.setattr("subprocess.run", fake_subprocess_run)

    res = actions.execute_screenshot({})
    assert res["success"] is True
    assert res["width"] == 1920
    assert res["height"] == 1080
    assert res["format"] == "jpeg"
    assert res["size_bytes"] == len(raw_jpeg)
    assert res["image_base64"] == b64_jpeg
    assert len(recorded_cmds) == 1
    assert recorded_cmds[0][0] == "powershell"


def test_mutter_screencast_session_creation_inhibited_raises_clear_permission_error(monkeypatch):
    from types import SimpleNamespace
    import host_bridge.actions as actions

    screensaver_active = {"value": True}
    dbus_calls = []

    class FakeBus:
        def call_sync(self, dest, path, iface, method, *args):
            dbus_calls.append((dest, method))
            if dest == "org.gnome.Mutter.ScreenCast" and method == "CreateSession":
                raise RuntimeError("GDBus.Error:org.freedesktop.DBus.Error.Failed: Session creation inhibited (0)")
            if dest == "org.gnome.ScreenSaver" and method == "GetActive":
                return SimpleNamespace(unpack=lambda: (screensaver_active["value"],))
            raise AssertionError(f"Unexpected D-Bus call: {dest}.{method}")

    class FakeContext:
        def __init__(self):
            self.pushed = 0
            self.popped = 0

        def push_thread_default(self):
            self.pushed += 1

        def pop_thread_default(self):
            self.popped += 1

    contexts = []

    def new_ctx():
        c = FakeContext()
        contexts.append(c)
        return c

    fake_gio = SimpleNamespace(
        BusType=SimpleNamespace(SESSION=1),
        DBusCallFlags=SimpleNamespace(NONE=0),
        bus_get_sync=lambda *args: FakeBus(),
    )
    fake_glib = SimpleNamespace(
        MainContext=SimpleNamespace(new=new_ctx),
        Variant=lambda sig, val: (sig, val),
        VariantType=lambda sig: sig,
    )

    monkeypatch.setattr(actions, "_import_gi_gio_glib", lambda: (fake_gio, fake_glib))

    # Case 1: ScreenSaver reports active -> CreateSession is called first (authoritative), then ScreenSaver diagnostic
    with pytest.raises(PermissionError) as exc_info_locked:
        actions._capture_with_mutter_screencast()

    msg_locked = str(exc_info_locked.value)
    assert "Desktop capture is unavailable because GNOME has restricted ScreenCast access" in msg_locked
    assert "Session creation inhibited (0)" in msg_locked
    assert "org.gnome.ScreenSaver reports the desktop is locked" in msg_locked
    assert "Unlock the desktop and retry" in msg_locked
    assert dbus_calls == [
        ("org.gnome.Mutter.ScreenCast", "CreateSession"),
        ("org.gnome.ScreenSaver", "GetActive"),
    ]
    assert contexts[0].pushed == 1 and contexts[0].popped == 1

    # Case 2: ScreenSaver reports False -> Mutter's inhibition error remains authoritative
    dbus_calls.clear()
    screensaver_active["value"] = False
    with pytest.raises(PermissionError) as exc_info_other:
        actions._capture_with_mutter_screencast()

    msg_other = str(exc_info_other.value)
    assert "Desktop capture is unavailable because GNOME has restricted ScreenCast access" in msg_other
    assert "Session creation inhibited (0)" in msg_other
    assert "Unlock the desktop and retry" in msg_other
    assert dbus_calls == [
        ("org.gnome.Mutter.ScreenCast", "CreateSession"),
        ("org.gnome.ScreenSaver", "GetActive"),
    ]
    assert contexts[1].pushed == 1 and contexts[1].popped == 1


def test_wayland_locked_session_short_circuits_x11_fallbacks(monkeypatch):
    import host_bridge.actions as actions

    monkeypatch.setattr(actions, "IS_WINDOWS", False)
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "ubuntu:GNOME")

    attempted = []

    def inhibited_mutter():
        attempted.append("mutter_screencast")
        raise PermissionError(
            "Desktop capture is unavailable because GNOME has restricted ScreenCast access "
            "(GDBus.Error:org.freedesktop.DBus.Error.Failed: Session creation inhibited (0); "
            "org.gnome.ScreenSaver reports the desktop is locked). Unlock the desktop and retry."
        )

    def should_not_call(name):
        def _fn():
            attempted.append(name)
            raise AssertionError(f"{name} should not be called when GNOME Wayland ScreenCast is inhibited")
        return _fn

    monkeypatch.setattr(actions, "_capture_with_mutter_screencast", inhibited_mutter)
    monkeypatch.setattr(actions, "_capture_with_gnome_screenshot", should_not_call("gnome_screenshot"))
    monkeypatch.setattr(actions, "_capture_with_gnome_shell_dbus", should_not_call("gnome_shell_dbus"))
    monkeypatch.setattr(actions, "_capture_with_grim", should_not_call("grim"))
    monkeypatch.setattr(actions, "_capture_with_mss", should_not_call("mss"))
    monkeypatch.setattr(actions, "_capture_with_scrot", should_not_call("scrot"))

    res = actions.execute_screenshot({})
    assert res["success"] is False
    assert "Screen capture permission denied" in res["error"]
    assert "GNOME has restricted ScreenCast access" in res["error"]
    assert "Session creation inhibited (0)" in res["error"]
    assert "session=wayland" in res["error"]
    assert attempted == ["mutter_screencast"]


def test_mutter_screencast_unlocked_and_concurrent_serialization(monkeypatch):
    import threading
    import time
    from types import SimpleNamespace
    import host_bridge.actions as actions

    raw_jpeg, _ = _make_sample_jpeg_bytes(640, 360, color=(45, 95, 165))
    active_sessions = {"count": 0, "max_concurrent": 0, "stopped": 0}
    state_lock = threading.Lock()

    class FakeBus:
        def __init__(self):
            self._cb = None

        def call_sync(self, dest, path, iface, method, *args):
            if dest == "org.gnome.Mutter.ScreenCast" and method == "CreateSession":
                with state_lock:
                    active_sessions["count"] += 1
                    if active_sessions["count"] > active_sessions["max_concurrent"]:
                        active_sessions["max_concurrent"] = active_sessions["count"]
                time.sleep(0.03)
                return SimpleNamespace(unpack=lambda: ("/org/gnome/Mutter/ScreenCast/Session/u1",))
            if dest == "org.gnome.Mutter.ScreenCast" and method == "RecordMonitor":
                return SimpleNamespace(unpack=lambda: ("/org/gnome/Mutter/ScreenCast/Stream/u1",))
            if dest == "org.gnome.Mutter.ScreenCast" and method == "Start":
                if self._cb is not None:
                    self._cb(None, None, "/org/gnome/Mutter/ScreenCast/Stream/u1", iface, "PipeWireStreamAdded", SimpleNamespace(unpack=lambda: (42,)))
                return None
            if dest == "org.gnome.Mutter.ScreenCast" and method == "Stop":
                with state_lock:
                    active_sessions["count"] -= 1
                    active_sessions["stopped"] += 1
                return None
            raise AssertionError(f"Unexpected D-Bus call: {dest}.{method}")

        def signal_subscribe(self, sender, iface, signal, path, arg0, flags, cb):
            self._cb = cb
            return 99

        def signal_unsubscribe(self, sub_id):
            self._cb = None

    class FakeContext:
        def __init__(self):
            self.pushed = 0
            self.popped = 0

        def push_thread_default(self):
            self.pushed += 1

        def pop_thread_default(self):
            self.popped += 1

    class FakeMainLoop:
        def __init__(self, ctx, is_running):
            self.ctx = ctx

        def run(self):
            pass

        def quit(self):
            pass

    class FakeTimeoutSource:
        def set_callback(self, cb):
            pass

        def attach(self, ctx):
            pass

        def destroy(self):
            pass

    fake_gio = SimpleNamespace(
        BusType=SimpleNamespace(SESSION=1),
        DBusCallFlags=SimpleNamespace(NONE=0),
        DBusSignalFlags=SimpleNamespace(NONE=0),
        bus_get_sync=lambda *args: FakeBus(),
    )
    fake_glib = SimpleNamespace(
        MainContext=SimpleNamespace(new=lambda: FakeContext()),
        MainLoop=SimpleNamespace(new=lambda ctx, r: FakeMainLoop(ctx, r)),
        Variant=lambda sig, val: (sig, val),
        VariantType=lambda sig: sig,
        timeout_source_new=lambda ms: FakeTimeoutSource(),
    )

    def fake_subprocess_run(cmd, **kwargs):
        assert cmd[0] == "gst-launch-1.0"
        loc_arg = [part for part in cmd if part.startswith("location=")][0]
        out_path = loc_arg.split("=", 1)[1]
        with open(out_path, "wb") as f:
            f.write(raw_jpeg)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(actions, "_import_gi_gio_glib", lambda: (fake_gio, fake_glib))
    monkeypatch.setattr("subprocess.run", fake_subprocess_run)

    results = []
    errors = []

    def worker():
        try:
            res = actions._capture_with_mutter_screencast()
            results.append(res)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)

    assert not errors
    assert len(results) == 4
    for w, h, out_raw, _ in results:
        assert (w, h) == (640, 360)
        assert out_raw == raw_jpeg
    assert active_sessions["max_concurrent"] == 1
    assert active_sessions["count"] == 0
    assert active_sessions["stopped"] == 4


