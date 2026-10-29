import asyncio
import json
import pytest
from unittest.mock import patch, MagicMock

from src.agent_tools.web_tools import WebSearchTool
from src.tool_schemas import FUNCTION_TOOL_SCHEMAS, function_call_to_tool_block
from src.agent_tools import TOOL_HANDLERS


def test_web_search_schema_registered():
    """Verify web_search is properly registered in tool schemas with expected properties."""
    schema = next((s for s in FUNCTION_TOOL_SCHEMAS if s.get("function", {}).get("name") == "web_search"), None)
    assert schema is not None, "web_search tool schema not found in FUNCTION_TOOL_SCHEMAS"
    fn = schema["function"]
    assert "query" in fn["parameters"]["properties"]
    assert "max_results" in fn["parameters"]["properties"]
    assert "time_filter" in fn["parameters"]["properties"]
    assert fn["parameters"]["required"] == ["query"]
    assert "web_search" in TOOL_HANDLERS
    assert callable(TOOL_HANDLERS["web_search"])


def test_function_call_to_tool_block_web_search():
    """Verify function_call_to_tool_block preserves query, max_results, and time_filter."""
    # Simple query
    block = function_call_to_tool_block("web_search", {"query": "rtx 5090"})
    assert block.tool_type == "web_search"
    assert block.content == "rtx 5090"

    # Query with max_results and time_filter
    block2 = function_call_to_tool_block("web_search", {"query": "rtx 5090", "max_results": 3, "time_filter": "week"})
    assert block2.tool_type == "web_search"
    payload = json.loads(block2.content)
    assert payload["query"] == "rtx 5090"
    assert payload["max_results"] == 3
    assert payload["time_filter"] == "week"


def test_web_search_successful_structured_results():
    """Verify successful SearXNG search formats structured results with title, url, engine, snippet, and source tags."""
    async def _run():
        mock_results = [
            {
                "title": "NVIDIA RTX 50 Series Overview",
                "url": "https://example.com/rtx-50",
                "snippet": "NVIDIA announced the next-gen RTX 50 Blackwell architecture...",
                "engine": "bing",
            },
            {
                "title": "RTX 5090 Mobile Specs",
                "url": "https://example.com/rtx-5090-mobile",
                "snippet": "Detailed benchmark leaks for RTX 5090 laptop GPU.",
                "engine": "mojeek",
            }
        ]

        with patch("src.agent_tools.web_tools.searxng_search_api", return_value=mock_results) as mock_api:
            tool = WebSearchTool()
            res = await tool.execute(json.dumps({"query": "rtx 50 laptop", "max_results": 2}))

            assert res["exit_code"] == 0
            assert res["untrusted_content"] is True
            output = res["output"]
            assert "Web search results for: 'rtx 50 laptop'" in output
            assert "NVIDIA RTX 50 Series Overview" in output
            assert "https://example.com/rtx-50" in output
            assert "bing" in output
            assert "RTX 5090 Mobile Specs" in output
            assert "<!-- SOURCES:" in output

            # Verify parsed sources inside marker
            src_start = output.find("<!-- SOURCES:") + len("<!-- SOURCES:")
            src_end = output.find(" -->", src_start)
            sources = json.loads(output[src_start:src_end])
            assert len(sources) == 2
            assert sources[0]["url"] == "https://example.com/rtx-50"
            assert sources[0]["title"] == "NVIDIA RTX 50 Series Overview"

    asyncio.run(_run())


def test_web_search_empty_query():
    """Verify empty query returns non-zero exit code with descriptive message."""
    async def _run():
        tool = WebSearchTool()
        res1 = await tool.execute("")
        assert res1["exit_code"] == 1
        assert "empty" in res1["output"].lower()

        res2 = await tool.execute("   \n\t  ")
        assert res2["exit_code"] == 1

        res3 = await tool.execute(json.dumps({"query": ""}))
        assert res3["exit_code"] == 1

    asyncio.run(_run())


def test_web_search_zero_results():
    """Verify zero results returns exit_code 0 with friendly explanation."""
    async def _run():
        with patch("src.agent_tools.web_tools.searxng_search_api", return_value=[]):
            tool = WebSearchTool()
            res = await tool.execute("unobtainable-term-xyz12345")
            assert res["exit_code"] == 0
            assert "No web search results found for: 'unobtainable-term-xyz12345'" in res["output"]

    asyncio.run(_run())


def test_web_search_timeout_and_error_handling():
    """Verify timeouts and network exceptions are handled gracefully."""
    async def _run():
        with patch("src.agent_tools.web_tools.searxng_search_api", side_effect=TimeoutError("Connection timed out")):
            tool = WebSearchTool()
            res = await tool.execute("rtx 5080")
            assert res["exit_code"] == 1
            assert "timed out" in res["output"].lower()

        with patch("src.agent_tools.web_tools.searxng_search_api", side_effect=Exception("SearXNG 503 Service Unavailable")):
            tool = WebSearchTool()
            res = await tool.execute("rtx 5080")
            assert res["exit_code"] == 1
            assert "SearXNG 503" in res["output"]

    asyncio.run(_run())


def test_web_search_content_clamping():
    """Verify snippet length is controlled and does not blow up LLM context."""
    async def _run():
        giant_snippet = "A" * 2000
        mock_results = [
            {
                "title": "Giant Article",
                "url": "https://example.com/giant",
                "snippet": giant_snippet,
                "engine": "presearch",
            }
        ]

        with patch("src.agent_tools.web_tools.searxng_search_api", return_value=mock_results):
            tool = WebSearchTool()
            res = await tool.execute("test giant")
            assert res["exit_code"] == 0
            # The snippet in output should be capped around 350 chars
            assert "A" * 400 not in res["output"]
            assert "A" * 300 in res["output"]

    asyncio.run(_run())
