"""Shared test configuration - ensure project root is on sys.path and stub heavy deps."""
import sys
import os
import types
import importlib.util
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Importing core.database below runs init_db() at import time, and its default
# (sqlite:///./data/app.db) can't be opened in a clean worktree because SQLite
# won't create the missing ./data parent dir - pytest then dies during
# collection, before any test module loads. Default to an in-memory DB for the
# test session so collection is deterministic and writes no repo-local
# artifacts. An explicit DATABASE_URL (a real test/CI database) is preserved.
# This only unblocks collection/import-time init; it does not provide a shared
# file-backed DB across processes - tests needing that must set DATABASE_URL.
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

# Pre-import real heavy modules BEFORE any test file's module-level stubs can
# replace them with MagicMock. Some test files (e.g. test_llm_core_sanitize_*)
# stub sqlalchemy/core.database at module scope with `if mod not in sys.modules`,
# which fires during collection. If the real module hasn't been imported yet,
# the stub wins and contaminates every subsequent test that needs the real ORM.
try:
    import sqlalchemy  # noqa: F401
    import sqlalchemy.orm  # noqa: F401
    import core.database  # noqa: F401
    import src.database
except ImportError:
    pass  # not installed - the stubs below will handle it

def _has_module(mod_name: str) -> bool:
    try:
        return importlib.util.find_spec(mod_name) is not None
    except (ImportError, ValueError):
        return False


# Stub optional dependencies only when they are not installed. Do not replace
# real FastAPI/Starlette/Pydantic modules: route tests import their subpackages.
for mod_name in [
    "sqlalchemy", "sqlalchemy.orm", "sqlalchemy.types", "sqlalchemy.ext", "sqlalchemy.ext.declarative",
    "sqlalchemy.ext.hybrid", "sqlalchemy.sql", "sqlalchemy.sql.expression",
    "sqlalchemy.sql.sqltypes", "bcrypt", "pyotp",
    "httpx", "fastapi", "fastapi.responses", "fastapi.routing",
    "starlette", "starlette.responses", "starlette.middleware", "starlette.middleware.base",
    "pydantic", "bs4", "requests",
]:
    if mod_name not in sys.modules and not _has_module(mod_name):
        sys.modules[mod_name] = MagicMock()

if "src.database" not in sys.modules:
    _db = types.ModuleType("src.database")
    _db.SessionLocal = MagicMock()
    _db.ModelEndpoint = MagicMock()
    sys.modules["src.database"] = _db

if "mcp" not in sys.modules and not _has_module("mcp"):
    _mcp = types.ModuleType("mcp")
    _mcp_server = types.ModuleType("mcp.server")
    _mcp_server_stdio = types.ModuleType("mcp.server.stdio")
    _mcp_types = types.ModuleType("mcp.types")

    class _StubServer:
        def __init__(self, *args, **kwargs): pass
        def list_tools(self, *args, **kwargs):
            return lambda fn: fn
        def call_tool(self, *args, **kwargs):
            return lambda fn: fn

    _mcp_server.Server = _StubServer
    _mcp_server.stdio = _mcp_server_stdio
    _mcp_server.stdio_server = MagicMock()
    _mcp_server_stdio.stdio_server = MagicMock()

    class _StubTextContent:
        def __init__(self, type="text", text="", **kwargs):
            self.type = type
            self.text = text

    class _StubTool:
        def __init__(self, name="", description="", inputSchema=None, **kwargs):
            self.name = name
            self.description = description
            self.inputSchema = inputSchema or {}

    _mcp_types.Tool = _StubTool
    _mcp_types.TextContent = _StubTextContent

    sys.modules["mcp"] = _mcp
    sys.modules["mcp.server"] = _mcp_server
    sys.modules["mcp.server.stdio"] = _mcp_server_stdio
    sys.modules["mcp.types"] = _mcp_types
    _mcp.server = _mcp_server
    _mcp.types = _mcp_types

# Pre-import core.models before test_agent_loop.py's module-level stubs
# run (it replaces sys.modules['core.models'] with a MagicMock during
# collection, which breaks session import in subsequent tests).
import core.models  # noqa: E402

# Platform compatibility: provide mock ctypes.windll on non-Windows platforms
# so that Windows-specific tests using `patch("ctypes.windll.user32", ...)`
# can run on Linux without AttributeError.
import ctypes  # noqa: E402
if not hasattr(ctypes, "windll"):
    mock_windll = MagicMock()
    mock_windll.user32 = MagicMock()
    mock_windll.user32.SendInput.return_value = 2
    mock_windll.kernel32 = MagicMock()
    ctypes.windll = mock_windll

if not hasattr(ctypes, "WINFUNCTYPE"):
    ctypes.WINFUNCTYPE = ctypes.CFUNCTYPE


def pytest_configure(config):
    """Register the dynamic taxonomy ``sub_*`` markers before collection.

    The stable ``area_*`` markers are declared in ``pyproject.toml``. The
    per-file ``sub_*`` markers are derived from the test filenames here so that
    unknown-mark warnings still surface genuine typos outside the taxonomy. This
    only registers marker names; it imports no production module.
    """
    import pathlib
    from tests._taxonomy import discover_markers

    tests_dir = pathlib.Path(__file__).parent
    paths = list(tests_dir.rglob("test_*.py")) + list(tests_dir.rglob("*_test.py"))
    for marker_name in discover_markers(paths):
        if marker_name.startswith("sub_"):
            config.addinivalue_line("markers", f"{marker_name}: taxonomy sub-area marker")


def pytest_collection_modifyitems(config, items):
    """Tag each collected test with its taxonomy ``area_*`` and ``sub_*`` markers.

    Collection-time only: this adds markers and nothing else. It does not skip,
    reorder, or deselect tests, mutate fixtures or the environment, or import any
    production module. See ``tests/_taxonomy.py`` for the classification rules.
    """
    import pytest
    from tests._taxonomy import markers_for_path

    for item in items:
        path = getattr(item, "path", None) or item.fspath
        for marker_name in markers_for_path(path):
            item.add_marker(getattr(pytest.mark, marker_name))
