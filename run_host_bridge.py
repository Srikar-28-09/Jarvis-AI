"""
run_host_bridge.py

Standalone entrypoint to run the Jarvis Host Bridge on the host machine (Linux and Windows).
Binds to 127.0.0.1:8096 (and Docker bridge gateway on Linux) to process authenticated requests from Odysseus.
"""

import logging
import os
import signal
import subprocess
import sys
import threading

# Ensure project root is in sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from host_bridge.actions import focus_or_launch_jarvis_window
from host_bridge.config import (
    HOST_BRIDGE_BIND,
    HOST_BRIDGE_PORT,
    IS_LINUX,
    IS_WINDOWS,
    JARVIS_HOTKEY,
    JARVIS_HOTKEY_ENABLED,
    JARVIS_UI_URL,
    get_or_create_bridge_token,
)
from host_bridge.hotkey import create_global_hotkey_listener
from host_bridge.server import create_host_bridge_server

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("host_bridge")


def _handle_sigterm(_signum, _frame):
    """Route service-manager termination through main()'s cleanup path."""
    raise KeyboardInterrupt


def detect_docker_gateway_ips() -> list[str]:
    """Discover local Docker bridge gateway IPs (e.g. 172.17.0.1) on Linux hosts."""
    ips: list[str] = []
    env_gw = os.environ.get("DOCKER_HOST_GATEWAY") or os.environ.get("DOCKER_GATEWAY_IP")
    if env_gw:
        ips.append(env_gw.strip())

    try:
        res = subprocess.run(
            ["ip", "-4", "-o", "addr", "show"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        for line in res.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 4 and any(d in parts[1] for d in ("docker", "br-")):
                ip = parts[3].split("/")[0]
                if ip not in ips:
                    ips.append(ip)
    except Exception:
        pass

    if not ips and IS_LINUX:
        ips.append("172.17.0.1")
    return ips


def main():
    if "--trigger-hotkey" in sys.argv or "--focus" in sys.argv:
        res = focus_or_launch_jarvis_window(url=JARVIS_UI_URL)
        logger.info("Focus/launch trigger completed: %s", res.get("message", res.get("action", "done")))
        sys.exit(0)

    token = get_or_create_bridge_token()
    token_masked = token[:6] + "..." + token[-4:] if len(token) > 10 else "***"
    platform_name = "LINUX (UBUNTU)" if IS_LINUX else ("WINDOWS" if IS_WINDOWS else "GENERIC")

    server = create_host_bridge_server(
        bind_address=HOST_BRIDGE_BIND,
        port=HOST_BRIDGE_PORT,
        auth_token=token,
    )

    docker_servers = []
    docker_server_threads = []
    if IS_LINUX and HOST_BRIDGE_BIND in ("127.0.0.1", "localhost"):
        for gw_ip in detect_docker_gateway_ips():
            if gw_ip != HOST_BRIDGE_BIND:
                try:
                    d_srv = create_host_bridge_server(
                        bind_address=gw_ip,
                        port=HOST_BRIDGE_PORT,
                        auth_token=token,
                    )
                    t = threading.Thread(
                        target=d_srv.serve_forever,
                        name=f"DockerGatewayBridge-{gw_ip}",
                        daemon=True,
                    )
                    t.start()
                    docker_servers.append(d_srv)
                    docker_server_threads.append(t)
                    logger.info("Docker Gateway Bridge Active: http://%s:%d", gw_ip, HOST_BRIDGE_PORT)
                except Exception as e:
                    logger.debug("Failed binding Docker gateway IP %s: %s", gw_ip, e)

    logger.info("==================================================")
    logger.info("JARVIS SECURE %s HOST BRIDGE", platform_name)
    logger.info("==================================================")
    logger.info("Listening on: http://%s:%d", HOST_BRIDGE_BIND, HOST_BRIDGE_PORT)
    logger.info("Docker target URL: http://host.docker.internal:%d", HOST_BRIDGE_PORT)
    logger.info("Authentication: Active (Token prefix: %s)", token_masked)
    logger.info("Allowed Actions: open_application, open_url, screenshot, click, double_click, type_text, press_key, scroll")
    logger.info("Security: Localhost and Docker internal gateway only. No LAN exposure.")

    hotkey_listener = None
    if JARVIS_HOTKEY_ENABLED:
        try:
            hotkey_listener = create_global_hotkey_listener(
                hotkey_str=JARVIS_HOTKEY,
                callback=lambda: focus_or_launch_jarvis_window(url=JARVIS_UI_URL),
            )
            started = hotkey_listener.start()
            if started:
                logger.info("Global Hotkey: Configured [%s] -> Focuses/launches %s", JARVIS_HOTKEY, JARVIS_UI_URL)
            else:
                logger.warning("Global Hotkey: Not registered [%s]", JARVIS_HOTKEY)
        except Exception as e:
            logger.warning("Global Hotkey initialization failed: %s", e)

    logger.info("Ready for requests from Jarvis Odysseus container...")
    logger.info("==================================================")

    servers = [server, *docker_servers]
    previous_sigterm_handler = None
    if hasattr(signal, "SIGTERM"):
        previous_sigterm_handler = signal.getsignal(signal.SIGTERM)
        # Raising in the main thread unwinds serve_forever into the same cleanup
        # path as Ctrl+C. This also lets systemd stop every gateway listener.
        signal.signal(signal.SIGTERM, _handle_sigterm)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down Host Bridge server...")
    finally:
        if previous_sigterm_handler is not None:
            signal.signal(signal.SIGTERM, previous_sigterm_handler)
        if hotkey_listener:
            try:
                hotkey_listener.stop()
            except Exception:
                logger.exception("Failed to stop global hotkey listener")
        # The primary serve_forever loop has already exited. Shutdown gateway
        # loops from this main thread, then wait for their serve loops to exit.
        for d_srv, thread in zip(docker_servers, docker_server_threads):
            try:
                d_srv.shutdown()
                thread.join(timeout=2.0)
            except Exception:
                logger.exception("Failed to stop Docker gateway listener")
        for srv in servers:
            try:
                srv.server_close()
            except Exception:
                logger.exception("Failed to close Host Bridge listener")


if __name__ == "__main__":
    main()


