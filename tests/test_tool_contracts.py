import subprocess
import sys
from pathlib import Path


def test_tool_parser_imports_without_loading_compatibility_facade():
    code = """
import sys
import src.tool_parsing
assert "src.agent_tools" not in sys.modules

from src.tool_contracts import TOOL_TAGS, ToolBlock
from src.agent_tools import TOOL_TAGS as facade_tags, ToolBlock as facade_block
assert facade_tags is TOOL_TAGS
assert facade_block is ToolBlock
"""
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=root,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
