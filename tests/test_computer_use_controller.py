"""
tests/test_computer_use_controller.py

Comprehensive tests for the Safe Closed-Loop Computer-Use / GUI Agent Controller:
1. Controller initialization, defaults, and custom configs
2. Sensitive-action detection (payments, emails, deletions, passwords/OTPs, finance)
3. Action payload & coordinate bounds validation
4. Rejection of forbidden command strings (powershell, cmd, bash, regedit)
5. Successful closed-loop task execution (mocked end-to-end flow)
6. Action budget exhaustion
7. Iteration budget exhaustion
8. Wall-clock timeout stop
9. Screenshot capture failure handling & repeated failure stop
10. Vision analysis failure handling & no blind guessing
11. Verification failure handling & stop on repeated unverified state changes
12. Repeated identical failing action detection and stop
13. Sensitive-action confirmation gate (pause on sensitive, resume on confirm)
14. Plan-mode mutation restrictions
15. Sensitive data redaction in structured logs and step histories
"""

import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from src.computer_use_controller import (
    ALLOWED_COMPUTER_USE_ACTIONS,
    COMPUTER_USE_TOOL_SCHEMAS,
    ComputerUseConfig,
    ComputerUseController,
    ComputerUseState,
    StepRecord,
    is_deterministically_verifiable,
    is_goal_requiring_visual_confirmation,
    is_sensitive_action,
    normalize_action_payload,
    redact_sensitive_data,
    validate_action_payload,
)


# ===========================================================================
# 1. Config & Validation Tests
# ===========================================================================

def test_config_defaults_and_overrides():
    default_cfg = ComputerUseConfig()
    assert default_cfg.max_actions == 15
    assert default_cfg.max_iterations == 8
    assert default_cfg.max_duration_seconds == 180.0
    assert default_cfg.max_consecutive_failures == 3
    assert default_cfg.require_confirmation_for_sensitive is True
    assert default_cfg.planner_model == "qwen3:8b"
    assert default_cfg.vision_model == "qwen2.5vl:3b"
    assert default_cfg.planner_num_predict == 384
    assert default_cfg.planner_timeout_seconds == 90.0

    custom_cfg = ComputerUseConfig(
        max_actions=5,
        max_iterations=8,
        max_duration_seconds=60.0,
        max_consecutive_failures=2,
        require_confirmation_for_sensitive=False,
        planner_num_predict=2048,
    )
    assert custom_cfg.max_actions == 5
    assert custom_cfg.max_iterations == 8
    assert custom_cfg.max_duration_seconds == 60.0
    assert custom_cfg.max_consecutive_failures == 2
    assert custom_cfg.require_confirmation_for_sensitive is False
    assert custom_cfg.planner_num_predict == 2048


def test_allowed_action_set():
    assert "open_application" in ALLOWED_COMPUTER_USE_ACTIONS
    assert "open_url" in ALLOWED_COMPUTER_USE_ACTIONS
    assert "click" in ALLOWED_COMPUTER_USE_ACTIONS
    assert "double_click" in ALLOWED_COMPUTER_USE_ACTIONS
    assert "type_text" in ALLOWED_COMPUTER_USE_ACTIONS
    assert "press_key" in ALLOWED_COMPUTER_USE_ACTIONS
    assert "scroll" in ALLOWED_COMPUTER_USE_ACTIONS
    assert "take_screenshot" in ALLOWED_COMPUTER_USE_ACTIONS
    assert "finish" in ALLOWED_COMPUTER_USE_ACTIONS

    # Forbidden actions
    for forbidden in ["bash", "shell", "powershell", "cmd", "exec", "eval", "delete_file"]:
        assert forbidden not in ALLOWED_COMPUTER_USE_ACTIONS


# ===========================================================================
# 2. Sensitive Action Detection Tests
# ===========================================================================

def test_sensitive_action_detection():
    # Purchases / payments
    is_sens, reason = is_sensitive_action("click", {"x": 500, "y": 400}, "Buy the item and checkout with credit card")
    assert is_sens is True
    assert "purchase" in reason or "payment" in reason

    is_sens, reason = is_sensitive_action("type_text", {"text": "4532 1111 2222 3333"}, "Enter payment details")
    assert is_sens is True

    # Sending messages / forms / emails
    is_sens, reason = is_sensitive_action("click", {"x": 200, "y": 200}, "Send email to customer")
    assert is_sens is True
    assert "communication" in reason or "form" in reason

    # File/data deletion
    is_sens, reason = is_sensitive_action("click", {"x": 100, "y": 100}, "Delete all project files and wipe storage")
    assert is_sens is True
    assert "delet" in reason

    # Passwords & OTPs
    is_sens, reason = is_sensitive_action("type_text", {"text": "Secret123!"}, "Enter your account password")
    assert is_sens is True
    assert "password" in reason or "credential" in reason

    is_sens, reason = is_sensitive_action("type_text", {"text": "847291"}, "Submit your 2FA OTP code")
    assert is_sens is True
    assert "password" in reason or "OTP" in reason or "credential" in reason

    # Financial
    is_sens, reason = is_sensitive_action("click", {"x": 300, "y": 300}, "Transfer funds to another bank account")
    assert is_sens is True
    assert "banking" in reason or "financial" in reason

    # Safe non-sensitive actions
    is_sens, reason = is_sensitive_action("open_application", {"application": "notepad"}, "Open Notepad")
    assert is_sens is False
    assert reason is None

    is_sens, reason = is_sensitive_action("type_text", {"text": "Hello, I am Jarvis."}, "Type a welcome greeting")
    assert is_sens is False
    assert reason is None

    # A destructive term inside a negative user constraint is not intent.
    is_sens, reason = is_sensitive_action(
        "open_application",
        {"application": "calculator"},
        "Open a harmless app. Do not save, overwrite, or delete any files.",
    )
    assert is_sens is False
    assert reason is None

    # The actual action arguments remain protected even under a negative goal.
    is_sens, reason = is_sensitive_action(
        "press_key", {"key": "DELETE"}, "Open the app; do not delete any files."
    )
    assert is_sens is True
    assert "deleting data" in reason

    for destructive in ("Overwrite report.txt", "Run rm -rf on the project"):
        is_sens, reason = is_sensitive_action("click", {"x": 5, "y": 5}, destructive)
        assert is_sens is True
        assert "deleting data" in reason


# ===========================================================================
# 3. Action Payload Validation Tests
# ===========================================================================

def test_validate_action_payload():
    # Valid click
    valid, err = validate_action_payload("click", {"x": 400, "y": 300})
    assert valid is True
    assert err is None

    # Invalid coordinates
    valid, err = validate_action_payload("click", {"x": -10, "y": 300})
    assert valid is False
    assert "outside screen bounds" in err

    valid, err = validate_action_payload("click", {"x": "400", "y": 300})
    assert valid is False
    assert "must be integers" in err

    valid, err = validate_action_payload("click", {"x": True, "y": 300})
    assert valid is False
    assert "must be integers" in err

    # Valid type_text
    valid, err = validate_action_payload("type_text", {"text": "Hello world"})
    assert valid is True
    assert err is None

    # Invalid type_text (empty or oversized)
    valid, err = validate_action_payload("type_text", {"text": ""})
    assert valid is False

    valid, err = validate_action_payload("type_text", {"text": "A" * 501})
    assert valid is False
    assert "exceeds maximum" in err

    # Valid press_key
    valid, err = validate_action_payload("press_key", {"key": "ENTER"})
    assert valid is True

    valid, err = validate_action_payload("press_key", {"key": "INVALID_KEY_XYZ"})
    assert valid is False
    assert "not in the allowed virtual key list" in err

    # Valid scroll
    valid, err = validate_action_payload("scroll", {"direction": "down", "amount": 5})
    assert valid is True

    valid, err = validate_action_payload("scroll", {"direction": "sideways", "amount": 5})
    assert valid is False

    valid, err = validate_action_payload("scroll", {"direction": "up", "amount": 0})
    assert valid is False

    valid, err = validate_action_payload("scroll", {"direction": "up", "amount": 100})
    assert valid is False

    # Forbidden action name
    valid, err = validate_action_payload("run_powershell", {"cmd": "dir"})
    assert valid is False
    assert "not permitted" in err

    # Forbidden execution substring in params
    valid, err = validate_action_payload("type_text", {"text": "powershell -c evil"})
    assert valid is False
    assert "Forbidden command execution term" in err


# ===========================================================================
# 4. Data Redaction Tests
# ===========================================================================

def test_redact_sensitive_data():
    raw_str = "Authorization: Bearer secret_token_1234567890abcdef"
    redacted = redact_sensitive_data(raw_str)
    assert "secret_token_1234567890abcdef" not in redacted
    assert "Bearer [REDACTED]" in redacted

    raw_dict = {
        "thought": "Typing password: password='MySuperSecret123'",
        "params": {"password": "MySuperSecret123"},
    }
    redacted_dict = redact_sensitive_data(raw_dict)
    assert "MySuperSecret123" not in str(redacted_dict)
    assert "[REDACTED]" in str(redacted_dict)


# ===========================================================================
# 5. Full Mocked Closed-Loop Execution
# ===========================================================================

def test_successful_closed_loop_execution():
    async def _run():
        controller = ComputerUseController(
            ComputerUseConfig(
                max_actions=5,
                max_iterations=6,
                action_pause_seconds=0.0,
            )
        )

        # Mock screenshot capture
        dummy_screenshot = {
            "success": True,
            "file_path": "/workspace/jarvis/screenshots/test.jpg",
            "image_base64": "dummy_b64",
            "width": 1536,
            "height": 864,
        }

        # Sequence of planner decisions
        planner_sequence = [
            # 1. Open Notepad
            {
                "thought": "Notepad is not open. Opening Notepad.",
                "action": "open_application",
                "params": {"application": "notepad"},
                "expected_outcome": "Notepad window is open and visible.",
            },
            # 2. Click in Notepad
            {
                "thought": "Notepad is open. Clicking inside edit canvas.",
                "action": "click",
                "params": {"x": 400, "y": 300},
                "expected_outcome": "Notepad edit area is focused.",
            },
            # 3. Type text
            {
                "thought": "Notepad focused. Typing 'Jarvis closed-loop test'.",
                "action": "type_text",
                "params": {"text": "Jarvis closed-loop test"},
                "expected_outcome": "Text 'Jarvis closed-loop test' appears in Notepad.",
            },
            # 4. Press Enter
            {
                "thought": "Text typed. Pressing Enter.",
                "action": "press_key",
                "params": {"key": "ENTER"},
                "expected_outcome": "Cursor on newline below typed text.",
            },
            # 5. Finish
            {
                "thought": "Goal satisfied: Notepad open, text typed, Enter pressed, and text verified.",
                "action": "finish",
                "params": {"summary": "Completed Notepad closed-loop test successfully."},
                "expected_outcome": "Task completed.",
            },
        ]
        seq_idx = 0

        async def mock_capture():
            return dummy_screenshot

        async def mock_vision(image_base64, query=None, model=None, ollama_url=None, keep_alive=0):
            return {
                "success": True,
                "structured": {
                    "observations": ["Notepad window visible"],
                    "windows": [{"title": "Notepad", "state": "active"}],
                    "coordinates": [{"element": "edit_canvas", "box_2d": [100, 100, 600, 800]}],
                    "readable_text": ["Jarvis closed-loop test"],
                },
            }

        async def mock_verify(image_base64, expected_change, prior_context=None, observation_query=None, model=None, ollama_url=None, keep_alive=0):
            return {
                "success": True,
                "verification": {
                    "verified": True,
                    "confidence": 0.95,
                    "description": f"Verified expected change: {expected_change}",
                    "state_change_observed": True,
                },
                "observation": {
                    "observations": ["notepad open"],
                    "windows": [{"title": "Notepad", "state": "active"}],
                    "coordinates": [],
                    "readable_text": [],
                },
            }

        async def mock_plan(state, visual_data):
            nonlocal seq_idx
            p = planner_sequence[seq_idx]
            seq_idx += 1
            return {
                "success": True,
                "thought": p["thought"],
                "action": p["action"],
                "params": p["params"],
                "expected_outcome": p["expected_outcome"],
            }

        async def mock_execute(action, params):
            return {"exit_code": 0, "output": f"Executed {action} successfully"}

        with patch.object(controller, "capture_screenshot", side_effect=mock_capture), \
             patch("src.computer_use_controller.analyze_screenshot", side_effect=mock_vision), \
             patch("src.computer_use_controller.analyze_and_verify", side_effect=mock_verify), \
             patch.object(controller, "plan_next_action", side_effect=mock_plan), \
             patch.object(controller, "execute_single_action", side_effect=mock_execute):

            goal = "Open Notepad, type 'Jarvis closed-loop test', press Enter, and verify the text is visible."
            final_state = await controller.run(goal)

            assert final_state.status == "COMPLETED"
            assert final_state.action_count == 4
            assert final_state.iteration == 5
            assert "Completed Notepad closed-loop test" in final_state.summary
            assert len(final_state.history) == 5

    asyncio.run(_run())


# ===========================================================================
# 6. Safety & Budget Exhaustion Tests
# ===========================================================================

def test_action_budget_exhaustion():
    async def _run():
        controller = ComputerUseController(ComputerUseConfig(max_actions=2, max_iterations=10, action_pause_seconds=0.0))

        dummy_screenshot = {"success": True, "image_base64": "dummy", "width": 1536, "height": 864}

        with patch.object(controller, "capture_screenshot", return_value=dummy_screenshot), \
             patch("src.computer_use_controller.analyze_screenshot", return_value={"success": True, "structured": {}}), \
             patch("src.computer_use_controller.analyze_and_verify", return_value={"success": True, "verification": {"verified": True}}), \
             patch.object(controller, "plan_next_action", return_value={"success": True, "thought": "Clicking", "action": "click", "params": {"x": 100, "y": 100}, "expected_outcome": "Clicked"}), \
             patch.object(controller, "execute_single_action", return_value={"exit_code": 0, "output": "ok"}):

            state = await controller.run("Keep clicking")
            assert state.status == "STOPPED_BY_BUDGET"
            assert state.action_count == 2
            assert "actions budget" in state.error

    asyncio.run(_run())


def test_iteration_budget_exhaustion():
    async def _run():
        controller = ComputerUseController(ComputerUseConfig(max_actions=10, max_iterations=2, action_pause_seconds=0.0))

        dummy_screenshot = {"success": True, "image_base64": "dummy", "width": 1536, "height": 864}

        with patch.object(controller, "capture_screenshot", return_value=dummy_screenshot), \
             patch("src.computer_use_controller.analyze_screenshot", return_value={"success": True, "structured": {}}), \
             patch("src.computer_use_controller.analyze_and_verify", return_value={"success": True, "verification": {"verified": True}}), \
             patch.object(controller, "plan_next_action", return_value={"success": True, "thought": "Clicking", "action": "click", "params": {"x": 100, "y": 100}, "expected_outcome": "Clicked"}), \
             patch.object(controller, "execute_single_action", return_value={"exit_code": 0, "output": "ok"}):

            state = await controller.run("Keep iterating")
            assert state.status == "STOPPED_BY_BUDGET"
            assert "iterations" in state.error

    asyncio.run(_run())


def test_timeout_exhaustion():
    async def _run():
        # Set 0.05s timeout
        controller = ComputerUseController(ComputerUseConfig(max_duration_seconds=0.05, action_pause_seconds=0.1))

        dummy_screenshot = {"success": True, "image_base64": "dummy", "width": 1536, "height": 864}

        with patch.object(controller, "capture_screenshot", return_value=dummy_screenshot), \
             patch("src.computer_use_controller.analyze_screenshot", return_value={"success": True, "structured": {}}), \
             patch("src.computer_use_controller.analyze_and_verify", return_value={"success": True, "verification": {"verified": True}}), \
             patch.object(controller, "plan_next_action", return_value={"success": True, "thought": "Clicking", "action": "click", "params": {"x": 100, "y": 100}, "expected_outcome": "Clicked"}), \
             patch.object(controller, "execute_single_action", return_value={"exit_code": 0, "output": "ok"}):

            state = await controller.run("Long task")
            assert state.status == "STOPPED_BY_TIMEOUT"
            assert "wall-clock duration" in state.error

    asyncio.run(_run())


# ===========================================================================
# 7. Verification & Repeated Failure Tests
# ===========================================================================

def test_repeated_verification_failure_stops():
    async def _run():
        controller = ComputerUseController(ComputerUseConfig(max_consecutive_failures=2, action_pause_seconds=0.0))

        dummy_screenshot = {"success": True, "image_base64": "dummy", "width": 1536, "height": 864}

        with patch.object(controller, "capture_screenshot", return_value=dummy_screenshot), \
             patch("src.computer_use_controller.analyze_screenshot", return_value={"success": True, "structured": {}}), \
             patch("src.computer_use_controller.analyze_and_verify", return_value={"success": True, "verification": {"verified": False, "description": "Button did not react"}}), \
             patch.object(controller, "plan_next_action", return_value={"success": True, "thought": "Clicking button", "action": "click", "params": {"x": 100, "y": 200}, "expected_outcome": "Button clicked"}), \
             patch.object(controller, "execute_single_action", return_value={"exit_code": 0, "output": "Clicked"}):

            state = await controller.run("Click the submit button")
            # FIX #1: The controller now catches the duplicate failed action BEFORE
            # re-execution, so it may return FAILED instead of VERIFICATION_FAILED.
            assert state.status in ("VERIFICATION_FAILED", "FAILED")
            assert ("Verification failed repeatedly" in state.error
                    or "previously succeeded at OS level" in state.error)

    asyncio.run(_run())


def test_repeated_identical_failing_action_stops():
    async def _run():
        controller = ComputerUseController(ComputerUseConfig(max_repeated_identical_actions=2, action_pause_seconds=0.0))

        dummy_screenshot = {"success": True, "image_base64": "dummy", "width": 1536, "height": 864}

        with patch.object(controller, "capture_screenshot", return_value=dummy_screenshot), \
             patch("src.computer_use_controller.analyze_screenshot", return_value={"success": True, "structured": {}}), \
             patch("src.computer_use_controller.analyze_and_verify", return_value={"success": True, "verification": {"verified": False, "description": "Not working"}}), \
             patch.object(controller, "plan_next_action", return_value={"success": True, "thought": "Clicking same spot", "action": "click", "params": {"x": 100, "y": 100}, "expected_outcome": "Clicked"}), \
             patch.object(controller, "execute_single_action", return_value={"exit_code": 0, "output": "ok"}):

            state = await controller.run("Try click")
            assert state.status == "FAILED"
            # FIX #1: The controller now catches the duplicate OS-succeeded-but-
            # verification-failed action BEFORE re-execution.
            assert ("Repeated identical failing action" in state.error
                    or "previously succeeded at OS level" in state.error)

    asyncio.run(_run())


# ===========================================================================
# 8. Sensitive-Action Confirmation Gate Tests
# ===========================================================================

def test_sensitive_action_pauses_for_confirmation():
    async def _run():
        controller = ComputerUseController(ComputerUseConfig(require_confirmation_for_sensitive=True, action_pause_seconds=0.0))

        dummy_screenshot = {"success": True, "image_base64": "dummy", "width": 1536, "height": 864}

        with patch.object(controller, "capture_screenshot", return_value=dummy_screenshot), \
             patch("src.computer_use_controller.analyze_screenshot", return_value={"success": True, "structured": {}}), \
             patch.object(controller, "plan_next_action", return_value={"success": True, "thought": "Submitting payment", "action": "click", "params": {"x": 500, "y": 600}, "expected_outcome": "Payment submitted"}):

            # Initial run -> should pause for confirmation
            state = await controller.run("Checkout and buy item with credit card")
            assert state.status == "AWAITING_CONFIRMATION"
            assert state.pending_confirmation is not None
            assert state.pending_confirmation["action"] == "click"
            assert "purchase" in state.pending_confirmation["reason"] or "payment" in state.pending_confirmation["reason"]

    asyncio.run(_run())


def test_approved_pending_action_resumes_exact_action_without_replanning():
    controller = ComputerUseController(ComputerUseConfig(action_pause_seconds=0.0))
    action = {"action": "click", "params": {"x": 35, "y": 42}, "reason": "deletion"}
    state = ComputerUseState(
        goal="Delete the selected file",
        session_id="approval-resume",
        status="AWAITING_CONFIRMATION",
        start_time=time.time(),
        current_screenshot={"success": True, "image_base64": "prior-image", "file_path": "prior.jpg"},
        current_visual_data={"observations": ["Delete confirmation dialog is visible"]},
        pending_confirmation=action,
        approved_action=action,
    )
    fake_capture = AsyncMock(return_value={
        "success": True, "image_base64": "post-image", "file_path": "post.jpg",
        "width": 100, "height": 100,
    })
    fake_execute = AsyncMock(return_value={"exit_code": 0, "output": "clicked"})
    fake_verify = AsyncMock(return_value={
        "success": True,
        "verification": {"verified": True, "confidence": 1.0, "description": "Confirmed"},
        "observation": {"observations": ["Action confirmed"]},
    })

    with patch.object(controller, "capture_screenshot", fake_capture), \
         patch.object(controller, "execute_single_action", fake_execute), \
         patch.object(controller, "plan_next_action", new_callable=AsyncMock) as planner, \
         patch("src.computer_use_controller.analyze_and_verify", fake_verify):
        updated, should_continue = asyncio.run(controller.step(state, confirmed=True))

    assert should_continue is True
    fake_execute.assert_awaited_once_with("click", {"x": 35, "y": 42})
    planner.assert_not_awaited()
    fake_capture.assert_awaited_once()  # post-action verification only; no pre-action recapture
    assert updated.history[-1]["confirmation_status"] == "approved"
    assert updated.approved_action is None


def test_screenshot_image_is_analyzed_once_and_description_reaches_planner():
    async def _run():
        controller = ComputerUseController(ComputerUseConfig(action_pause_seconds=0.0))
        image_b64 = "the-real-bridge-image"
        handler = AsyncMock(return_value={
            "exit_code": 0,
            "image_base64": image_b64,
            "file_path": "/workspace/jarvis/screenshots/actual.jpg",
            "width": 800,
            "height": 600,
        })
        observations = {"observations": ["Calculator window is open"], "windows": [{"title": "Calculator"}], "readable_text": ["Calculator"]}
        async def mock_analyze(**kwargs):
            assert kwargs["image_base64"] == image_b64
            return {"success": True, "structured": observations, "model_load_seconds": 1.2, "model_inference_seconds": 2.3, "was_resident": False}

        async def mock_plan(state, visual_data):
            assert visual_data["observations"] == ["Calculator window is open"]
            assert "Calculator" in json.dumps(visual_data)
            return {"success": True, "action": "finish", "params": {"summary": "Calculator is open."}, "thought": "Observed Calculator"}

        with patch("src.computer_use_controller.get_computer_use_handlers", return_value={"take_screenshot": handler}), \
             patch("src.computer_use_controller.analyze_screenshot", side_effect=mock_analyze) as vision, \
             patch.object(controller, "plan_next_action", side_effect=mock_plan):
            state = await controller.run("Open a harmless application")

        assert state.status == "COMPLETED"
        assert handler.await_count == 1
        assert vision.await_count == 1
        assert state.total_model_load_seconds == 1.2
        assert state.total_model_inference_seconds == 2.3

    asyncio.run(_run())


# ===========================================================================
# 9. Plan Mode Restriction Tests
# ===========================================================================

def test_plan_mode_blocks_mutating_actions():
    async def _run():
        controller = ComputerUseController(ComputerUseConfig(plan_mode=True, action_pause_seconds=0.0))

        dummy_screenshot = {"success": True, "image_base64": "dummy", "width": 1536, "height": 864}

        with patch.object(controller, "capture_screenshot", return_value=dummy_screenshot), \
             patch("src.computer_use_controller.analyze_screenshot", return_value={"success": True, "structured": {}}), \
             patch.object(controller, "plan_next_action", return_value={"success": True, "thought": "Launching Notepad", "action": "open_application", "params": {"application": "notepad"}, "expected_outcome": "Notepad opens"}):

            state = await controller.run("Open Notepad")
            assert state.status == "STOPPED_BY_PLAN_MODE"
            assert "blocked in plan mode" in state.error

    asyncio.run(_run())


# ===========================================================================
# 10. Failure & Error Recovery Handling Tests
# ===========================================================================

def test_screenshot_unavailable_handling():
    async def _run():
        controller = ComputerUseController(ComputerUseConfig(max_consecutive_failures=2, action_pause_seconds=0.0))

        # Host bridge unavailable
        with patch.object(controller, "capture_screenshot", return_value={"success": False, "error": "Windows host bridge unavailable", "bridge_available": False}):
            state = await controller.run("Check screen")
            assert state.status == "FAILED"
            assert "Screenshot capture failed" in state.error

    asyncio.run(_run())


def test_malformed_vision_response_handling():
    async def _run():
        controller = ComputerUseController(ComputerUseConfig(max_consecutive_failures=2, action_pause_seconds=0.0))

        dummy_screenshot = {"success": True, "image_base64": "dummy", "width": 1536, "height": 864}

        with patch.object(controller, "capture_screenshot", return_value=dummy_screenshot), \
             patch("src.computer_use_controller.analyze_screenshot", return_value={"success": False, "error": "Malformed vision JSON output"}):

            state = await controller.run("Analyze desktop")
            assert state.status == "FAILED"
            assert "Vision analysis failed" in state.error

    asyncio.run(_run())


def test_password_otp_extraction_rejection():
    is_sens, reason = is_sensitive_action("type_text", {"text": "dummy"}, "Extract the password from the screen")
    assert is_sens is True
    assert "password" in reason.lower() or "credential" in reason.lower()

    is_sens, reason = is_sensitive_action("click", {"x": 100, "y": 100}, "Find OTP code from screen and submit")
    assert is_sens is True
    assert "otp" in reason.lower() or "password" in reason.lower() or "credential" in reason.lower()


# ===========================================================================
# 11. Lifecycle Instrumentation & Optimization Tests
# ===========================================================================

def test_model_lifecycle_instrumentation_records_all_fields():
    async def _run():
        controller = ComputerUseController(ComputerUseConfig(max_actions=2, max_iterations=2, action_pause_seconds=0.0))
        dummy_screenshot = {"success": True, "image_base64": "dummy", "width": 1536, "height": 864}

        with patch.object(controller, "capture_screenshot", return_value=dummy_screenshot), \
             patch("src.computer_use_controller.analyze_screenshot", return_value={"success": True, "structured": {"observations": ["notepad open"]}, "was_resident": True}), \
             patch("src.computer_use_controller.analyze_and_verify", return_value={"success": True, "verification": {"verified": True, "was_resident": True}}), \
             patch.object(controller, "plan_next_action", return_value={"success": True, "thought": "Done", "action": "finish", "params": {"summary": "all done"}}):

            state = await controller.run("Check screen")
            assert state.status == "COMPLETED"
            assert "model_invocations" in state.metrics
            assert len(state.model_invocations) >= 1
            for inv in state.model_invocations:
                assert "model" in inv
                assert "phase" in inv
                assert "start_time" in inv
                assert "end_time" in inv
                assert "latency" in inv
                assert "keep_alive" in inv
                assert "was_resident" in inv
                assert "total_task_elapsed_seconds" in inv

    asyncio.run(_run())


def test_planned_action_queue_reduces_planner_calls():
    async def _run():
        controller = ComputerUseController(ComputerUseConfig(max_actions=5, max_iterations=5, action_pause_seconds=0.0))
        dummy_screenshot = {"success": True, "image_base64": "dummy", "width": 1536, "height": 864}

        plan_call_count = 0
        async def mock_plan(state, visual_data):
            nonlocal plan_call_count
            plan_call_count += 1
            return {
                "success": True,
                "thought": "Opening notepad and typing text",
                "action": "open_application",
                "params": {"application": "notepad"},
                "expected_outcome": "Notepad is open",
                "planned_steps": [
                    {"action": "type_text", "params": {"text": "hello"}, "expected_outcome": "Text typed"},
                    {"action": "finish", "params": {"summary": "Done"}, "expected_outcome": "Completed"},
                ]
            }

        with patch.object(controller, "capture_screenshot", return_value=dummy_screenshot), \
             patch("src.computer_use_controller.analyze_screenshot", return_value={"success": True, "structured": {}}), \
             patch("src.computer_use_controller.analyze_and_verify", return_value={"success": True, "verification": {"verified": True}}), \
             patch.object(controller, "plan_next_action", side_effect=mock_plan), \
             patch.object(controller, "execute_single_action", return_value={"exit_code": 0, "output": "ok"}):

            state = await controller.run("Open notepad and type hello")
            assert state.status == "COMPLETED"
            assert state.action_count == 2
            # Planner was only called ONCE because planned_steps supplied the remaining actions!
            assert plan_call_count == 1
            assert state.iteration == 3

    asyncio.run(_run())


def test_planned_action_queue_clears_on_verification_failure():
    async def _run():
        controller = ComputerUseController(ComputerUseConfig(max_actions=5, max_iterations=5, max_consecutive_failures=3, action_pause_seconds=0.0))
        dummy_screenshot = {"success": True, "image_base64": "dummy", "width": 1536, "height": 864}

        plan_call_count = 0
        async def mock_plan(state, visual_data):
            nonlocal plan_call_count
            plan_call_count += 1
            if plan_call_count == 1:
                return {
                    "success": True,
                    "thought": "Batch plan",
                    "action": "open_application",
                    "params": {"application": "notepad"},
                    "expected_outcome": "Notepad open",
                    "planned_steps": [
                        {"action": "type_text", "params": {"text": "hello"}, "expected_outcome": "Text typed"},
                    ]
                }
            else:
                return {
                    "success": True,
                    "thought": "Recovering",
                    "action": "finish",
                    "params": {"summary": "Recovered"},
                }

        # Step 1 verification fails -> clears queue!
        with patch.object(controller, "capture_screenshot", return_value=dummy_screenshot), \
             patch("src.computer_use_controller.analyze_screenshot", return_value={"success": True, "structured": {}}), \
             patch("src.computer_use_controller.analyze_and_verify", return_value={"success": True, "verification": {"verified": False, "description": "Failed to open"}}), \
             patch.object(controller, "plan_next_action", side_effect=mock_plan), \
             patch.object(controller, "execute_single_action", return_value={"exit_code": 0, "output": "ok"}):

            state = await controller.run("Open notepad")
            # Because step 1 verification failed, planned_steps was cleared, and planner was re-invoked
            assert plan_call_count == 2
            assert state.status == "COMPLETED"

    asyncio.run(_run())


# ===========================================================================
# FIX #1/#2: Repeated-action prevention and launched-vs-foreground tests
# ===========================================================================

def test_open_app_os_succeeded_but_verification_failed_blocks_repeat():
    """FIX #1: If open_application succeeded at OS level but verification failed,
    the same action must NOT be executed again (to prevent duplicate launches)."""
    async def _run():
        controller = ComputerUseController(ComputerUseConfig(
            max_actions=5, max_iterations=5, max_consecutive_failures=3,
            action_pause_seconds=0.0,
        ))
        dummy_screenshot = {"success": True, "image_base64": "dummy", "width": 1536, "height": 864}

        plan_call_count = 0
        async def mock_plan(state, visual_data):
            nonlocal plan_call_count
            plan_call_count += 1
            # Planner always proposes the same open_application
            return {
                "success": True,
                "thought": "Open notepad",
                "action": "open_application",
                "params": {"application": "notepad"},
                "expected_outcome": "Notepad is open",
            }

        execute_call_count = 0
        original_execute = controller.execute_single_action
        async def counting_execute(action, params):
            nonlocal execute_call_count
            execute_call_count += 1
            # OS-level launch always "succeeds"
            return {"exit_code": 0, "output": "Launched Notepad"}

        with patch.object(controller, "capture_screenshot", return_value=dummy_screenshot), \
             patch("src.computer_use_controller.analyze_screenshot", return_value={"success": True, "structured": {"observations": ["IDE is active"]}}), \
             patch("src.computer_use_controller.analyze_and_verify", return_value={
                 "success": True,
                 "verification": {
                     "verified": False, "confidence": 0.8,
                     "description": "Notepad launched but Antigravity IDE remains the foreground window",
                     "state_change_observed": False, "active_window": "Antigravity IDE",
                 },
             }), \
             patch.object(controller, "plan_next_action", side_effect=mock_plan), \
             patch.object(controller, "execute_single_action", side_effect=counting_execute):

            state = await controller.run("Open Notepad")

        # Must FAIL, not loop forever
        assert state.status == "FAILED"
        # Must have executed open_application only ONCE (not twice+)
        assert execute_call_count == 1
        # Error must explain the launched-but-not-foreground situation
        assert "previously succeeded at OS level" in state.error
        assert "foreground" in state.error.lower() or "duplicates" in state.error.lower()

    asyncio.run(_run())


def test_exact_same_failed_action_not_repeated_before_execution():
    """FIX #1: The exact same action+params that failed verification must be blocked
    BEFORE execution, not just after N repetitions in history."""
    async def _run():
        controller = ComputerUseController(ComputerUseConfig(
            max_actions=5, max_iterations=5, max_consecutive_failures=5,
            action_pause_seconds=0.0,
        ))
        dummy_screenshot = {"success": True, "image_base64": "dummy", "width": 1536, "height": 864}

        # Pre-populate state with a failed action record
        state = ComputerUseState(
            goal="Open Notepad",
            session_id="test_session",
            status="RUNNING",
            start_time=time.time(),
            last_failed_action={"action": "open_application", "params": {"application": "notepad"}},
            last_failed_action_os_succeeded=True,
            consecutive_failures=1,
        )

        # Mock planner to propose the same action
        async def mock_plan(state, visual_data):
            return {
                "success": True,
                "thought": "Open notepad again",
                "action": "open_application",
                "params": {"application": "notepad"},
                "expected_outcome": "Notepad is open",
            }

        with patch.object(controller, "capture_screenshot", return_value=dummy_screenshot), \
             patch("src.computer_use_controller.analyze_screenshot", return_value={"success": True, "structured": {}}), \
             patch.object(controller, "plan_next_action", side_effect=mock_plan), \
             patch.object(controller, "execute_single_action") as mock_exec:

            state, should_continue = await controller.step(state)

        # The action must NOT have been executed
        mock_exec.assert_not_called()
        assert state.status == "FAILED"
        assert "previously succeeded at OS level" in state.error

    asyncio.run(_run())


def test_failed_action_with_parameter_variations_is_blocked():
    """Verify that equivalent actions with different key names or casing
    (e.g. app_name: Notepad.exe vs application: notepad) are recognized and blocked."""
    async def _run():
        controller = ComputerUseController(ComputerUseConfig(
            max_actions=5, max_iterations=5, max_consecutive_failures=5,
            action_pause_seconds=0.0,
        ))
        dummy_screenshot = {"success": True, "image_base64": "dummy", "width": 1536, "height": 864}

        # Pre-populate state with a failed action record using canonical key
        state = ComputerUseState(
            goal="Open Notepad",
            session_id="test_session",
            status="RUNNING",
            start_time=time.time(),
            last_failed_action={"action": "open_application", "params": {"application": "notepad"}},
            last_failed_action_os_succeeded=True,
            consecutive_failures=1,
        )

        # Mock planner to propose the same application using a variation (app_name: 'Notepad.exe')
        async def mock_plan(state, visual_data):
            return {
                "success": True,
                "thought": "Try launching with app_name variation",
                "action": "open_application",
                "params": {"app_name": "Notepad.exe"},
                "expected_outcome": "Notepad opens",
            }

        with patch.object(controller, "capture_screenshot", return_value=dummy_screenshot), \
             patch("src.computer_use_controller.analyze_screenshot", return_value={"success": True, "structured": {}}), \
             patch.object(controller, "plan_next_action", side_effect=mock_plan), \
             patch.object(controller, "execute_single_action") as mock_exec:

            state, should_continue = await controller.step(state)

        # Must be blocked by normalization before execution
        mock_exec.assert_not_called()
        assert state.status == "FAILED"
        assert "previously succeeded at OS level" in state.error

    asyncio.run(_run())


def test_state_change_evidence_permits_safe_retry_different_action():
    """FIX #1: After a failed action, if the planner proposes a DIFFERENT action,
    it should be allowed to proceed normally."""
    async def _run():
        controller = ComputerUseController(ComputerUseConfig(
            max_actions=5, max_iterations=5, max_consecutive_failures=3,
            action_pause_seconds=0.0,
        ))
        dummy_screenshot = {"success": True, "image_base64": "dummy", "width": 1536, "height": 864}

        state = ComputerUseState(
            goal="Open Notepad",
            session_id="test_session",
            status="RUNNING",
            start_time=time.time(),
            last_failed_action={"action": "open_application", "params": {"application": "notepad"}},
            last_failed_action_os_succeeded=True,
            consecutive_failures=1,
        )

        # Planner proposes a DIFFERENT action (click on taskbar)
        async def mock_plan(state, visual_data):
            return {
                "success": True,
                "thought": "Click taskbar to bring Notepad to foreground",
                "action": "click",
                "params": {"x": 500, "y": 1060},
                "expected_outcome": "Notepad is foreground",
            }

        with patch.object(controller, "capture_screenshot", return_value=dummy_screenshot), \
             patch("src.computer_use_controller.analyze_screenshot", return_value={"success": True, "structured": {}}), \
             patch("src.computer_use_controller.analyze_and_verify", return_value={
                 "success": True,
                 "verification": {
                     "verified": True, "confidence": 0.95,
                     "description": "Notepad is now the foreground window",
                 },
             }), \
             patch.object(controller, "plan_next_action", side_effect=mock_plan), \
             patch("src.computer_use_controller.get_screen_bounds", return_value=(1920, 1080)), \
             patch.object(controller, "execute_single_action", return_value={"exit_code": 0, "output": "Clicked"}):

            state, should_continue = await controller.step(state)

        # The different action should succeed
        assert state.status == "RUNNING"
        assert state.action_count == 1
        assert state.consecutive_failures == 0
        # last_failed_action should be cleared on success
        assert state.last_failed_action is None

    asyncio.run(_run())


def test_no_safe_foreground_recovery_produces_structured_failure():
    """FIX #2: When open_application succeeds at OS level but the application
    cannot be verified as foreground, the failure message must be structured."""
    async def _run():
        controller = ComputerUseController(ComputerUseConfig(
            max_actions=5, max_iterations=5, action_pause_seconds=0.0,
        ))
        dummy_screenshot = {"success": True, "image_base64": "dummy", "width": 1536, "height": 864}

        async def mock_plan(state, visual_data):
            return {
                "success": True,
                "thought": "Open notepad",
                "action": "open_application",
                "params": {"application": "notepad"},
                "expected_outcome": "Notepad is open",
            }

        with patch.object(controller, "capture_screenshot", return_value=dummy_screenshot), \
             patch("src.computer_use_controller.analyze_screenshot", return_value={"success": True, "structured": {}}), \
             patch("src.computer_use_controller.analyze_and_verify", return_value={
                 "success": True,
                 "verification": {
                     "verified": False, "confidence": 0.9,
                     "description": "Application launched but not foreground",
                 },
             }), \
             patch.object(controller, "plan_next_action", side_effect=mock_plan), \
             patch.object(controller, "execute_single_action", return_value={"exit_code": 0, "output": "Launched"}):

            state = await controller.run("Open Notepad")

        assert state.status == "FAILED"
        assert "No safe foreground recovery" in state.error

    asyncio.run(_run())


def test_successful_app_launch_and_foreground_verification():
    """FIX #2: When open_application succeeds and verification confirms the app
    is foreground, the flow continues normally."""
    async def _run():
        controller = ComputerUseController(ComputerUseConfig(
            max_actions=5, max_iterations=5, action_pause_seconds=0.0,
        ))
        dummy_screenshot = {"success": True, "image_base64": "dummy", "width": 1536, "height": 864}

        plan_call_count = 0
        async def mock_plan(state, visual_data):
            nonlocal plan_call_count
            plan_call_count += 1
            if plan_call_count == 1:
                return {
                    "success": True,
                    "thought": "Open notepad",
                    "action": "open_application",
                    "params": {"application": "notepad"},
                    "expected_outcome": "Notepad is open",
                }
            return {
                "success": True,
                "thought": "Done",
                "action": "finish",
                "params": {"summary": "Notepad opened successfully"},
            }

        with patch.object(controller, "capture_screenshot", return_value=dummy_screenshot), \
             patch("src.computer_use_controller.analyze_screenshot", return_value={"success": True, "structured": {}}), \
             patch("src.computer_use_controller.analyze_and_verify", return_value={
                 "success": True,
                 "verification": {
                     "verified": True, "confidence": 1.0,
                     "description": "Notepad is the active foreground window",
                     "active_window": "Notepad",
                 },
             }), \
             patch.object(controller, "plan_next_action", side_effect=mock_plan), \
             patch.object(controller, "execute_single_action", return_value={"exit_code": 0, "output": "Launched"}):

            state = await controller.run("Open Notepad")

        assert state.status == "COMPLETED"
        assert state.last_failed_action is None

    asyncio.run(_run())


def test_planner_instrumentation_reports_model_timings():
    """FIX #3: Model invocations must include timing instrumentation."""
    async def _run():
        controller = ComputerUseController(ComputerUseConfig(
            max_actions=5, max_iterations=5, action_pause_seconds=0.0,
        ))
        dummy_screenshot = {"success": True, "image_base64": "dummy", "width": 1536, "height": 864}

        with patch.object(controller, "capture_screenshot", return_value=dummy_screenshot), \
             patch("src.computer_use_controller.analyze_screenshot", return_value={
                 "success": True, "structured": {"observations": ["desktop"]},
                 "was_resident": True,
             }), \
             patch("src.computer_use_controller.analyze_and_verify", return_value={
                 "success": True,
                 "verification": {"verified": True, "was_resident": True},
             }), \
             patch.object(controller, "plan_next_action", return_value={
                 "success": True, "thought": "Done",
                 "action": "finish", "params": {"summary": "Done"},
             }):

            state = await controller.run("Check screen")

        assert state.status == "COMPLETED"
        assert "model_invocations" in state.metrics
        for inv in state.model_invocations:
            assert "model" in inv
            assert "phase" in inv
            assert "start_time" in inv
            assert "end_time" in inv
            assert "latency" in inv
            assert "keep_alive" in inv
            assert "was_resident" in inv
            assert inv["end_time"] >= inv["start_time"]
            assert inv["latency"] >= 0

    asyncio.run(_run())


def test_safety_restrictions_preserved_after_fixes():
    """FIX #6: All existing safety restrictions must remain intact after fixes."""
    # Forbidden actions still rejected
    for forbidden in ["bash", "shell", "powershell", "cmd", "exec", "eval", "delete_file"]:
        assert forbidden not in ALLOWED_COMPUTER_USE_ACTIONS

    # Forbidden exec terms in params still detected
    valid, err = validate_action_payload("type_text", {"text": "powershell -Command Get-Process"})
    assert valid is False
    assert "powershell" in err.lower()

    # Sensitive actions still detected
    is_sens, reason = is_sensitive_action("click", {"x": 500, "y": 400}, "Buy the item and checkout")
    assert is_sens is True

    # Coordinate validation still works
    valid, err = validate_action_payload("click", {"x": -1, "y": 100})
    assert valid is False

    # Key allowlist still enforced
    valid, err = validate_action_payload("press_key", {"key": "F13_FORBIDDEN"})
    assert valid is False


def test_planner_num_predict_is_bounded():
    """FIX #3: The planner must use num_predict to cap thinking chain length."""
    controller = ComputerUseController(ComputerUseConfig())
    # Verify num_predict is set in the planner call payload
    import unittest.mock
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "message": {"content": '{"action":"finish","params":{"summary":"done"}}', "tool_calls": []}
        }).encode("utf-8")
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        controller._call_planner_sync([{"role": "user", "content": "test"}])

        # Extract the payload that was sent
        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        sent_payload = json.loads(req.data.decode("utf-8"))
        assert "num_predict" in sent_payload.get("options", {})
        assert sent_payload["options"]["num_predict"] == 384


def test_custom_planner_num_predict_sent_to_ollama():
    """Verify custom planner_num_predict config is sent to Ollama."""
    controller = ComputerUseController(ComputerUseConfig(planner_num_predict=768))
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "message": {"content": '{"action":"finish","params":{"summary":"done"}}', "tool_calls": []}
        }).encode("utf-8")
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_resp

        controller._call_planner_sync([{"role": "user", "content": "test"}])

        call_args = mock_urlopen.call_args
        req = call_args[0][0]
        sent_payload = json.loads(req.data.decode("utf-8"))
        assert sent_payload["options"]["num_predict"] == 768


def test_planner_messages_contain_concise_planning_directive():
    """Verify build_planner_messages includes the concise reasoning directive."""
    controller = ComputerUseController(ComputerUseConfig())
    messages = controller.build_planner_messages(
        goal="Open Notepad and type test",
        history=[],
        visual_data={"observations": ["Desktop"], "windows": [], "coordinates": [], "readable_text": []},
    )
    assert len(messages) == 1
    prompt_text = messages[0]["content"]
    assert "Keep reasoning brief" in prompt_text


def test_failed_action_warning_in_planner_prompt():
    """FIX #2: When open_application failed verification with OS success,
    the planner prompt must include a warning to not repeat it."""
    controller = ComputerUseController(ComputerUseConfig())
    history = [
        {
            "iteration": 1,
            "proposed_action": {"action": "open_application", "params": {"application": "notepad"}},
            "verification": {"verified": False, "description": "Not foreground"},
            "tool_result": {"exit_code": 0, "output": "Launched"},
        }
    ]
    visual_data = {"observations": ["IDE is active"], "windows": [], "coordinates": [], "readable_text": []}
    messages = controller.build_planner_messages("Open Notepad", history, visual_data)
    prompt_text = messages[0]["content"]
    assert "IMPORTANT" in prompt_text
    assert "Do NOT call open_application" in prompt_text
    assert "notepad" in prompt_text.lower()


# ===========================================================================
# Selective / Fast-Path Deterministic Verification Tests (Strategy 1)
# ===========================================================================

def test_successful_open_application_can_bypass_vision():
    """1. Successful open_application with OS evidence bypasses vision."""
    tool_res = {"exit_code": 0, "success": True, "focused": True, "hwnd": 12345, "pid": 6789}
    is_det, desc = is_deterministically_verifiable("open_application", {"application": "notepad"}, tool_res, "Open Notepad")
    assert is_det is True
    assert "notepad" in desc.lower()
    assert "hwnd=12345" in desc


def test_successful_type_text_can_bypass_vision_when_visual_confirmation_not_required():
    """2. Successful type_text bypasses vision when visual confirmation is not required."""
    tool_res = {"exit_code": 0, "success": True, "message": "Typed 5 characters"}
    is_det, desc = is_deterministically_verifiable("type_text", {"text": "Hello"}, tool_res, "Open Notepad and type Hello")
    assert is_det is True
    assert "5 chars" in desc

    # Also works for write_text
    is_det, desc = is_deterministically_verifiable("write_text", {"text": "Hello World"}, tool_res, "Type Hello World")
    assert is_det is True


def test_successful_press_key_can_bypass_vision_when_visual_confirmation_not_required():
    """3. Successful press_key bypasses vision when visual confirmation is not required."""
    tool_res = {"exit_code": 0, "success": True, "message": "Key ENTER pressed"}
    is_det, desc = is_deterministically_verifiable("press_key", {"key": "ENTER"}, tool_res, "Press enter to submit")
    assert is_det is True
    assert "ENTER" in desc


def test_click_remains_visually_verified():
    """4. Click actions remain visually verified."""
    tool_res = {"exit_code": 0, "success": True, "message": "Clicked at (100, 200)"}
    is_det, desc = is_deterministically_verifiable("click", {"x": 100, "y": 200}, tool_res, "Click the button")
    assert is_det is False
    assert "visual verification" in desc.lower()


def test_double_click_remains_visually_verified():
    """5. Double_click actions remain visually verified."""
    tool_res = {"exit_code": 0, "success": True, "message": "Double clicked at (150, 250)"}
    is_det, desc = is_deterministically_verifiable("double_click", {"x": 150, "y": 250}, tool_res, "Double click the icon")
    assert is_det is False
    assert "visual verification" in desc.lower()


def test_scroll_remains_visually_verified():
    """6. Scroll actions remain visually verified."""
    tool_res = {"exit_code": 0, "success": True, "message": "Scrolled down 5 units"}
    is_det, desc = is_deterministically_verifiable("scroll", {"direction": "down", "amount": 5}, tool_res, "Scroll down to see results")
    assert is_det is False
    assert "visual verification" in desc.lower()


def test_goal_with_visual_confirmation_intermediate_open_bypasses_vision():
    """A. Goal requiring final visual confirmation allows intermediate open_application to use deterministic fast path."""
    goal = "Open Notepad, type 'Jarvis test', press Enter, and verify that the text is visible."
    assert is_goal_requiring_visual_confirmation(goal) is True
    tool_res = {"exit_code": 0, "success": True, "focused": True, "hwnd": 12345, "pid": 6789}
    is_det, desc = is_deterministically_verifiable("open_application", {"application": "notepad"}, tool_res, goal)
    assert is_det is True
    assert "notepad" in desc.lower()


def test_goal_with_visual_confirmation_intermediate_type_text_bypasses_vision():
    """B. Goal requiring final visual confirmation allows intermediate type_text to use deterministic fast path."""
    goal = "Open Notepad, type 'Jarvis test', press Enter, and verify that the text is visible."
    tool_res = {"exit_code": 0, "success": True, "message": "Typed 11 characters"}
    is_det, desc = is_deterministically_verifiable("type_text", {"text": "Jarvis test"}, tool_res, goal)
    assert is_det is True
    assert "11 chars" in desc


def test_goal_with_visual_confirmation_intermediate_press_key_bypasses_vision():
    """C. Goal requiring final visual confirmation allows intermediate press_key to use deterministic fast path."""
    goal = "Open Notepad, type 'Jarvis test', press Enter, and verify that the text is visible."
    tool_res = {"exit_code": 0, "success": True, "message": "Key ENTER pressed"}
    is_det, desc = is_deterministically_verifiable("press_key", {"key": "ENTER"}, tool_res, goal)
    assert is_det is True
    assert "ENTER" in desc


def test_final_checkpoint_invokes_exactly_one_vision_call():
    """D & E. Final checkpoint invokes exactly one vision call and completes on success."""
    controller = ComputerUseController(ComputerUseConfig())
    goal = "Open Notepad, type 'Jarvis test', and verify that the text is visible."
    state = ComputerUseState(goal=goal, session_id="test-final-verif", status="RUNNING", start_time=time.time())

    mock_handlers = {
        "take_screenshot": AsyncMock(return_value={"exit_code": 0, "image_base64": "dummy", "file_path": "/tmp/ss.png"}),
    }

    # Mock planner returning finish
    with patch("src.computer_use_controller.get_computer_use_handlers", return_value=mock_handlers), \
         patch.object(controller, "plan_next_action", new_callable=AsyncMock) as mock_plan, \
         patch("src.computer_use_controller.analyze_and_verify", new_callable=AsyncMock) as mock_analyze_and_verify, \
         patch("src.computer_use_controller.unload_model_sync") as mock_unload:

        mock_plan.return_value = {
            "success": True,
            "action": "finish",
            "params": {"summary": "Done"},
            "thought": "All steps done, finishing",
        }
        mock_analyze_and_verify.return_value = {
            "success": True,
            "verification": {"verified": True, "confidence": 0.95, "description": "Text is visible in Notepad."},
            "observation": {"observations": ["Notepad text visible"]},
        }
        state.current_visual_data = {"observations": ["Ready"], "windows": []}
        state.last_verification = {"verified": True}

        updated_state, should_continue = asyncio.run(controller.step(state))

        assert should_continue is False
        assert updated_state.status == "COMPLETED"
        assert updated_state.final_visual_verification_passed is True
        # Exactly ONE vision verification call was made for the final checkpoint
        assert mock_analyze_and_verify.call_count == 1
        # keep_alive=0 evicts vision after the inference; no duplicate unload request.
        assert mock_unload.call_count == 0


def test_final_vision_failure_triggers_recovery():
    """F. Final visual verification failure triggers recovery/re-planning."""
    controller = ComputerUseController(ComputerUseConfig(max_consecutive_failures=3))
    goal = "Open Notepad and verify that the text is visible."
    state = ComputerUseState(goal=goal, session_id="test-final-fail", status="RUNNING", start_time=time.time())

    mock_handlers = {
        "take_screenshot": AsyncMock(return_value={"exit_code": 0, "image_base64": "dummy", "file_path": "/tmp/ss.png"}),
    }

    with patch("src.computer_use_controller.get_computer_use_handlers", return_value=mock_handlers), \
         patch.object(controller, "plan_next_action", new_callable=AsyncMock) as mock_plan, \
         patch("src.computer_use_controller.analyze_and_verify", new_callable=AsyncMock) as mock_analyze_and_verify, \
         patch("src.computer_use_controller.unload_model_sync"):

        mock_plan.return_value = {
            "success": True,
            "action": "finish",
            "params": {"summary": "Done"},
            "thought": "Finishing",
        }
        # Final vision verification FAILS (text is missing)
        mock_analyze_and_verify.return_value = {
            "success": True,
            "verification": {"verified": False, "confidence": 0.2, "description": "Text is not visible in Notepad."},
            "observation": {"observations": ["Notepad empty"]},
        }
        state.current_visual_data = {"observations": ["Ready"], "windows": []}
        state.last_verification = {"verified": True}

        updated_state, should_continue = asyncio.run(controller.step(state))

        # Must NOT complete, should continue so planner can retry/fix
        assert should_continue is True
        assert updated_state.status == "RUNNING"
        assert updated_state.final_visual_verification_passed is False
        assert updated_state.consecutive_failures == 1
        assert "not visible" in updated_state.last_verification["description"].lower()


def test_nondeterministic_click_triggers_immediate_vision():
    """G. Non-deterministic click triggers immediate vision verification."""
    tool_res = {"exit_code": 0, "success": True, "message": "Clicked"}
    is_det, desc = is_deterministically_verifiable("click", {"x": 100, "y": 200}, tool_res, "Click button")
    assert is_det is False
    assert "visual verification" in desc.lower()


def test_sensitive_actions_remain_protected_from_vision_bypass():
    """H. Sensitive actions always require visual verification."""
    tool_res = {"exit_code": 0, "success": True}
    is_det, desc = is_deterministically_verifiable("type_text", {"text": "4532 1234 5678 9012"}, tool_res, "Enter credit card and checkout")
    assert is_det is False
    assert "sensitive" in desc.lower()


def test_planned_action_queue_runs_deterministic_actions_without_vision_until_finish():
    """I. Planned action queue executes deterministic actions with zero vision calls until final checkpoint."""
    controller = ComputerUseController(ComputerUseConfig())
    goal = "Open Notepad, type Hello, press Enter, and verify text is visible"
    state = ComputerUseState(goal=goal, session_id="test-queue", status="RUNNING", start_time=time.time())

    # Pre-populate queue with [type_text, press_key, finish]
    state.planned_action_queue = [
        {"action": "type_text", "params": {"text": "Hello"}, "thought": "Step 2: Type Hello"},
        {"action": "press_key", "params": {"key": "ENTER"}, "thought": "Step 3: Press Enter"},
        {"action": "finish", "params": {"summary": "Done"}, "thought": "Step 4: Finish"},
    ]
    state.current_visual_data = {"observations": ["Notepad ready"], "windows": []}
    state.last_verification = {"verified": True}

    mock_handlers = {
        "type_text": AsyncMock(return_value={"exit_code": 0, "success": True, "message": "Typed"}),
        "press_key": AsyncMock(return_value={"exit_code": 0, "success": True, "message": "Pressed"}),
        "take_screenshot": AsyncMock(return_value={"exit_code": 0, "image_base64": "dummy", "file_path": "/tmp/ss.png"}),
    }

    with patch("src.computer_use_controller.get_computer_use_handlers", return_value=mock_handlers), \
         patch("src.computer_use_controller.analyze_and_verify", new_callable=AsyncMock) as mock_analyze_and_verify, \
         patch("src.computer_use_controller.unload_model_sync"):

        mock_analyze_and_verify.return_value = {
            "success": True,
            "verification": {"verified": True, "confidence": 0.95, "description": "Text Hello is visible."},
            "observation": {"observations": ["Hello visible"]},
        }

        # Step 1: type_text from queue -> fast-path OS verify -> NO vision
        state, cont = asyncio.run(controller.step(state))
        assert cont is True
        assert mock_analyze_and_verify.call_count == 0

        # Step 2: press_key from queue -> fast-path OS verify -> NO vision
        state, cont = asyncio.run(controller.step(state))
        assert cont is True
        assert mock_analyze_and_verify.call_count == 0

        # Step 3: finish from queue -> triggers final visual checkpoint -> exactly 1 vision call!
        state, cont = asyncio.run(controller.step(state))
        assert cont is False
        assert state.status == "COMPLETED"
        assert mock_analyze_and_verify.call_count == 1


def test_no_visual_confirmation_goal_has_zero_final_vision_calls():
    """J. Goals with no explicit visual requirement complete finish with zero vision calls."""
    controller = ComputerUseController(ComputerUseConfig())
    goal = "Open Notepad and type Hello"
    assert is_goal_requiring_visual_confirmation(goal) is False
    state = ComputerUseState(goal=goal, session_id="test-no-vis", status="RUNNING", start_time=time.time())

    mock_handlers = {
        "take_screenshot": AsyncMock(return_value={"exit_code": 0, "image_base64": "dummy", "file_path": "/tmp/ss.png"}),
    }

    with patch("src.computer_use_controller.get_computer_use_handlers", return_value=mock_handlers), \
         patch.object(controller, "plan_next_action", new_callable=AsyncMock) as mock_plan, \
         patch("src.computer_use_controller.analyze_and_verify", new_callable=AsyncMock) as mock_analyze_and_verify:

        mock_plan.return_value = {
            "success": True,
            "action": "finish",
            "params": {"summary": "Done"},
            "thought": "Finishing without visual check",
        }
        state.current_visual_data = {"observations": ["Ready"], "windows": []}
        state.last_verification = {"verified": True}

        updated_state, should_continue = asyncio.run(controller.step(state))

        assert should_continue is False
        assert updated_state.status == "COMPLETED"
        # 0 Vision calls!
        assert mock_analyze_and_verify.call_count == 0


def test_failed_ambiguous_os_action_triggers_vision_recovery():
    """Failed or ambiguous OS action does not bypass vision / triggers recovery."""
    tool_res_fail = {"exit_code": 1, "error": "Failed to launch process"}
    is_det, desc = is_deterministically_verifiable("open_application", {"application": "notepad"}, tool_res_fail, "Open Notepad")
    assert is_det is False
    assert "non-zero exit code" in desc or "error" in desc

    tool_res_no_success = {"exit_code": 0, "success": False}
    is_det, desc = is_deterministically_verifiable("write_text", {"text": "hello"}, tool_res_no_success, "Type hello")
    assert is_det is False


def test_repeated_action_guards_remain_intact_with_deterministic_actions():
    """Repeated-action detection and limits remain fully functional."""
    controller = ComputerUseController(ComputerUseConfig(max_repeated_identical_actions=2))
    state = ComputerUseState(goal="Open Notepad", session_id="test-rep", status="RUNNING", start_time=time.time())

    state.history = [
        {
            "iteration": 1,
            "proposed_action": {"action": "open_application", "params": {"application": "notepad"}},
            "verification": {"verified": False},
        },
        {
            "iteration": 2,
            "proposed_action": {"action": "open_application", "params": {"application": "notepad"}},
            "verification": {"verified": False},
        },
    ]

    mock_handlers = {
        "open_application": AsyncMock(return_value={"exit_code": 0, "success": True}),
        "take_screenshot": AsyncMock(return_value={"exit_code": 0, "image_base64": "dummy", "file_path": "/tmp/s.png"}),
    }

    with patch("src.computer_use_controller.get_computer_use_handlers", return_value=mock_handlers), \
         patch.object(controller, "plan_next_action", new_callable=AsyncMock) as mock_plan:

        mock_plan.return_value = {
            "success": True,
            "action": "open_application",
            "params": {"application": "notepad"},
        }
        state.current_visual_data = {"observations": ["Desktop"], "windows": []}
        state.last_verification = {"verified": True}

        updated_state, should_continue = asyncio.run(controller.step(state))

        assert should_continue is False
        assert updated_state.status == "FAILED"
        assert "repeated identical" in updated_state.error.lower()


# ===========================================================================
# Planner Screenshot Tool Registration & Result-Handling Tests
# ===========================================================================

def test_take_screenshot_and_open_url_exposed_in_planner_tool_schemas():
    """Verify take_screenshot and open_url are exposed in COMPUTER_USE_TOOL_SCHEMAS with valid schemas."""
    schema_by_name = {s["function"]["name"]: s["function"] for s in COMPUTER_USE_TOOL_SCHEMAS}

    assert set(schema_by_name.keys()) == ALLOWED_COMPUTER_USE_ACTIONS

    ss_schema = schema_by_name["take_screenshot"]
    assert ss_schema["parameters"]["type"] == "object"
    assert ss_schema["parameters"]["properties"] == {}
    assert ss_schema["parameters"]["required"] == []

    url_schema = schema_by_name["open_url"]
    assert url_schema["parameters"]["type"] == "object"
    assert "url" in url_schema["parameters"]["properties"]
    assert url_schema["parameters"]["required"] == ["url"]

    alias_schema = schema_by_name["screenshot"]
    assert alias_schema["parameters"]["type"] == "object"
    assert alias_schema["parameters"]["properties"] == {}
    assert alias_schema["parameters"]["required"] == []

    valid_ss, err_ss = validate_action_payload("take_screenshot", {})
    assert valid_ss is True
    assert err_ss is None

    valid_alias, err_alias = validate_action_payload("screenshot", {})
    assert valid_alias is True
    assert err_alias is None

    # The exact model-facing tool set has a live handler that calls the
    # authenticated Host Bridge screenshot action.
    from src.computer_use_controller import get_computer_use_handlers
    handlers = get_computer_use_handlers()
    assert "take_screenshot" in handlers
    assert "screenshot" in handlers

    norm_act, norm_params = normalize_action_payload("screenshot", {})
    assert norm_act == "take_screenshot"

    valid_url, err_url = validate_action_payload("open_url", {"url": "https://example.com"})
    assert valid_url is True
    assert err_url is None

    for bad_url in [
        "file:///etc/passwd",
        "javascript:alert(1)",
        "data:text/html,<h1>x</h1>",
        "ftp://example.com/file",
        "not_a_url",
        "",
    ]:
        ok, err = validate_action_payload("open_url", {"url": bad_url})
        assert ok is False
        assert err is not None


def test_planner_can_request_screenshot_and_handler_is_invoked():
    """Verify the planner can request take_screenshot, the handler is invoked,
    its result reaches the planner in a usable form on the next turn, and no
    duplicate post-action screenshot capture occurs."""
    async def _run():
        controller = ComputerUseController(ComputerUseConfig(
            max_actions=5,
            max_iterations=5,
            action_pause_seconds=0.0,
        ))

        mock_ss_handler = AsyncMock(return_value={
            "exit_code": 0,
            "output": "Screenshot captured (1920x1080) and saved to /tmp/desktop_live.png",
            "file_path": "/tmp/desktop_live.png",
            "width": 1920,
            "height": 1080,
            "image_base64": "bGl2ZV9wbmdfYmFzZTY0",
        })
        mock_handlers = {"take_screenshot": mock_ss_handler}

        planner_prompts: List[str] = []
        turn = 0

        def fake_call_planner_sync(messages, timeout=120.0):
            nonlocal turn
            turn += 1
            planner_prompts.append(messages[0]["content"])
            if turn == 1:
                return {
                    "success": True,
                    "content": "",
                    "thinking": "Requesting a screenshot first.",
                    "tool_calls": [
                        {"function": {"name": "take_screenshot", "arguments": {}}}
                    ],
                    "latency_seconds": 0.01,
                    "was_resident": True,
                    "model": "qwen3:8b",
                }
            return {
                "success": True,
                "content": "",
                "thinking": "Screenshot received, finishing.",
                "tool_calls": [
                    {
                        "function": {
                            "name": "finish",
                            "arguments": {"summary": "Captured desktop screenshot showing VS Code and Terminal."},
                        }
                    }
                ],
                "latency_seconds": 0.01,
                "was_resident": True,
                "model": "qwen3:8b",
            }

        with patch("src.computer_use_controller.get_computer_use_handlers", return_value=mock_handlers), \
             patch.object(controller, "_call_planner_sync", side_effect=fake_call_planner_sync), \
             patch("src.computer_use_controller.analyze_screenshot", new_callable=AsyncMock) as mock_analyze_ss, \
             patch("src.computer_use_controller.analyze_and_verify", new_callable=AsyncMock) as mock_analyze_verify, \
             patch("src.computer_use_controller.unload_model_sync"):

            mock_analyze_ss.return_value = {
                "success": True,
                "structured": {
                    "observations": ["GNOME desktop with VS Code and Terminal open"],
                    "windows": [{"title": "Visual Studio Code", "state": "active"}],
                    "coordinates": [{"label": "Terminal", "x": 400, "y": 300}],
                    "readable_text": ["odysseus - Visual Studio Code"],
                },
            }
            mock_analyze_verify.return_value = {
                "success": True,
                "verification": {
                    "verified": True,
                    "confidence": 0.98,
                    "description": "Desktop screenshot verified showing VS Code and Terminal.",
                },
                "observation": {"observations": ["GNOME desktop with VS Code and Terminal open"]},
            }

            state = ComputerUseState(
                goal="Capture a desktop screenshot and summarize open windows",
                session_id="test-ss-flow",
                status="RUNNING",
                start_time=time.time(),
                # Pre-populated initial screenshot so Step 1 only invokes handler for the planner's take_screenshot action
                current_screenshot={
                    "success": True,
                    "file_path": "/tmp/initial.png",
                    "image_base64": "aW5pdGlhbF9iNjQ=",
                    "width": 1920,
                    "height": 1080,
                },
                current_visual_data={
                    "observations": ["Initial desktop state"],
                    "windows": [],
                    "coordinates": [],
                    "readable_text": [],
                },
                last_verification={"verified": True},
            )

            # Iteration 1: Planner requests take_screenshot -> handler is invoked once (no duplicate post-action capture)
            state, cont = await controller.step(state)
            assert cont is True
            assert mock_ss_handler.call_count == 1
            assert state.current_screenshot["file_path"] == "/tmp/desktop_live.png"
            assert state.history[-1]["proposed_action"]["action"] == "take_screenshot"
            assert state.history[-1]["verification"]["verified"] is True
            # Because the new screenshot image_base64 differed from the prior screenshot, vision analyzed it once
            assert mock_analyze_ss.call_count == 1

            # Iteration 2: Planner receives the screenshot result & observations in its prompt and calls finish
            state.final_visual_verification_passed = True
            state, cont = await controller.step(state)
            assert cont is False
            assert state.status == "COMPLETED"
            # No duplicate pre-step screenshot capture on Iteration 2
            assert mock_ss_handler.call_count == 1

            assert len(planner_prompts) == 2
            second_prompt = planner_prompts[1]
            assert "/tmp/desktop_live.png" in second_prompt
            assert "1920x1080" in second_prompt
            assert "GNOME desktop with VS Code and Terminal open" in second_prompt
            assert "Do NOT call take_screenshot() again" in second_prompt

    asyncio.run(_run())


def test_planner_screenshot_failure_is_reported_accurately():
    """Verify that when take_screenshot fails, the error is recorded accurately
    in step history, surfaced to the planner without fabricating screen content,
    and stops cleanly after max_consecutive_failures."""
    async def _run():
        controller = ComputerUseController(ComputerUseConfig(
            max_actions=5,
            max_iterations=5,
            max_consecutive_failures=2,
            action_pause_seconds=0.0,
        ))

        mock_ss_handler = AsyncMock(return_value={
            "exit_code": 1,
            "error": "take_screenshot error: Desktop screenshot capture failed on Linux (session=wayland): all capture methods failed",
            "bridge_available": True,
        })
        mock_handlers = {"take_screenshot": mock_ss_handler}

        state = ComputerUseState(
            goal="Take a screenshot",
            session_id="test-ss-fail",
            status="RUNNING",
            start_time=time.time(),
            current_screenshot={
                "success": True,
                "file_path": "/tmp/prior.png",
                "image_base64": "cHJpb3I=",
                "width": 1920,
                "height": 1080,
            },
            current_visual_data={"observations": ["Prior state"], "windows": [], "coordinates": [], "readable_text": []},
            last_verification={"verified": True},
        )

        with patch("src.computer_use_controller.get_computer_use_handlers", return_value=mock_handlers), \
             patch.object(controller, "plan_next_action", new_callable=AsyncMock) as mock_plan:

            mock_plan.return_value = {
                "success": True,
                "thought": "Take screenshot",
                "action": "take_screenshot",
                "params": {},
            }

            # First failure
            state, cont = await controller.step(state)
            assert cont is True
            assert state.consecutive_failures == 1
            assert state.history[-1]["verification"]["verified"] is False
            assert "all capture methods failed" in state.history[-1]["error"]
            assert "all capture methods failed" in state.history[-1]["verification"]["description"]

            # Verify planner prompt accurately reports the screenshot error and warns against fabrication
            msgs = controller.build_planner_messages(state.goal, state.history, state.current_visual_data)
            prompt_text = msgs[0]["content"]
            assert "all capture methods failed" in prompt_text
            assert "Do NOT fabricate a description of the screen" in prompt_text

    asyncio.run(_run())


def test_consecutive_take_screenshot_loop_is_stopped():
    """Verify that repeated consecutive take_screenshot actions do not loop indefinitely."""
    async def _run():
        controller = ComputerUseController(ComputerUseConfig(
            max_actions=10,
            max_iterations=10,
            max_repeated_identical_actions=2,
            action_pause_seconds=0.0,
        ))

        mock_ss_handler = AsyncMock(return_value={
            "exit_code": 0,
            "output": "Screenshot captured (1920x1080) and saved to /tmp/ss.png",
            "file_path": "/tmp/ss.png",
            "width": 1920,
            "height": 1080,
            "image_base64": "c2FtZV9iNjQ=",
        })
        mock_handlers = {"take_screenshot": mock_ss_handler}

        state = ComputerUseState(
            goal="Take screenshot",
            session_id="test-ss-loop",
            status="RUNNING",
            start_time=time.time(),
            current_screenshot={
                "success": True,
                "file_path": "/tmp/ss.png",
                "image_base64": "c2FtZV9iNjQ=",
                "width": 1920,
                "height": 1080,
            },
            current_visual_data={"observations": ["Desktop"], "windows": [], "coordinates": [], "readable_text": []},
            last_verification={"verified": True},
        )

        with patch("src.computer_use_controller.get_computer_use_handlers", return_value=mock_handlers), \
             patch.object(controller, "plan_next_action", new_callable=AsyncMock) as mock_plan:

            mock_plan.return_value = {
                "success": True,
                "thought": "Take screenshot again",
                "action": "take_screenshot",
                "params": {},
            }

            # Step 1: succeeds
            state, cont1 = await controller.step(state)
            assert cont1 is True
            # Step 2: succeeds
            state, cont2 = await controller.step(state)
            assert cont2 is True
            # Step 3: blocked by repeated take_screenshot guard
            state, cont3 = await controller.step(state)
            assert cont3 is False
            assert state.status == "STOPPED_BY_REPEATED_ACTION"
            assert "take_screenshot" in state.error
            assert mock_ss_handler.call_count == 2

    asyncio.run(_run())


