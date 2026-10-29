"""
src/host_bridge_client.py

Client library used by Odysseus (inside Docker) to communicate with the
secure Windows Host Bridge running on the host (at host.docker.internal:8096).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional
import urllib.error
import urllib.request

logger = logging.getLogger("src.host_bridge_client")

# Defaults
DEFAULT_BRIDGE_URL = "http://host.docker.internal:8096"
WORKSPACE_TOKEN_PATH = "/workspace/jarvis/.host_bridge_token"
DEFAULT_TIMEOUT_SECONDS = 12.0


def get_bridge_url() -> str:
    """Get the Host Bridge URL with automatic Docker/host detection."""
    url = os.environ.get("JARVIS_HOST_BRIDGE_URL", "").strip()
    if url:
        return url.rstrip("/")
    if os.path.exists("/.dockerenv"):
        return DEFAULT_BRIDGE_URL
    return "http://127.0.0.1:8096"


def get_bridge_token() -> str:
    """Retrieve the authentication token for the Host Bridge.

    Priority:
    1. Environment variable JARVIS_HOST_BRIDGE_TOKEN
    2. Shared token file /workspace/jarvis/.host_bridge_token (Docker)
    3. Host token paths (~/Projects/JarvisWorkspace/.host_bridge_token, D:\JarvisWorkspace, etc.)
    """
    token = os.environ.get("JARVIS_HOST_BRIDGE_TOKEN", "").strip()
    if token:
        return token

    candidate_paths = [
        Path(WORKSPACE_TOKEN_PATH),
        Path.home() / "Projects" / "JarvisWorkspace" / ".host_bridge_token",
        Path.home() / ".odysseus" / "host_bridge_token.txt",
        Path("data/host_bridge_token.txt"),
        Path(r"D:\JarvisWorkspace\.host_bridge_token"),
    ]
    workspace_env = os.environ.get("JARVIS_WORKSPACE_DIR")
    if workspace_env:
        candidate_paths.insert(0, Path(workspace_env) / ".host_bridge_token")

    for cand in candidate_paths:
        try:
            if cand.exists() and cand.is_file():
                content = cand.read_text(encoding="utf-8").strip()
                if content:
                    return content
        except Exception:
            continue

    return ""


def check_host_bridge_health_sync(timeout: float = 2.0) -> Dict[str, Any]:
    """Synchronous health check for the Host Bridge."""
    url = f"{get_bridge_url()}/health"
    token = get_bridge_token()

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Odysseus-HostBridgeClient/1.0",
            "Authorization": f"Bearer {token}",
        },
        method="GET",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return {"available": True, "details": data}
    except Exception as e:
        return {"available": False, "error": str(e)}


async def check_host_bridge_health(timeout: float = 2.0) -> Dict[str, Any]:
    """Asynchronous health check for the Host Bridge."""
    return await asyncio.to_thread(check_host_bridge_health_sync, timeout)


def execute_host_action_sync(
    action: str,
    params: Optional[Dict[str, Any]] = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    """Synchronously execute an action via the Windows Host Bridge."""
    base_url = get_bridge_url()
    token = get_bridge_token()
    action_url = f"{base_url}/v1/action"

    payload = {"action": action}
    if params:
        payload.update(params)

    body_bytes = json.dumps(payload).encode("utf-8")

    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Content-Length": str(len(body_bytes)),
        "User-Agent": "Odysseus-HostBridgeClient/1.0",
        "Authorization": f"Bearer {token}",
    }

    req = urllib.request.Request(action_url, data=body_bytes, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp_body = resp.read().decode("utf-8")
            return json.loads(resp_body)
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8")
            err_json = json.loads(err_body)
            return {
                "success": False,
                "error": err_json.get("error", f"HTTP {e.code}: {e.reason}"),
                "status_code": e.code,
                "bridge_available": True,
                "execution_status_uncertain": action in {"open_application", "open_url"} and e.code >= 500,
            }
        except Exception:
            return {
                "success": False,
                "error": f"Host bridge returned HTTP {e.code}: {e.reason}",
                "status_code": e.code,
                "bridge_available": True,
                "execution_status_uncertain": action in {"open_application", "open_url"} and e.code >= 500,
            }
    except (urllib.error.URLError, TimeoutError, ConnectionRefusedError, OSError) as e:
        logger.warning("Host bridge request failed (unavailable): %s", e)
        return {
            "success": False,
            "error": "Windows host bridge unavailable",
            "bridge_available": False,
            "details": str(e),
            "execution_status_uncertain": action in {"open_application", "open_url"},
        }
    except Exception as e:
        logger.exception("Unexpected error communicating with host bridge: %s", e)
        return {
            "success": False,
            "error": f"Host bridge client error: {e}",
            "bridge_available": False,
            "execution_status_uncertain": action in {"open_application", "open_url"},
        }


async def execute_host_action(
    action: str,
    params: Optional[Dict[str, Any]] = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    """Asynchronously execute an action via the Windows Host Bridge."""
    return await asyncio.to_thread(execute_host_action_sync, action, params, timeout)
