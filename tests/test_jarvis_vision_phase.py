"""
tests/test_jarvis_vision_phase.py

Comprehensive test suite for the Jarvis Vision Phase covering:
1. Vision model availability & detection
2. Image payload creation
3. Multimodal Ollama request formatting
4. Screenshot -> vision pipeline
5. Structured visual result parsing & validation
6. Malformed image handling
7. Oversized image handling
8. Vision model unavailable fallback
9. Qwen3 remains default model
10. Existing computer-control functionality
11. Memory system integrity
12. Web search integrity
13. Reminder tools integrity
14. TTS / STT settings integrity
15. Host Bridge client integrity
"""

import asyncio
import base64
import json
import os
import tempfile
import urllib.error
from unittest.mock import MagicMock, patch
import pytest

from src.chat_helpers import is_vision_model
from src.vision_analyzer import (
    clean_base64_image,
    build_vision_prompt,
    parse_structured_vision_output,
    analyze_screenshot_sync,
    analyze_screenshot,
    DEFAULT_VISION_MODEL,
    MAX_IMAGE_BYTES,
    _ollama_inference_seconds,
)
from src.agent_tools.computer_control_tools import (
    ListDirectoryTool,
    CreateDirectoryTool,
    OpenApplicationTool,
    OpenUrlTool,
    TakeScreenshotTool,
    _APPLICATION_ALLOWLIST,
    JARVIS_WORKSPACE,
)
from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
from src.agent_tools import TOOL_HANDLERS, TOOL_TAGS
from src.settings import DEFAULT_SETTINGS, get_setting


def test_ollama_inference_timing_uses_server_prompt_and_eval_durations():
    assert _ollama_inference_seconds({
        "prompt_eval_duration": 2_000_000_000,
        "eval_duration": 3_000_000_000,
        "load_duration": 20_000_000_000,
    }, 30.0) == 5.0
    # Older Ollama responses may omit server-side timing fields.
    assert _ollama_inference_seconds({"load_duration": 2_000_000_000}, 7.0) == 5.0


# ===========================================================================
# 1. Vision Model Availability & Detection
# ===========================================================================

def test_qwen25vl_detected_as_vision_model():
    """Verify that qwen2.5vl:3b and its variants are detected as vision-capable."""
    assert is_vision_model("qwen2.5vl:3b") is True
    assert is_vision_model("qwen2.5vl:3b-q4_K_M") is True
    assert is_vision_model("qwen2.5-vl:3b") is True
    assert is_vision_model("qwen2.5-vl:7b") is True
    assert is_vision_model("qwen2.5vl") is True


def test_qwen3_not_flagged_as_vision_model():
    """Verify that text-only Qwen3:8b is not flagged as a vision model."""
    assert is_vision_model("qwen3:8b") is False
    assert is_vision_model("qwen3") is False
    assert is_vision_model("nemotron-3-nano:4b") is False


# ===========================================================================
# 2. Image Payload Creation & Cleaning
# ===========================================================================

def test_clean_base64_image_strips_data_uri():
    raw_b64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    with_prefix = f"data:image/png;base64,{raw_b64}"
    cleaned = clean_base64_image(with_prefix)
    assert cleaned == raw_b64
    assert not cleaned.startswith("data:")


def test_clean_base64_image_raw_valid():
    raw_b64 = base64.b64encode(b"test image bytes").decode("utf-8")
    cleaned = clean_base64_image(raw_b64)
    assert cleaned == raw_b64


# ===========================================================================
# 3. Multimodal Ollama Request Formatting
# ===========================================================================

def test_build_vision_prompt_structure():
    prompt = build_vision_prompt(query="Identify open windows")
    assert "observations" in prompt
    assert "windows" in prompt
    assert "coordinates" in prompt
    assert "Identify open windows" in prompt


# ===========================================================================
# 4. Screenshot -> Vision Pipeline
# ===========================================================================

def test_screenshot_to_vision_pipeline_mocked():
    sample_b64 = base64.b64encode(b"dummy image content").decode("utf-8")
    mock_resp_body = {
        "model": "qwen2.5vl:3b",
        "message": {
            "role": "assistant",
            "content": json.dumps({
                "observations": ["Observed desktop with active browser window"],
                "windows": [{"title": "Browser", "state": "active", "region": "center"}],
                "coordinates": [{"element": "Browser", "box_2d": [0, 0, 1080, 1920]}],
            }),
        },
        "eval_count": 50,
        "eval_duration": 1000000000,
    }

    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(mock_resp_body).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp
    mock_resp.__exit__.return_value = None

    with patch("urllib.request.urlopen", return_value=mock_resp):
        res = analyze_screenshot_sync(sample_b64, query="Find browser")
        assert res["success"] is True
        assert res["model"] == "qwen2.5vl:3b"
        assert len(res["structured"]["windows"]) == 1
        assert res["structured"]["windows"][0]["title"] == "Browser"


# ===========================================================================
# 5. Structured Visual Result Parsing & Validation
# ===========================================================================

def test_parse_structured_vision_output_json():
    raw = '```json\n{"observations": ["App open"], "windows": [{"title": "App"}], "coordinates": []}\n```'
    parsed = parse_structured_vision_output(raw)
    assert parsed["observations"] == ["App open"]
    assert len(parsed["windows"]) == 1
    assert parsed["windows"][0]["title"] == "App"


def test_parse_structured_vision_output_freeform_fallback():
    raw = "I see a blue screen with a start menu on the bottom."
    parsed = parse_structured_vision_output(raw)
    assert parsed["observations"] == [raw]
    assert parsed["windows"] == []
    assert parsed["raw_text"] == raw


# ===========================================================================
# 6. Malformed Image Handling
# ===========================================================================

def test_malformed_image_handling():
    with pytest.raises(ValueError, match="Invalid base64"):
        clean_base64_image("not-a-valid-base64-string!@#$%^&*")

    res = analyze_screenshot_sync("not-valid-base64")
    assert res["success"] is False
    assert res["reason"] == "malformed_image"
    assert "validation error" in res["error"]


# ===========================================================================
# 7. Oversized Image Handling
# ===========================================================================

def test_oversized_image_handling():
    oversized_bytes = b"X" * (MAX_IMAGE_BYTES + 1024)
    oversized_b64 = base64.b64encode(oversized_bytes).decode("utf-8")
    with pytest.raises(ValueError, match="Image exceeds maximum allowable size"):
        clean_base64_image(oversized_b64)


# ===========================================================================
# 8. Vision Model Unavailable Fallback
# ===========================================================================

def test_vision_model_unavailable_fallback():
    sample_b64 = base64.b64encode(b"test").decode("utf-8")
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("Connection refused")):
        res = analyze_screenshot_sync(sample_b64)
        assert res["success"] is False
        assert res["reason"] == "service_unavailable"
        assert "unavailable" in res["error"]


# ===========================================================================
# 9. Qwen3 Remains Default Model
# ===========================================================================

def test_qwen3_remains_primary_agent_model():
    """Verify that default model setting does not select vision model as general chat LLM."""
    default_model = DEFAULT_SETTINGS.get("default_model", "")
    # Vision models are dedicated analyzers, not default chat models
    assert not is_vision_model(default_model)


# ===========================================================================
# 10. Existing Computer Control Functionality
# ===========================================================================

def test_computer_control_tools_registered_and_safe():
    expected_tools = ["list_directory", "create_directory", "open_application", "open_url", "take_screenshot"]
    for t in expected_tools:
        assert t in TOOL_HANDLERS
        assert t in TOOL_TAGS

    # Test open_application rejects dangerous inputs
    open_app = OpenApplicationTool()
    res = asyncio.run(open_app.execute("cmd.exe"))
    assert res["exit_code"] == 1
    assert "not in the allowed application list" in res["error"]

    # Test open_url rejects dangerous schemes
    open_url = OpenUrlTool()
    res_url = asyncio.run(open_url.execute("file:///etc/passwd"))
    assert res_url["exit_code"] == 1
    assert "scheme 'file://' is not allowed" in res_url["error"]


# ===========================================================================
# 11. Memory System Integrity
# ===========================================================================

def test_memory_system_integrity():
    from src.tool_implementations import do_search_chats
    res = asyncio.run(do_search_chats({"query": "test"}))
    assert isinstance(res, (dict, str))


# ===========================================================================
# 12. Web Search Integrity
# ===========================================================================

def test_web_search_tool_integrity():
    from src.agent_tools.web_tools import WebSearchTool
    tool = WebSearchTool()
    assert hasattr(tool, "execute")
    assert "web_search" in TOOL_HANDLERS


# ===========================================================================
# 13. Reminder Tools Integrity
# ===========================================================================

def test_reminder_tools_integrity():
    from src.agent_tools.notification_tools import ScheduleReminderTool, SendNotificationTool
    assert "schedule_reminder" in TOOL_HANDLERS
    assert "send_notification" in TOOL_HANDLERS


# ===========================================================================
# 14. TTS / STT Service Integrity
# ===========================================================================

def test_speech_settings_integrity():
    assert "tts_enabled" in DEFAULT_SETTINGS
    assert "tts_provider" in DEFAULT_SETTINGS
    assert "tts_voice" in DEFAULT_SETTINGS
    assert "tts_speed" in DEFAULT_SETTINGS


# ===========================================================================
# 15. Host Bridge Client Integrity
# ===========================================================================

def test_host_bridge_client_integrity():
    from src.host_bridge_client import get_bridge_url, get_bridge_token, check_host_bridge_health_sync
    url = get_bridge_url()
    assert url.startswith("http://")
    token = get_bridge_token()
    assert isinstance(token, str)
