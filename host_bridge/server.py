"""
host_bridge/server.py

Secure, localhost-only HTTP server for the Jarvis Windows Host Bridge.
Enforces token authentication, payload size caps, strict allowlisted action routing,
and zero shell/arbitrary command exposure.
"""

from __future__ import annotations

import hmac
from http.server import HTTPServer, BaseHTTPRequestHandler
import ipaddress
import json
import logging
import socket
import subprocess
import sys
from typing import Any, Optional

from .actions import ACTION_HANDLERS
from .config import (
    HOST_BRIDGE_BIND,
    HOST_BRIDGE_PORT,
    MAX_REQUEST_BYTES,
    get_or_create_bridge_token,
)

logger = logging.getLogger("host_bridge.server")


def _get_local_host_ips() -> set[str]:
    """Return IP addresses assigned to this machine's own network interfaces."""
    ips = {"127.0.0.1", "::1"}
    try:
        res = subprocess.run(["ip", "-4", "-o", "addr", "show"], capture_output=True, text=True, timeout=2)
        for line in res.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 4:
                ips.add(parts[3].split("/")[0])
    except Exception:
        pass
    return ips


class HostBridgeHTTPRequestHandler(BaseHTTPRequestHandler):
    """HTTP Request Handler for Jarvis Host Bridge."""

    server_version = "JarvisHostBridge/1.0"

    def _send_json(self, status_code: int, payload: dict) -> None:
        """Send a JSON response."""
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _is_allowed_client_ip(self) -> bool:
        """Verify connecting client is localhost, internal Docker network, or the local host itself."""
        client_ip_str = self.client_address[0]
        try:
            ip = ipaddress.ip_address(client_ip_str)
            if ip.is_loopback or client_ip_str in _get_local_host_ips():
                return True
            for net in [ipaddress.ip_network("172.16.0.0/12"), ipaddress.ip_network("10.0.0.0/8")]:
                if ip in net:
                    return True
            return False
        except ValueError:
            return False

    def _authenticate(self) -> bool:
        """Verify request authentication token using constant-time comparison."""
        expected_token = getattr(self.server, "auth_token", "")
        if not expected_token:
            return False

        # Check Authorization: Bearer <token>
        auth_header = self.headers.get("Authorization", "")
        token = ""
        if auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()
        elif auth_header.startswith("bearer "):
            token = auth_header[7:].strip()

        # Check X-Host-Bridge-Token fallback header
        if not token:
            token = self.headers.get("X-Host-Bridge-Token", "").strip()

        if not token:
            return False

        return hmac.compare_digest(token.encode("utf-8"), expected_token.encode("utf-8"))

    def do_GET(self) -> None:
        """Handle GET requests (e.g. /health)."""
        if not self._is_allowed_client_ip():
            self._send_json(403, {"error": "Forbidden: Client IP not permitted"})
            return

        path = self.path.split("?")[0].rstrip("/")

        if path in ("/health", "/v1/health"):
            if not self._authenticate():
                self._send_json(401, {"error": "Unauthorized: missing or invalid authentication token"})
                return
            self._send_json(200, {
                "status": "healthy",
                "service": "jarvis-host-bridge",
                "version": "1.0.0",
                "allowed_actions": sorted(ACTION_HANDLERS.keys()),
            })
            return

        # Explicitly reject all other GET paths
        self._send_json(404, {"error": f"Endpoint '{self.path}' not found"})

    def do_POST(self) -> None:
        """Handle POST action requests."""
        if not self._is_allowed_client_ip():
            self._send_json(403, {"error": "Forbidden: Client IP not permitted"})
            return

        path = self.path.split("?")[0].rstrip("/")

        # Validate Content-Length first so we know how many bytes to read/drain
        content_length_str = self.headers.get("Content-Length")
        content_length = 0
        if content_length_str:
            try:
                content_length = int(content_length_str)
            except ValueError:
                pass

        def _drain_body():
            if content_length > 0:
                try:
                    self.rfile.read(min(content_length, MAX_REQUEST_BYTES + 4096))
                except Exception:
                    pass

        # Check for disallowed/dangerous endpoints immediately
        if any(bad in path for bad in ("/shell", "/exec", "/cmd", "/powershell", "/terminal", "/bash")):
            _drain_body()
            self._send_json(403, {"error": "Forbidden: Arbitrary command/shell endpoints do not exist."})
            return

        if path not in ("/v1/action", "/action"):
            _drain_body()
            self._send_json(404, {"error": f"Endpoint '{self.path}' not found. Supported endpoint: POST /v1/action"})
            return

        # Authenticate
        if not self._authenticate():
            _drain_body()
            self._send_json(401, {"error": "Unauthorized: missing or invalid authentication token"})
            return

        if not content_length_str:
            self._send_json(400, {"error": "Content-Length header is required"})
            return

        if content_length > MAX_REQUEST_BYTES:
            _drain_body()
            self._send_json(413, {"error": f"Payload exceeds max size of {MAX_REQUEST_BYTES} bytes"})
            return

        if content_length <= 0:
            self._send_json(400, {"error": "Request body cannot be empty"})
            return

        # Read body
        try:
            body_bytes = self.rfile.read(content_length)
            body_json = json.loads(body_bytes.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            self._send_json(400, {"error": f"Malformed JSON body: {e}"})
            return
        except Exception as e:
            self._send_json(500, {"error": f"Failed to read request body: {e}"})
            return

        if not isinstance(body_json, dict):
            self._send_json(400, {"error": "JSON body must be an object"})
            return

        # Route action
        action_name = str(body_json.get("action") or "").strip().lower()
        if not action_name:
            self._send_json(400, {
                "error": "Field 'action' is required",
                "allowed_actions": sorted(ACTION_HANDLERS.keys()),
            })
            return

        handler = ACTION_HANDLERS.get(action_name)
        if not handler:
            self._send_json(400, {
                "error": f"Unknown action '{action_name}'",
                "allowed_actions": sorted(ACTION_HANDLERS.keys()),
            })
            return

        # Execute safe action handler
        try:
            result = handler(body_json)
            status_code = 200 if result.get("success", False) else 400
            self._send_json(status_code, result)
        except Exception as e:
            logger.exception("Error executing action '%s'", action_name)
            self._send_json(500, {"success": False, "error": f"Action execution failed: {e}"})

    def log_message(self, format: str, *args: Any) -> None:
        """Structured logging without leaking request bodies or auth tokens."""
        logger.info("%s - - [%s] %s", self.address_string(), self.log_date_time_string(), format % args)


def is_safe_bind_address(bind_address: str) -> bool:
    """Validate that the bind address is loopback or a private Docker bridge gateway."""
    if bind_address in ("127.0.0.1", "localhost", "::1"):
        return True
    try:
        ip = ipaddress.ip_address(bind_address)
        if ip.is_loopback:
            return True
        if ip in ipaddress.ip_network("172.16.0.0/12"):
            return True
    except ValueError:
        pass
    return False


def create_host_bridge_server(
    bind_address: str = HOST_BRIDGE_BIND,
    port: int = HOST_BRIDGE_PORT,
    auth_token: Optional[str] = None,
) -> HTTPServer:
    """Create and configure the Host Bridge HTTP Server."""
    # Ensure localhost or private docker interface only
    if not is_safe_bind_address(bind_address):
        raise ValueError(f"Security violation: Bridge must bind to localhost or docker interface only (got {bind_address})")

    token = auth_token or get_or_create_bridge_token()
    server = HTTPServer((bind_address, port), HostBridgeHTTPRequestHandler)
    server.auth_token = token
    return server

