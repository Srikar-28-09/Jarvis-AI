import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from host_bridge.server import is_safe_bind_address
from src.host_bridge_client import check_host_bridge_health_sync


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_entrypoint():
    spec = importlib.util.spec_from_file_location("run_host_bridge_under_test", PROJECT_ROOT / "run_host_bridge.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bridge_listener_configuration_keeps_loopback_and_docker_private_only():
    assert is_safe_bind_address("127.0.0.1")
    assert is_safe_bind_address("172.17.0.1")
    assert is_safe_bind_address("172.28.10.1")
    assert not is_safe_bind_address("0.0.0.0")
    assert not is_safe_bind_address("192.168.1.20")


def test_docker_gateway_discovery_includes_default_and_custom_bridges(monkeypatch):
    module = _load_entrypoint()
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **kw: SimpleNamespace(
        stdout="""1: lo inet 127.0.0.1/8 scope host lo\n2: docker0 inet 172.17.0.1/16 brd 172.17.255.255 scope global docker0\n3: br-abc inet 172.28.10.1/24 brd 172.28.10.255 scope global br-abc\n"""
    ))
    assert module.detect_docker_gateway_ips() == ["172.17.0.1", "172.28.10.1"]


def test_sigterm_style_shutdown_closes_each_listener_and_hotkey(monkeypatch):
    module = _load_entrypoint()
    calls = []

    class Server:
        def __init__(self, name):
            self.name = name

        def serve_forever(self):
            calls.append((self.name, "serve"))
            raise KeyboardInterrupt

        def shutdown(self):
            calls.append((self.name, "shutdown"))

        def server_close(self):
            calls.append((self.name, "close"))

    primary, gateway = Server("local"), Server("gateway")
    monkeypatch.setattr(module, "get_or_create_bridge_token", lambda: "secret-token")
    monkeypatch.setattr(module, "create_host_bridge_server", lambda **kwargs: primary if kwargs["bind_address"] == "127.0.0.1" else gateway)
    monkeypatch.setattr(module, "detect_docker_gateway_ips", lambda: ["172.17.0.1"])
    monkeypatch.setattr(module, "IS_LINUX", True)
    monkeypatch.setattr(module, "HOST_BRIDGE_BIND", "127.0.0.1")
    monkeypatch.setattr(module, "JARVIS_HOTKEY_ENABLED", True)

    class Hotkey:
        def start(self):
            return True

        def stop(self):
            calls.append(("hotkey", "stop"))

    monkeypatch.setattr(module, "create_global_hotkey_listener", lambda **kwargs: Hotkey())
    # Gateway serve loop is started in a background thread; execute only the
    # shutdown helper synchronously so cleanup is deterministic here.
    class FakeThread:
        def __init__(self, target, **kwargs):
            self.target = target
            self.is_gateway_loop = "name" in kwargs

        def start(self):
            if not self.is_gateway_loop:
                self.target()

        def join(self, timeout=None):
            return None

    monkeypatch.setattr(module.threading, "Thread", FakeThread)
    monkeypatch.setattr(module.sys, "argv", ["run_host_bridge.py"])

    module.main()

    assert ("local", "serve") in calls
    assert ("gateway", "shutdown") in calls
    assert ("local", "close") in calls and ("gateway", "close") in calls
    assert ("hotkey", "stop") in calls


def test_user_service_restarts_bridge_and_uses_project_venv():
    unit = (PROJECT_ROOT / "odysseus-host-bridge.service").read_text()
    assert "ExecStart=%h/Projects/odysseus/.venv/bin/python %h/Projects/odysseus/run_host_bridge.py" in unit
    assert "Restart=on-failure" in unit
    assert "WantedBy=default.target" in unit
    assert "127.0.0.1" not in unit  # bind is configured securely by bridge defaults
    assert "0.0.0.0" not in unit


def test_health_check_reports_connection_refusal(monkeypatch):
    import src.host_bridge_client as client

    monkeypatch.setattr(client, "get_bridge_url", lambda: "http://127.0.0.1:1")
    monkeypatch.setattr(client, "get_bridge_token", lambda: "test-token")
    monkeypatch.setattr(client.urllib.request, "urlopen", lambda *a, **kw: (_ for _ in ()).throw(ConnectionRefusedError("refused")))
    result = check_host_bridge_health_sync(timeout=0.1)
    assert result["available"] is False
    assert "refused" in result["error"]

