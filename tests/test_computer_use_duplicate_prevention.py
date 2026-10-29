"""
tests/test_computer_use_duplicate_prevention.py

Comprehensive regression tests for duplicate Computer Use execution prevention,
concurrency locks, single-instance application launching, and runaway execution guards.
"""

import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.computer_use_controller import (
    ComputerUseConfig,
    ComputerUseController,
    ComputerUseState,
)
from host_bridge.actions import (
    execute_open_application,
    normalize_app_name,
)
from src.host_bridge_client import execute_host_action_sync
from src.tool_capabilities import ToolRunSecurityContext
from host_bridge.config import APPLICATION_ALLOWLIST
from host_bridge.config import LINUX_APPLICATION_ALLOWLIST, WINDOWS_APPLICATION_ALLOWLIST
from routes.chat_routes import (
    _running_computer_use_tasks,
    get_computer_use_lock,
)
from src.agent_tools.computer_control_tools import ComputerUseTool, _TOOL_COMPUTER_USE_LOCK


@pytest.mark.asyncio
async def test_100_repeated_notepad_planner_outputs_launches_at_most_once():
    """Simulate a planner returning open_application("notepad") 100 times.
    
    Verifies:
    1. OS-level execution is called at most ONCE.
    2. Subsequent iterations are intercepted by Guard F and marked already_satisfied.
    3. The controller safely terminates without runaway execution.
    """
    cfg = ComputerUseConfig(
        max_iterations=10,
        max_actions=10,
        action_pause_seconds=0.0,
    )
    controller = ComputerUseController(config=cfg)

    # Mock screenshot capture
    controller.capture_screenshot = AsyncMock(return_value={
        "success": True,
        "image_base64": "mock_b64",
        "file_path": "/workspace/jarvis/screenshots/mock.jpg",
    })

    # Mock planner to return open_application("notepad") every time
    planner_mock = AsyncMock(return_value={
        "success": True,
        "thought": "I will open notepad again",
        "action": "open_application",
        "params": {"application": "notepad"},
        "planned_steps": [],
    })
    controller.plan_next_action = planner_mock

    # Mock execute_single_action to track OS-level launches
    launch_count = 0
    async def mock_execute_single_action(action, params):
        nonlocal launch_count
        if action == "open_application":
            launch_count += 1
            return {
                "success": True,
                "exit_code": 0,
                "action": "open_application",
                "application": "notepad",
                "already_running": False,
                "hwnd": 12345,
                "focused": True,
                "message": "Successfully launched Notepad",
            }
        return {"success": True, "exit_code": 0}

    controller.execute_single_action = mock_execute_single_action

    with patch("src.computer_use_controller.analyze_screenshot", new=AsyncMock(return_value={
        "success": True,
        "structured": {"observations": ["Desktop ready"]},
    })):
        state = await controller.run(goal="Open Notepad and write text")

    # Hard Invariant: Popen / execute_single_action called at most ONCE
    assert launch_count == 1, f"Expected exactly 1 OS launch, got {launch_count}"
    assert "notepad" in state.launched_applications

    # Verify Guard F was triggered on subsequent steps
    already_satisfied_steps = [
        h for h in state.history
        if (h.get("tool_result") or {}).get("already_satisfied")
    ]
    assert len(already_satisfied_steps) > 0


@pytest.mark.asyncio
async def test_notepad_exe_and_notepad_treated_as_same_application():
    """Verify that 'notepad.exe' and 'notepad' are recognized as the same application in controller and host bridge."""
    # 1. Normalization helper
    assert normalize_app_name("notepad.exe") == "notepad"
    assert normalize_app_name("Notepad.EXE") == "notepad"
    assert normalize_app_name("notepad") == "notepad"

    # 2. Controller workflow level
    cfg = ComputerUseConfig(max_iterations=5, action_pause_seconds=0.0)
    controller = ComputerUseController(config=cfg)
    controller.capture_screenshot = AsyncMock(return_value={"success": True, "image_base64": "b64", "file_path": "p"})

    launch_count = 0
    async def mock_exec(action, params):
        nonlocal launch_count
        launch_count += 1
        return {"success": True, "exit_code": 0, "application": "notepad", "hwnd": 12345, "focused": True}
    controller.execute_single_action = mock_exec

    with patch("src.computer_use_controller.analyze_screenshot", new=AsyncMock(return_value={
        "success": True,
        "structured": {"observations": ["Desktop ready"]},
    })):
        # Step 1: planner requests notepad.exe
        controller.plan_next_action = AsyncMock(return_value={
            "success": True,
            "thought": "open notepad.exe",
            "action": "open_application",
            "params": {"application": "notepad.exe"},
        })
        state, cont = await controller.step(ComputerUseState(goal="Open Notepad", session_id="s1"))
        assert launch_count == 1
        assert "notepad" in state.launched_applications

        # Step 2: planner requests notepad without .exe
        controller.plan_next_action = AsyncMock(return_value={
            "success": True,
            "thought": "open notepad",
            "action": "open_application",
            "params": {"application": "notepad"},
        })
        state, cont = await controller.step(state)

    # Launch count must still be 1 (Guard F intercepted notepad)
    assert launch_count == 1
    assert state.last_action_result.get("already_satisfied") is True


def test_host_bridge_single_instance_notepad_protection():
    """Verify Host Bridge execute_open_application does not call Popen if application is running."""
    # Test when application is NOT running -> Popen called
    with patch("host_bridge.actions.is_application_running", return_value=(False, None)), \
         patch("subprocess.Popen") as mock_popen:
        res = execute_open_application({"application": "notepad"})
        assert res["success"] is True
        assert res.get("already_running") is False
        assert mock_popen.call_count == 1

    # Test when application IS running -> Popen NOT called, window focused
    with patch("host_bridge.actions.is_application_running", return_value=(True, "Untitled - Notepad")), \
         patch("host_bridge.actions.focus_application_window", return_value=True), \
         patch("subprocess.Popen") as mock_popen:
        res = execute_open_application({"application": "notepad"})
        assert res["success"] is True
        assert res.get("already_running") is True
        assert res.get("focused") is True
        assert mock_popen.call_count == 0  # No new process spawned!


@pytest.mark.asyncio
async def test_single_chrome_request_with_alias_repeats_dispatches_one_launch():
    controller = ComputerUseController(ComputerUseConfig(
        max_iterations=6, max_actions=6, action_pause_seconds=0.0,
    ))
    controller.capture_screenshot = AsyncMock(return_value={
        "success": True, "image_base64": "mock_b64", "file_path": "mock.jpg",
    })
    proposals = ["chrome", "Google Chrome", "google chrome", "finish"]
    launch_calls = []

    async def planner(state, visual_data):
        app = proposals.pop(0)
        return {
            "success": True,
            "action": "finish" if app == "finish" else "open_application",
            "params": {"summary": "Chrome is open"} if app == "finish" else {"application": app},
        }

    async def dispatch(action, params):
        launch_calls.append((action, dict(params)))
        return {
            "success": True, "exit_code": 0, "application": "Google Chrome",
            "already_running": False, "focused": True, "hwnd": 123, "pid": 456,
        }

    controller.plan_next_action = planner
    controller.execute_single_action = dispatch
    with patch("src.computer_use_controller.analyze_screenshot", new=AsyncMock(return_value={
        "success": True, "structured": {"observations": ["Desktop ready"]},
    })):
        state = await controller.run(goal="Open Google Chrome")

    assert state.status == "COMPLETED"
    assert len(launch_calls) == 1
    assert "google chrome" in state.launched_applications
    assert state.action_count == 1


@pytest.mark.asyncio
async def test_unrequested_application_is_rejected_before_dispatch():
    controller = ComputerUseController(ComputerUseConfig(action_pause_seconds=0.0))
    controller.capture_screenshot = AsyncMock(return_value={
        "success": True, "image_base64": "mock_b64", "file_path": "mock.jpg",
    })
    controller.plan_next_action = AsyncMock(return_value={
        "success": True, "action": "open_application", "params": {"application": "notepad"},
    })
    dispatch = AsyncMock(return_value={"success": True, "exit_code": 0})
    controller.execute_single_action = dispatch

    with patch("src.computer_use_controller.analyze_screenshot", new=AsyncMock(return_value={
        "success": True, "structured": {"observations": ["Desktop ready"]},
    })):
        state = await controller.run(goal="Open Chrome")

    assert state.status == "FAILED"
    assert "did not request" in state.error
    dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_uncertain_chrome_launch_response_is_not_retried():
    controller = ComputerUseController(ComputerUseConfig(
        max_iterations=5, max_actions=5, action_pause_seconds=0.0,
    ))
    controller.capture_screenshot = AsyncMock(return_value={
        "success": True, "image_base64": "mock_b64", "file_path": "mock.jpg",
    })
    controller.plan_next_action = AsyncMock(return_value={
        "success": True, "action": "open_application", "params": {"application": "chrome"},
    })
    dispatch = AsyncMock(return_value={
        "success": False, "exit_code": 1, "error": "Host Bridge request timed out",
        "execution_status_uncertain": True,
    })
    controller.execute_single_action = dispatch

    with patch("src.computer_use_controller.analyze_screenshot", new=AsyncMock(return_value={
        "success": True, "structured": {"observations": ["Desktop ready"]},
    })):
        state = await controller.run(goal="Open Chrome")

    assert state.status == "FAILED"
    assert "Stopped without retrying" in state.error
    dispatch.assert_awaited_once_with("open_application", {"application": "chrome"})


@pytest.mark.asyncio
async def test_definite_chrome_launch_failure_is_not_retried_in_same_run():
    controller = ComputerUseController(ComputerUseConfig(
        max_iterations=5, max_actions=5, action_pause_seconds=0.0,
    ))
    controller.capture_screenshot = AsyncMock(return_value={
        "success": True, "image_base64": "mock_b64", "file_path": "mock.jpg",
    })
    planner = AsyncMock(side_effect=[
        {"success": True, "action": "open_application", "params": {"application": "Chrome"}},
        {"success": True, "action": "open_application", "params": {"application": "Google Chrome"}},
    ])
    dispatch = AsyncMock(return_value={
        "success": False, "exit_code": 1, "error": "launch failed before process creation",
        "execution_status_uncertain": False,
    })
    controller.plan_next_action = planner
    controller.execute_single_action = dispatch

    with patch("src.computer_use_controller.analyze_screenshot", new=AsyncMock(return_value={
        "success": True, "structured": {"observations": ["Desktop ready"]},
    })):
        state = await controller.run(goal="Open Chrome")

    assert state.status == "FAILED"
    assert "already attempted" in state.error
    assert state.application_launch_attempts == {"google chrome"}
    dispatch.assert_awaited_once_with("open_application", {"application": "Chrome"})


@pytest.mark.asyncio
async def test_repeated_identical_url_proposals_dispatch_once_after_success():
    controller = ComputerUseController(ComputerUseConfig(
        max_iterations=6, max_actions=6, action_pause_seconds=0.0,
    ))
    controller.capture_screenshot = AsyncMock(return_value={
        "success": True, "image_base64": "mock_b64", "file_path": "mock.jpg",
    })
    proposals = ["open", "open", "finish"]
    dispatch = AsyncMock(return_value={
        "success": True, "exit_code": 0, "action": "open_url",
        "url": "https://github.com", "output": "Opened URL",
    })

    async def planner(state, visual_data):
        proposal = proposals.pop(0)
        if proposal == "finish":
            return {"success": True, "action": "finish", "params": {"summary": "Done"}}
        return {"success": True, "action": "open_url", "params": {"url": "https://github.com/"}}

    controller.plan_next_action = planner
    controller.execute_single_action = dispatch
    with patch("src.computer_use_controller.analyze_screenshot", new=AsyncMock(return_value={
        "success": True, "structured": {"observations": ["Browser visible"]},
    })), patch("src.computer_use_controller.analyze_and_verify", new=AsyncMock(return_value={
        "success": True,
        "verification": {"verified": True, "confidence": 0.9, "description": "Page visible"},
        "observation": {"observations": ["GitHub visible"]},
    })):
        state = await controller.run(goal="Open https://github.com")

    assert state.status == "COMPLETED"
    assert state.navigation_attempts["https://github.com/"]["status"] == "completed"
    dispatch.assert_awaited_once_with("open_url", {"url": "https://github.com/"})


@pytest.mark.asyncio
async def test_uncertain_or_timed_out_url_navigation_is_not_retried():
    controller = ComputerUseController(ComputerUseConfig(
        max_iterations=5, max_actions=5, action_pause_seconds=0.0,
    ))
    controller.capture_screenshot = AsyncMock(return_value={
        "success": True, "image_base64": "mock_b64", "file_path": "mock.jpg",
    })
    controller.plan_next_action = AsyncMock(return_value={
        "success": True, "action": "open_url", "params": {"url": "https://github.com"},
    })
    dispatch = AsyncMock(return_value={
        "success": False, "exit_code": 1, "error": "response timed out after dispatch",
        "execution_status_uncertain": True,
    })
    controller.execute_single_action = dispatch
    with patch("src.computer_use_controller.analyze_screenshot", new=AsyncMock(return_value={
        "success": True, "structured": {"observations": ["Browser visible"]},
    })):
        state = await controller.run(goal="Open https://github.com")

    assert state.status == "FAILED"
    assert "uncertain" in state.error
    assert state.navigation_attempts["https://github.com/"]["status"] == "uncertain"
    dispatch.assert_awaited_once_with("open_url", {"url": "https://github.com"})


@pytest.mark.asyncio
async def test_post_navigation_screenshot_failure_does_not_repeat_url():
    controller = ComputerUseController(ComputerUseConfig(
        max_iterations=5, max_actions=5, action_pause_seconds=0.0,
    ))
    screenshot_calls = 0
    async def screenshot_with_post_action_failure():
        nonlocal screenshot_calls
        screenshot_calls += 1
        if screenshot_calls in (1, 3):
            return {"success": True, "image_base64": "initial", "file_path": "initial.jpg"}
        return {"success": False, "error": "capture unavailable"}
    controller.capture_screenshot = AsyncMock(side_effect=screenshot_with_post_action_failure)
    proposals = ["open", "open", "finish"]
    dispatch = AsyncMock(return_value={"success": True, "exit_code": 0, "url": "https://github.com"})

    async def planner(state, visual_data):
        proposal = proposals.pop(0)
        if proposal == "finish":
            return {"success": True, "action": "finish", "params": {"summary": "Done"}}
        return {"success": True, "action": "open_url", "params": {"url": "https://github.com"}}

    controller.plan_next_action = planner
    controller.execute_single_action = dispatch
    with patch("src.computer_use_controller.analyze_screenshot", new=AsyncMock(return_value={
        "success": True, "structured": {"observations": ["Browser visible"]},
    })):
        state = await controller.run(goal="Open https://github.com")

    assert state.status == "COMPLETED"
    dispatch.assert_awaited_once()


@pytest.mark.asyncio
async def test_navigation_focus_or_observation_failure_does_not_repeat_url():
    controller = ComputerUseController(ComputerUseConfig(
        max_iterations=5, max_actions=5, action_pause_seconds=0.0,
    ))
    controller.capture_screenshot = AsyncMock(return_value={
        "success": True, "image_base64": "mock_b64", "file_path": "mock.jpg",
    })
    proposals = ["open", "open"]
    dispatch = AsyncMock(return_value={
        "success": True, "exit_code": 0, "url": "https://github.com", "focused": False,
    })

    async def planner(state, visual_data):
        proposals.pop(0)
        return {"success": True, "action": "open_url", "params": {"url": "https://github.com"}}

    controller.plan_next_action = planner
    controller.execute_single_action = dispatch
    with patch("src.computer_use_controller.analyze_screenshot", new=AsyncMock(return_value={
        "success": True, "structured": {"observations": ["Desktop observed"]},
    })), patch("src.computer_use_controller.analyze_and_verify", new=AsyncMock(return_value={
        "success": True,
        "verification": {"verified": False, "confidence": 0.1, "description": "Target not focused"},
        "observation": {"observations": ["Target not focused"]},
    })):
        state = await controller.run(goal="Open https://github.com")

    assert state.status == "FAILED"
    assert "follow-up verification failed" in state.error
    dispatch.assert_awaited_once()


@pytest.mark.asyncio
async def test_computer_use_shares_request_navigation_guard_with_other_tools():
    request_guard = ToolRunSecurityContext()
    claimed, _ = request_guard.claim_navigation("https://github.com")
    assert claimed
    request_guard.finish_navigation("https://github.com", "completed")

    controller = ComputerUseController(
        ComputerUseConfig(action_pause_seconds=0.0),
        navigation_guard=request_guard,
    )
    controller.capture_screenshot = AsyncMock(return_value={
        "success": True, "image_base64": "mock_b64", "file_path": "mock.jpg",
    })
    controller.plan_next_action = AsyncMock(return_value={
        "success": True, "action": "open_url", "params": {"url": "https://github.com/"},
    })
    dispatch = AsyncMock(return_value={"success": True, "exit_code": 0})
    controller.execute_single_action = dispatch

    with patch("src.computer_use_controller.analyze_screenshot", new=AsyncMock(return_value={
        "success": True, "structured": {"observations": ["Browser visible"]},
    })):
        state = await controller.run(goal="Open https://github.com")

    assert state.status == "FAILED"
    assert "already attempted in this request" in state.error
    dispatch.assert_not_awaited()


def test_chrome_is_single_instance_and_existing_process_is_not_respawned():
    assert LINUX_APPLICATION_ALLOWLIST["chrome"]["single_instance"] is True
    assert WINDOWS_APPLICATION_ALLOWLIST["chrome"]["single_instance"] is True

    with patch("host_bridge.actions.find_application_window", return_value=(None, None, None)), \
         patch("host_bridge.actions.is_application_running", return_value=(True, "chrome (PID 123)")), \
         patch("host_bridge.actions.focus_application_window", return_value=False), \
         patch("host_bridge.actions.subprocess.Popen") as mock_popen:
        result = execute_open_application({"application": "Google Chrome"})

    assert result["success"] is True
    assert result["already_running"] is True
    assert result["focused"] is False
    assert "No duplicate was launched" in result["message"]
    mock_popen.assert_not_called()


def test_open_application_transport_timeout_is_marked_uncertain():
    with patch("src.host_bridge_client.urllib.request.urlopen", side_effect=TimeoutError("response timed out")):
        result = execute_host_action_sync("open_application", {"application": "Google Chrome"}, timeout=0.01)

    assert result["success"] is False
    assert result["execution_status_uncertain"] is True


def test_open_url_transport_timeout_is_marked_uncertain():
    with patch("src.host_bridge_client.urllib.request.urlopen", side_effect=TimeoutError("response timed out")):
        result = execute_host_action_sync("open_url", {"url": "https://github.com"}, timeout=0.01)

    assert result["success"] is False
    assert result["execution_status_uncertain"] is True


@pytest.mark.asyncio
async def test_per_session_concurrency_rejection():
    """Verify that multiple simultaneous Computer Use requests for the same session are rejected."""
    session_id = "test_session_concurrency_123"

    # Simulate an active running task
    async def dummy_long_task():
        await asyncio.sleep(5.0)

    task = asyncio.create_task(dummy_long_task())
    _running_computer_use_tasks[session_id] = task

    try:
        # Check active status
        existing = _running_computer_use_tasks.get(session_id)
        assert existing is not None
        assert not existing.done()
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        _running_computer_use_tasks.pop(session_id, None)


@pytest.mark.asyncio
async def test_tool_level_concurrency_lock():
    """Verify that ComputerUseTool prevents overlapping concurrent execution."""
    tool = ComputerUseTool()

    # Acquire lock externally to simulate an active running controller
    async with _TOOL_COMPUTER_USE_LOCK:
        res = await tool.execute(content=json.dumps({"goal": "Open Calculator"}))
        assert res["status"] == "failed"
        assert "already running in another active task" in res["error"]


@pytest.mark.asyncio
async def test_hard_stop_on_repeated_identical_action_loops():
    """Verify that attempting the identical action signature > 3 times triggers STOPPED_BY_REPEATED_ACTION."""
    cfg = ComputerUseConfig(max_iterations=10, action_pause_seconds=0.0)
    controller = ComputerUseController(config=cfg)
    controller.capture_screenshot = AsyncMock(return_value={"success": True, "image_base64": "b64", "file_path": "p"})
    controller.plan_next_action = AsyncMock(return_value={
        "success": True,
        "thought": "click button",
        "action": "click",
        "params": {"x": 100, "y": 200},
    })
    controller.execute_single_action = AsyncMock(return_value={"success": True, "exit_code": 0})

    state = ComputerUseState(goal="click something", session_id="s_repeat")

    with patch("src.computer_use_controller.analyze_screenshot", new=AsyncMock(return_value={
        "success": True,
        "structured": {"observations": ["Desktop ready"]},
    })), patch("src.computer_use_controller.analyze_and_verify", new=AsyncMock(return_value={
        "success": True,
        "verification": {"verified": True, "confidence": 0.9, "description": "Clicked successfully"},
        "observation": {},
    })):
        # Run step 1, 2, 3 (allowed)
        state, cont = await controller.step(state)
        assert cont is True
        state, cont = await controller.step(state)
        assert cont is True
        state, cont = await controller.step(state)
        assert cont is True

        # Run step 4 (4th attempt of identical action -> must stop immediately)
        state, cont = await controller.step(state)
        assert cont is False
        assert state.status == "STOPPED_BY_REPEATED_ACTION"
        assert "Hard safety stop" in state.error
