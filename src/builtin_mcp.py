"""
builtin_mcp.py

Auto-registration of built-in MCP servers on startup.
Each server runs as a stdio subprocess managed by McpManager.
"""

import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys

from core.platform_compat import IS_WINDOWS, which_tool
from src.runtime_paths import get_app_root

logger = logging.getLogger(__name__)


def _find_npx() -> str:
    """Find the npx binary, checking common locations if not on PATH.

    On Windows the shim is `npx.cmd`, which `which_tool` resolves via PATHEXT.
    """
    npx = which_tool("npx")
    if npx:
        return npx
    if IS_WINDOWS:
        # Minimal-PATH fallbacks: npm's global bin lives under %APPDATA%\npm,
        # and node's installer dir carries npx.cmd alongside node.exe.
        appdata = os.environ.get("APPDATA", os.path.expanduser("~"))
        for candidate in (
            os.path.join(appdata, "npm", "npx.cmd"),
            r"C:\Program Files\nodejs\npx.cmd",
        ):
            if os.path.isfile(candidate):
                return candidate
        node = which_tool("node")
        if node:
            cand = os.path.join(os.path.dirname(node), "npx.cmd")
            if os.path.isfile(cand):
                return cand
        return "npx.cmd"  # fallback, will fail with a clear error
    # Common POSIX locations when PATH is minimal (e.g. systemd)
    for candidate in [
        os.path.expanduser("~/.npm-global/bin/npx"),
        os.path.expanduser("~/.local/bin/npx"),
        "/usr/local/bin/npx",
        "/usr/bin/npx",
    ]:
        if os.path.isfile(candidate):
            return candidate
    # Try to find node and use npx from same dir
    node = shutil.which("node")
    if node:
        npx_candidate = os.path.join(os.path.dirname(node), "npx")
        if os.path.isfile(npx_candidate):
            return npx_candidate
    return "npx"  # fallback, will fail with a clear error

# Server definitions: id -> (script path relative to project root, display name)
#
# bash / python / filesystem / web_search were folded into native in-process
# execution (src/tool_execution.py:_direct_fallback). Those trivial subprocess
# wrappers are gone.
#
# image_gen / memory / rag / email still run as stdio MCP servers — each
# carries hundreds of LOC of unique IMAP / HTTP / manager logic not worth
# duplicating into the native path right now.
_BUILTIN_SERVERS = {
    "image_gen":  ("mcp_servers/image_gen_server.py",  "Built-in: Image Generation"),
    "memory":     ("mcp_servers/memory_server.py",     "Built-in: Memory"),
    "rag":        ("mcp_servers/rag_server.py",        "Built-in: RAG"),
    "email":      ("mcp_servers/email_server.py",      "Built-in: Email"),
}

# NPX-based built-in servers (run via npx, not Python)
_BUILTIN_NPX_SERVERS = {
    "builtin_browser": {
        "name": "Built-in: Browser",
        "command": "npx",
        "args": ["-y", "@playwright/mcp@latest", "--headless", "--caps", "vision"],
    }
}

# Global flag to disable MCP if there are compatibility issues
MCP_DISABLED = os.environ.get("ODYSSEUS_DISABLE_MCP", "").lower() in ("1", "true", "yes")
BROWSER_MCP_REQUIRE_CACHE = os.environ.get("ODYSSEUS_BROWSER_MCP_REQUIRE_CACHE", "").lower() in ("1", "true", "yes")
LAZY_MCP_ENABLED = os.environ.get("ODYSSEUS_LAZY_MCP", "1").lower() not in ("0", "false", "no")

_DEFAULT_BROWSER_TOOLS = [
    {
        "name": "browser_navigate",
        "description": "Navigate to a URL",
        "inputSchema": {
            "type": "object",
            "properties": {"url": {"type": "string", "description": "URL to navigate to"}},
            "required": ["url"],
        },
    },
    {
        "name": "browser_snapshot",
        "description": "Capture an accessibility snapshot of the current page",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "browser_click",
        "description": "Click an element on the page",
        "inputSchema": {
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "CSS or text selector to click"},
                "element": {"type": "string", "description": "Element description/selector to click"},
            },
        },
    },
    {
        "name": "browser_type",
        "description": "Type text into an element",
        "inputSchema": {
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "CSS selector to type into"},
                "text": {"type": "string", "description": "Text to type"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "browser_fill_form",
        "description": "Fill multiple form inputs at once",
        "inputSchema": {
            "type": "object",
            "properties": {"elements": {"type": "array", "description": "List of form field mappings"}},
        },
    },
    {
        "name": "browser_select_option",
        "description": "Select an option from a dropdown",
        "inputSchema": {
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "Dropdown selector"},
                "value": {"type": "string", "description": "Option value to select"},
            },
        },
    },
    {
        "name": "browser_press_key",
        "description": "Press a key on the keyboard",
        "inputSchema": {
            "type": "object",
            "properties": {"key": {"type": "string", "description": "Key name (e.g. Enter, Tab, ArrowDown)"}},
            "required": ["key"],
        },
    },
    {
        "name": "browser_wait_for",
        "description": "Wait for a selector or timeout",
        "inputSchema": {
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "Selector to wait for"},
                "timeout": {"type": "number", "description": "Timeout in milliseconds"},
            },
        },
    },
    {
        "name": "browser_take_screenshot",
        "description": "Take a screenshot of the current page or an element",
        "inputSchema": {
            "type": "object",
            "properties": {
                "selector": {"type": "string", "description": "Optional element selector to screenshot"},
            },
        },
    },
    {
        "name": "browser_drag",
        "description": "Drag and drop an element",
        "inputSchema": {
            "type": "object",
            "properties": {
                "startSelector": {"type": "string", "description": "Source element selector"},
                "endSelector": {"type": "string", "description": "Target element selector"},
            },
            "required": ["startSelector", "endSelector"],
        },
    },
    {
        "name": "browser_navigate_back",
        "description": "Navigate back in browser history",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "browser_close",
        "description": "Close the browser page/session",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

_FALLBACK_BUILTIN_TOOLS = {
    "image_gen": [
        {
            "name": "generate_image",
            "description": "Generate an image using an image-capable model (e.g. gpt-image-1)",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "Image description prompt"},
                    "model": {"type": "string", "description": "Model name (auto-detects if omitted)"},
                    "size": {"type": "string", "description": "Image size (default 1024x1024)"},
                    "quality": {"type": "string", "description": "Quality: low, medium, high, auto (default medium)"},
                },
                "required": ["prompt"],
            },
        }
    ],
    "memory": [
        {
            "name": "manage_memory",
            "description": "Manage the user's memory system: list, add, edit, delete, or search memories.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "add", "edit", "delete", "search"], "description": "The action to perform"},
                    "text": {"type": "string", "description": "Memory text (add/edit) or search query (search)"},
                    "memory_id": {"type": "string", "description": "Memory ID (edit/delete)"},
                    "category": {"type": "string", "enum": ["fact", "event", "contact", "preference"], "description": "Memory category (add/list filter)"},
                },
                "required": ["action"],
            },
        }
    ],
    "rag": [
        {
            "name": "manage_rag",
            "description": "Manage RAG indexed documents. List indexed files, add directories, or remove directories.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "action": {"type": "string", "enum": ["list", "add_directory", "remove_directory"], "description": "The action to perform"},
                    "directory": {"type": "string", "description": "Directory path (for add/remove)"},
                },
                "required": ["action"],
            },
        }
    ],
    "builtin_browser": _DEFAULT_BROWSER_TOOLS,
}


async def _get_builtin_python_tools(server_id: str) -> list[dict]:
    """Inspect or retrieve tools for a built-in Python MCP server without spawning subprocesses."""
    try:
        if server_id == "image_gen":
            from mcp_servers import image_gen_server
            tools = await image_gen_server.list_tools()
            return [{"name": t.name, "description": t.description, "inputSchema": t.inputSchema} for t in tools]
        elif server_id == "memory":
            from mcp_servers import memory_server
            tools = await memory_server.list_tools()
            return [{"name": t.name, "description": t.description, "inputSchema": t.inputSchema} for t in tools]
        elif server_id == "rag":
            from mcp_servers import rag_server
            tools = await rag_server.list_tools()
            return [{"name": t.name, "description": t.description, "inputSchema": t.inputSchema} for t in tools]
        elif server_id == "email":
            from mcp_servers import email_server
            tools = await email_server.list_tools()
            return [{"name": t.name, "description": t.description, "inputSchema": t.inputSchema} for t in tools]
    except Exception as e:
        logger.debug(f"Dynamic tool extraction for {server_id} deferred: {e}")
    return _FALLBACK_BUILTIN_TOOLS.get(server_id, [])


# Strong references to the fire-and-forget startup tasks scheduled below.
# asyncio only keeps weak references to tasks created via create_task, so
# without this the GC can collect a task mid-execution and the server
# registration silently never runs. Mirrors _spawn_bg in routes/chat_helpers.py.
_BG_TASKS: set[asyncio.Task] = set()


def _spawn_bg(coro) -> asyncio.Task:
    """Schedule a background task and hold a strong reference until it finishes."""
    task = asyncio.create_task(coro)
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)
    return task

def _find_browser_executable() -> str:
    """Find a browser binary for the built-in Playwright MCP server.

    Docker images ship Debian's `chromium`; desktop installs may already have
    Chrome/Chromium in a conventional location. If nothing is found, return an
    empty string and let Playwright MCP use its own default browser/channel.
    """
    configured = os.environ.get("ODYSSEUS_BROWSER_EXECUTABLE", "").strip()
    if configured:
        return configured
    for name in ("google-chrome", "chromium", "chromium-browser"):
        path = shutil.which(name)
        if path:
            return path
    for candidate in (
        "/opt/google/chrome/chrome",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
    ):
        if os.path.isfile(candidate):
            return candidate
    return ""


def _browser_mcp_args(args: list[str]) -> list[str]:
    """Return Playwright MCP args with a concrete browser executable when found."""
    out = list(args or [])
    if "--executable-path" not in out:
        browser = _find_browser_executable()
        if browser:
            out.extend(["--executable-path", browser])
    if os.environ.get("ODYSSEUS_BROWSER_ISOLATED", "1").lower() not in ("0", "false", "no"):
        if "--isolated" not in out and "--user-data-dir" not in out:
            out.append("--isolated")
    if os.environ.get("ODYSSEUS_BROWSER_NO_SANDBOX", "1").lower() not in ("0", "false", "no"):
        if "--no-sandbox" not in out and "--sandbox" not in out:
            out.append("--no-sandbox")
    return out


def builtin_python_env(base_dir: str) -> dict[str, str]:
    """Environment for built-in Python MCP subprocesses.

    The app root must be importable so mcp_servers can import local modules, but
    replacing PYTHONPATH entirely hides site-packages in container/dev launches
    that rely on PYTHONPATH for their active environment.
    """
    existing = os.environ.get("PYTHONPATH", "")
    parts = [base_dir]
    for item in existing.split(os.pathsep):
        if item and item not in parts:
            parts.append(item)
    return {"PYTHONPATH": os.pathsep.join(parts)}


async def register_builtin_servers(mcp_manager):
    """Connect all built-in MCP servers to the manager."""
    if MCP_DISABLED:
        logger.info("Built-in MCP servers disabled via ODYSSEUS_DISABLE_MCP")
        return

    base_dir = get_app_root()
    python = sys.executable

    if LAZY_MCP_ENABLED:
        logger.info("Registering built-in MCP servers in lazy on-demand mode")
        for server_id, (script, name) in _BUILTIN_SERVERS.items():
            script_path = os.path.join(base_dir, script)
            if not os.path.exists(script_path):
                logger.warning(f"Built-in MCP server script not found: {script_path}")
                continue
            tools = await _get_builtin_python_tools(server_id)
            mcp_manager.register_lazy_builtin(
                server_id=server_id,
                name=name,
                command=python,
                args=[script_path],
                env=builtin_python_env(base_dir),
                tools=tools,
            )
            logger.info(f"Registered lazy built-in MCP server: {name} ({len(tools)} tools)")

        npx_path = _find_npx()
        cfg = _BUILTIN_NPX_SERVERS.get("builtin_browser")
        if cfg:
            args = _browser_mcp_args(cfg["args"])
            cache_home = os.environ.get(
                "ODYSSEUS_BROWSER_MCP_CACHE",
                os.path.join(base_dir, "data", "local", "playwright-mcp-cache"),
            )
            os.makedirs(cache_home, exist_ok=True)
            env = {
                "XDG_CACHE_HOME": cache_home,
                "PLAYWRIGHT_BROWSERS_PATH": os.path.join(cache_home, "browsers"),
            }
            mcp_manager.register_lazy_builtin(
                server_id="builtin_browser",
                name=cfg["name"],
                command=npx_path,
                args=args,
                env=env,
                tools=_DEFAULT_BROWSER_TOOLS,
            )
            logger.info(f"Registered lazy built-in NPX server: {cfg['name']} ({len(_DEFAULT_BROWSER_TOOLS)} tools)")
        return

    async def _connect_python_server(server_id: str, script_path: str, name: str):
        try:
            ok = await mcp_manager.connect_server(
                server_id=server_id,
                name=name,
                transport="stdio",
                command=python,
                args=[script_path],
                env=builtin_python_env(base_dir),
            )
            if ok:
                logger.info(f"Built-in MCP server registered: {name}")
            else:
                logger.warning(f"Built-in MCP server failed to connect: {name}")
        except asyncio.CancelledError:
            logger.warning(f"Built-in MCP server {name} cancelled")
            raise
        except BaseException as e:
            logger.warning(f"Built-in MCP server {name} error: {type(e).__name__}: {e}")

    for server_id, (script, name) in _BUILTIN_SERVERS.items():
        script_path = os.path.join(base_dir, script)
        if not os.path.exists(script_path):
            logger.warning(f"Built-in MCP server script not found: {script_path}")
            continue
        _spawn_bg(_connect_python_server(server_id, script_path, name))

    # Register NPX-based servers in the background (they take longer to start)
    npx_path = _find_npx()
    logger.info(f"NPX binary resolved to: {npx_path}")

    async def _start_npx_servers():
        await asyncio.sleep(3)  # let Python servers finish first
        for server_id, cfg in _BUILTIN_NPX_SERVERS.items():
            args = _browser_mcp_args(cfg["args"]) if server_id == "builtin_browser" else list(cfg["args"])
            pkg_spec = _npx_package_from_args(args)
            if BROWSER_MCP_REQUIRE_CACHE and pkg_spec and not await _is_npx_package_cached(npx_path, pkg_spec):
                logger.warning(
                    f"{cfg['name']} is not available.\n"
                    f"  Reason: npm package {pkg_spec!r} is not installed in the npx cache.\n"
                    f"  Impact: tools provided by this MCP server will be unavailable.\n"
                    f"  Fix:    {os.path.basename(npx_path)} -y {pkg_spec} --version\n"
                    f"          (run once, then restart Odysseus)\n"
                    f"  Notes:  ODYSSEUS_BROWSER_MCP_REQUIRE_CACHE=1 is set, "
                    f"so Odysseus will not install browser automation on startup."
                )
                continue

            logger.info(f"Starting NPX server: {cfg['name']} ({npx_path} {' '.join(args)})")
            try:
                env = None
                if server_id == "builtin_browser":
                    cache_home = os.environ.get(
                        "ODYSSEUS_BROWSER_MCP_CACHE",
                        os.path.join(base_dir, "data", "local", "playwright-mcp-cache"),
                    )
                    os.makedirs(cache_home, exist_ok=True)
                    env = {
                        "XDG_CACHE_HOME": cache_home,
                        "PLAYWRIGHT_BROWSERS_PATH": os.path.join(cache_home, "browsers"),
                    }
                ok = await mcp_manager.connect_server(
                    server_id=server_id,
                    name=cfg["name"],
                    transport="stdio",
                    command=npx_path,
                    args=args,
                    env=env,
                )
                if ok:
                    logger.info(f"Built-in NPX server registered: {cfg['name']}")
                else:
                    logger.warning(f"Built-in NPX server failed to connect: {cfg['name']}")
            except asyncio.CancelledError:
                raise
            except BaseException as e:
                logger.warning(f"Built-in NPX server {cfg['name']} error: {type(e).__name__}: {e}")

    _spawn_bg(_start_npx_servers())


def _npx_package_from_args(args):
    """Pick the package spec out of an npx args list shaped like
    ['-y', '<package@version>', ...flags]. Returns None if the
    convention doesn't match (we then skip the cache check and just
    try the connect)."""
    if not args:
        return None
    if "-y" in args:
        idx = args.index("-y") + 1
        if idx < len(args) and not args[idx].startswith("-"):
            return args[idx]
    # No -y prefix: first non-flag arg is the package
    for a in args:
        if not a.startswith("-"):
            return a
    return None


async def _is_npx_package_cached(npx_path, package_spec, timeout_s=5):
    """Probe whether an npx package is already in the local cache.

    First checks the local `_npx` cache for an installed package. If the
    package is not found there, falls back to `npx --no-install <pkg>
    --version` so older npm layouts still work without downloading.
    """
    if _is_package_in_npx_cache(package_spec):
        return True

    try:
        proc = await asyncio.create_subprocess_exec(
            npx_path, "--no-install", package_spec, "--version",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except NotImplementedError:
        try:
            result = subprocess.run(
                [npx_path, "--no-install", package_spec, "--version"],
                capture_output=True,
                timeout=timeout_s,
            )
        except (subprocess.TimeoutExpired, OSError, ValueError):
            return False
        return result.returncode == 0 and bool(result.stdout.strip())
    except (OSError, ValueError):
        return False
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        return False
    except asyncio.CancelledError:
        # The probe was cancelled (e.g. app shutdown). Reap the child so it
        # isn't orphaned, then propagate the cancellation.
        try:
            proc.kill()
            await proc.wait()
        except Exception:
            pass
        raise
    return proc.returncode == 0 and bool(stdout.strip())


def _is_package_in_npx_cache(package_spec):
    """Return True when npm's `_npx` cache already contains package_spec."""
    package_name = _npx_package_name(package_spec)
    if not package_name:
        return False

    for cache_root in _npm_cache_roots():
        npx_root = os.path.join(cache_root, "_npx")
        if _npx_cache_contains_package(npx_root, package_name):
            return True
    return False


def _npx_package_name(package_spec):
    """Strip a version/range suffix from an npm package spec."""
    if not package_spec:
        return ""
    if package_spec.startswith("@"):
        parts = package_spec.split("@", 2)
        if len(parts) >= 3:
            return f"@{parts[1]}"
        return package_spec
    return package_spec.split("@", 1)[0]


def _npm_cache_roots():
    roots = []
    configured = os.environ.get("npm_config_cache")
    if configured:
        roots.append(os.path.expanduser(configured))
    home = os.environ.get("HOME") or os.path.expanduser("~")
    roots.append(os.path.join(home, ".npm"))
    roots.append(os.path.join(os.path.expanduser("~"), ".npm"))
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        roots.append(os.path.join(local_app_data, "npm-cache"))
    return list(dict.fromkeys(roots))


def _npx_cache_contains_package(npx_root, package_name):
    if not os.path.isdir(npx_root):
        return False
    package_path = os.path.join("node_modules", *package_name.split("/"), "package.json")
    try:
        entries = list(os.scandir(npx_root))
    except OSError:
        return False
    for entry in entries:
        try:
            is_dir = entry.is_dir()
        except OSError:
            continue
        cached_name = _cached_package_name(os.path.join(entry.path, package_path))
        if is_dir and cached_name == package_name:
            return True
    return False


def _cached_package_name(package_json_path):
    try:
        with open(package_json_path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return ""
    return str(data.get("name", "")).strip()
