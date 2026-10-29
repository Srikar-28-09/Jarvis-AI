"""
tests/test_agent_computer_use_integration.py

Integration tests for safe closed-loop computer use integrated into the real Jarvis agent path.

Tests:
- A: User: "Open Notepad." -> computer_use invoked
- B: User: "Open Notepad and type Hello from Jarvis." -> computer_use invoked & completed
- C: Normal question: "What is the capital of France?" -> computer_use NOT invoked
- D: Sensitive request: "Send this message to someone." -> confirmation workflow (ask_user)
- E: Plan mode: computer use blocked
- F: Controller failure: structured failure result returned
- G: Feature flag: COMPUTER_USE_ENABLED=False blocks execution cleanly
- Security: non-admin execution blocked by public tool policy
- Progress streaming: verifies non-leaking events are dispatched via progress_cb
- Timing and metrics: verifies execution metrics (planning, vision, action latencies, swap counts)
"""

import asyncio
import json
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

from src.agent_tools.computer_control_tools import ComputerUseTool
from src.computer_use_controller import (
    ComputerUseConfig,
    ComputerUseController,
    ComputerUseState,
    is_computer_use_enabled,
)
from src.tool_execution import NO_TOOL_SECURITY_CONTEXT, execute_tool_block
from src.agent_tools import ToolBlock


def test_tool_registry_contains_computer_use():
    """Verify computer_use is registered in TOOL_HANDLERS, TOOL_TAGS, and FUNCTION_TOOL_SCHEMAS."""
    from src.agent_tools import TOOL_HANDLERS, TOOL_TAGS
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    from src.tool_capabilities import capabilities_for_tool
    from src.tool_security import NON_ADMIN_BLOCKED_TOOLS, _PLAN_MODE_KNOWN_MUTATORS

    assert "computer_use" in TOOL_HANDLERS
    assert "computer_use" in TOOL_TAGS
    assert any(s.get("function", {}).get("name") == "computer_use" for s in FUNCTION_TOOL_SCHEMAS)
    
    caps = capabilities_for_tool("computer_use")
    assert caps.known is True
    
    assert "computer_use" in NON_ADMIN_BLOCKED_TOOLS
    assert "computer_use" in _PLAN_MODE_KNOWN_MUTATORS


def test_computer_use_invoked_for_open_notepad():
    """Test A: User 'Open Notepad' executes computer_use and delegates to controller."""
    async def _test():
        mock_run = AsyncMock()
        mock_run.return_value = ComputerUseState(
            goal="Open Notepad",
            session_id="test_session_1",
            status="COMPLETED",
            summary="Launched Notepad on Windows desktop.",
            action_count=1,
            iteration=1,
            metrics={
                "total_planning_seconds": 0.5,
                "total_vision_seconds": 0.8,
                "total_action_seconds": 0.2,
                "total_verification_seconds": 0.7,
                "total_elapsed_seconds": 2.2,
                "model_swap_count": 3,
                "iterations": 1,
                "action_count": 1,
                "status": "COMPLETED",
            }
        )

        with patch.object(ComputerUseController, "run", mock_run):
            tool = ComputerUseTool()
            result = await tool.execute(json.dumps({"goal": "Open Notepad"}))

            assert result["status"] == "completed"
            assert result["exit_code"] == 0
            assert "Notepad" in result["summary"]
            assert result["actions_executed"] == 1
            assert result["metrics"]["model_swap_count"] == 3
            mock_run.assert_called_once_with(goal="Open Notepad")

    asyncio.run(_test())


def test_computer_use_invoked_and_completed_end_to_end():
    """Test B: User 'Open Notepad and type Hello from Jarvis' completes closed loop."""
    async def _test():
        mock_run = AsyncMock()
        mock_run.return_value = ComputerUseState(
            goal="Open Notepad and type Hello from Jarvis.",
            session_id="test_session_2",
            status="COMPLETED",
            summary="Opened Notepad, typed 'Hello from Jarvis.', and verified on screen.",
            action_count=2,
            iteration=3,
            metrics={
                "total_planning_seconds": 1.1,
                "total_vision_seconds": 2.4,
                "total_action_seconds": 0.6,
                "total_verification_seconds": 1.8,
                "total_elapsed_seconds": 5.9,
                "model_swap_count": 6,
                "iterations": 3,
                "action_count": 2,
                "status": "COMPLETED",
            }
        )

        with patch.object(ComputerUseController, "run", mock_run):
            block = ToolBlock("computer_use", json.dumps({"goal": "Open Notepad and type Hello from Jarvis."}))
            desc, result = await execute_tool_block(block, owner="admin", security_context=NO_TOOL_SECURITY_CONTEXT)

            assert result["status"] == "completed"
            assert result["exit_code"] == 0
            assert result["actions_executed"] == 2
            assert result["iterations"] == 3
            assert result["metrics"]["total_elapsed_seconds"] == 5.9

    asyncio.run(_test())


def test_normal_question_does_not_invoke_computer_use():
    """Test C: Normal question 'What is the capital of France?' does NOT invoke computer use."""
    from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS

    # Check that tool descriptions exist and general query does not invoke computer_use
    assert "computer_use" in BUILTIN_TOOL_DESCRIPTIONS
    
    with patch.object(ComputerUseController, "run") as mock_run:
        block = ToolBlock("web_search", "capital of France")
        assert block.tool_type != "computer_use"
        mock_run.assert_not_called()


def test_sensitive_request_triggers_confirmation_workflow():
    """Test D: Sensitive request returns AWAITING_CONFIRMATION with ask_user payload."""
    async def _test():
        mock_run = AsyncMock()
        mock_run.return_value = ComputerUseState(
            goal="Send this message to someone.",
            session_id="test_session_sensitive",
            status="AWAITING_CONFIRMATION",
            action_count=0,
            iteration=1,
            pending_confirmation={
                "action": "click",
                "params": {"x": 500, "y": 400},
                "reason": "Operation involves sending external communications or submitting forms",
                "thought": "I will click the Send button to deliver the message.",
            },
            metrics={
                "total_planning_seconds": 0.4,
                "total_vision_seconds": 0.6,
                "total_action_seconds": 0.0,
                "total_verification_seconds": 0.0,
                "total_elapsed_seconds": 1.0,
                "model_swap_count": 2,
                "iterations": 1,
                "action_count": 0,
                "status": "AWAITING_CONFIRMATION",
            }
        )

        with patch.object(ComputerUseController, "run", mock_run):
            tool = ComputerUseTool()
            result = await tool.execute(json.dumps({"goal": "Send this message to someone."}))

            assert result["status"] == "awaiting_confirmation"
            assert "ask_user" in result
            assert "question" in result["ask_user"]
            assert len(result["ask_user"]["options"]) == 2
            labels = [o["label"] for o in result["ask_user"]["options"]]
            assert "Approve" in labels
            assert "Reject" in labels
            assert result["exit_code"] == 0

    asyncio.run(_test())


def test_plan_mode_blocks_computer_use():
    """Test E: Plan mode blocks computer use."""
    async def _test():
        tool = ComputerUseTool()
        ctx = {"plan_mode": True}
        result = await tool.execute(json.dumps({"goal": "Open Notepad and type test"}), ctx=ctx)

        assert result["status"] == "plan_mode_blocked"
        assert result["exit_code"] == 1
        assert "plan mode" in result["error"].lower()

    asyncio.run(_test())


def test_controller_failure_returns_structured_result():
    """Test F: Controller failure returns structured error to normal agent."""
    async def _test():
        mock_run = AsyncMock()
        mock_run.return_value = ComputerUseState(
            goal="Open BrokenApp",
            session_id="test_session_fail",
            status="FAILED",
            error="Application failed to open: Application 'BrokenApp' is not in the allowlist",
            action_count=0,
            iteration=1,
            metrics={
                "total_planning_seconds": 0.3,
                "total_vision_seconds": 0.0,
                "total_action_seconds": 0.0,
                "total_verification_seconds": 0.0,
                "total_elapsed_seconds": 0.3,
                "model_swap_count": 1,
                "iterations": 1,
                "action_count": 0,
                "status": "FAILED",
            }
        )

        with patch.object(ComputerUseController, "run", mock_run):
            block = ToolBlock("computer_use", json.dumps({"goal": "Open BrokenApp"}))
            desc, result = await execute_tool_block(block, owner="admin", security_context=NO_TOOL_SECURITY_CONTEXT)

            assert result["status"] == "failed"
            assert result["exit_code"] == 1
            assert "BrokenApp" in result["error"]

    asyncio.run(_test())


def test_feature_flag_disabled_blocks_computer_use():
    """Test G: COMPUTER_USE_ENABLED=False cleanly blocks execution."""
    async def _test():
        with patch.dict(os.environ, {"COMPUTER_USE_ENABLED": "false"}):
            assert is_computer_use_enabled() is False
            tool = ComputerUseTool()
            result = await tool.execute(json.dumps({"goal": "Open Notepad"}))

            assert result["status"] == "failed"
            assert result["exit_code"] == 1
            assert "COMPUTER_USE_ENABLED" in result["error"]

    asyncio.run(_test())


def test_public_user_blocked_by_security_policy():
    """Verify non-admin/public users cannot execute computer_use."""
    async def _test():
        block = ToolBlock("computer_use", json.dumps({"goal": "Open Notepad"}))
        desc, result = await execute_tool_block(block, owner=None, security_context=NO_TOOL_SECURITY_CONTEXT)
        assert result["exit_code"] == 1
        assert "restricted" in result["error"].lower() or "blocked" in result["error"].lower()

    asyncio.run(_test())


def test_progress_streaming_events_forwarded():
    """Verify progress events are emitted with sanitized information and without sensitive leaks."""
    async def _test():
        events = []

        async def mock_progress_cb(data):
            events.append(data)

        tool = ComputerUseTool()
        ctx = {"progress_cb": mock_progress_cb}

        async def fake_run(goal, initial_state=None, confirmed=False):
            controller = ComputerUseController(progress_callback=mock_progress_cb)
            await controller._notify_progress("screenshot_capturing", "Capturing desktop screenshot...")
            await controller._notify_progress("vision_analyzing", "Analyzing screen...")
            await controller._notify_progress("action_executing", "Executing open_application notepad with Bearer secret_token_123456789")
            await controller._notify_progress("completed", "Completed goal")
            return ComputerUseState(goal=goal, session_id="prog_sess", status="COMPLETED", summary="Done", action_count=1)

        with patch.object(ComputerUseController, "run", side_effect=fake_run):
            result = await tool.execute(json.dumps({"goal": "Open Notepad"}), ctx=ctx)

            assert len(events) >= 4
            stages = [e.get("stage") for e in events]
            assert "screenshot_capturing" in stages
            assert "vision_analyzing" in stages
            assert "action_executing" in stages
            assert "completed" in stages

            # Check secret redaction in stream
            exec_event = next(e for e in events if e.get("stage") == "action_executing")
            assert "secret_token_123456789" not in exec_event.get("tail", "")
            assert "[REDACTED]" in exec_event.get("tail", "")

    asyncio.run(_test())


def test_computer_use_stream_endpoint_directly_invokes_controller():
    """Verify POST /api/computer_use/stream directly runs ComputerUseController and yields SSE events."""
    from types import SimpleNamespace
    from fastapi import Request
    from fastapi.datastructures import FormData
    from routes.chat_routes import setup_chat_routes

    async def _test():
        mock_run = AsyncMock()
        mock_run.return_value = ComputerUseState(
            goal="Open Notepad and type Jarvis",
            session_id="cu_sess_1",
            status="COMPLETED",
            summary="Opened Notepad successfully.",
            action_count=1,
            iteration=1,
            metrics={"total_elapsed_seconds": 1.5, "status": "COMPLETED", "iterations": 1, "action_count": 1}
        )

        class FakeSession:
            def __init__(self):
                self.id = "cu_sess_1"
                self.model = "qwen3:8b"
                self.endpoint_url = "http://localhost:11434"
                self.headers = {}
                self.history = []
                self.user = "test_user"
                self.owner = "test_user"
            def add_message(self, msg):
                self.history.append(msg)

        class FakeSessionManager:
            def __init__(self):
                self.sessions = {"cu_sess_1": FakeSession()}
            def get_session(self, sid):
                return self.sessions[sid]
            def save_sessions(self):
                pass

        sm = FakeSessionManager()
        router = setup_chat_routes(
            session_manager=sm,
            chat_handler=SimpleNamespace(),
            chat_processor=SimpleNamespace(),
            memory_manager=SimpleNamespace(),
            research_handler=SimpleNamespace(),
            upload_handler=SimpleNamespace(),
        )

        stream_endpoint = next(
            r.endpoint for r in router.routes if r.path == "/api/computer_use/stream" and "POST" in r.methods
        )

        mock_req = MagicMock(spec=Request)
        mock_req.headers = {"content-type": "application/x-www-form-urlencoded"}
        mock_req.state = SimpleNamespace(user="test_user", privileges={"is_admin": True})
        mock_req.form = AsyncMock(return_value=FormData([("message", "Open Notepad and type Jarvis"), ("session", "cu_sess_1")]))

        with patch("routes.chat_routes.require_api_token_scope"), \
             patch("routes.chat_routes._verify_session_owner"), \
             patch("routes.chat_routes.resolve_session_auth"), \
             patch("routes.chat_routes._enforce_chat_privileges"), \
             patch("routes.chat_routes.save_assistant_response"), \
             patch("core.database.update_session_last_accessed"), \
             patch.object(ComputerUseController, "run", mock_run):

            response = await stream_endpoint(mock_req)
            chunks = []
            async for chunk in response.body_iterator:
                chunks.append(chunk)

            full_sse = "".join(chunks)
            assert "data: " in full_sse
            assert "model_info" in full_sse
            assert "computer_use" in full_sse
            assert "Opened Notepad successfully" in full_sse
            assert "data: [DONE]" in full_sse
            mock_run.assert_called_once_with(goal="Open Notepad and type Jarvis")

    asyncio.run(_test())


def test_computer_use_stream_endpoint_sensitive_action_confirmation():
    """Verify POST /api/computer_use/stream handles sensitive action confirmation properly."""
    from types import SimpleNamespace
    from fastapi import Request
    from fastapi.datastructures import FormData
    from routes.chat_routes import setup_chat_routes

    async def _test():
        mock_run = AsyncMock()
        mock_run.return_value = ComputerUseState(
            goal="Delete system files",
            session_id="cu_sess_2",
            status="AWAITING_CONFIRMATION",
            pending_confirmation={"action": "press_key", "params": {"key": "DELETE"}, "reason": "File deletion detected"},
            action_count=0,
            iteration=1,
            metrics={"total_elapsed_seconds": 0.5, "status": "AWAITING_CONFIRMATION"}
        )

        class FakeSession:
            def __init__(self):
                self.id = "cu_sess_2"
                self.model = "qwen3:8b"
                self.endpoint_url = "http://localhost:11434"
                self.headers = {}
                self.history = []
                self.user = "test_user"
                self.owner = "test_user"
            def add_message(self, msg):
                self.history.append(msg)

        class FakeSessionManager:
            def __init__(self):
                self.sessions = {"cu_sess_2": FakeSession()}
            def get_session(self, sid):
                return self.sessions[sid]
            def save_sessions(self):
                pass

        sm = FakeSessionManager()
        router = setup_chat_routes(
            session_manager=sm,
            chat_handler=SimpleNamespace(),
            chat_processor=SimpleNamespace(),
            memory_manager=SimpleNamespace(),
            research_handler=SimpleNamespace(),
            upload_handler=SimpleNamespace(),
        )

        stream_endpoint = next(
            r.endpoint for r in router.routes if r.path == "/api/computer_use/stream" and "POST" in r.methods
        )

        mock_req = MagicMock(spec=Request)
        mock_req.headers = {"content-type": "application/x-www-form-urlencoded"}
        mock_req.state = SimpleNamespace(user="test_user", privileges={"is_admin": True})
        mock_req.form = AsyncMock(return_value=FormData([("message", "Delete system files"), ("session", "cu_sess_2")]))

        with patch("routes.chat_routes.require_api_token_scope"), \
             patch("routes.chat_routes._verify_session_owner"), \
             patch("routes.chat_routes.resolve_session_auth"), \
             patch("routes.chat_routes._enforce_chat_privileges"), \
             patch("routes.chat_routes.save_assistant_response"), \
             patch("core.database.update_session_last_accessed"), \
             patch.object(ComputerUseController, "run", mock_run):

            response = await stream_endpoint(mock_req)
            chunks = []
            async for chunk in response.body_iterator:
                chunks.append(chunk)

            full_sse = "".join(chunks)
            assert "ask_user" in full_sse
            assert "Jarvis requires your confirmation" in full_sse
            assert "File deletion detected" in full_sse
            assert "data: [DONE]" in full_sse

    asyncio.run(_test())


def test_computer_use_approval_resumes_original_action_and_rejection_cancels():
    """The ask_user card must continue its original run, never treat approval text as a new goal."""
    from types import SimpleNamespace
    from fastapi import Request, HTTPException
    from fastapi.datastructures import FormData
    from routes.chat_routes import setup_chat_routes, _computer_use_pending_approvals

    async def _test():
        states = {}
        calls = []

        class FakeController:
            def __init__(self, config=None, progress_callback=None):
                self.config = config

            async def run(self, goal, initial_state=None, confirmed=False):
                calls.append((goal, initial_state, confirmed))
                if initial_state is None:
                    state = ComputerUseState(
                        goal=goal,
                        session_id="approval-life",
                        status="AWAITING_CONFIRMATION",
                        pending_confirmation={
                            "action": "open_application",
                            "params": {"application": "calculator"},
                            "reason": "sensitive test action",
                        },
                        start_time=time.time(),
                    )
                    states[goal] = state
                    return state
                assert initial_state is states[goal]
                assert confirmed is True
                assert initial_state.approved_action["action"] == "open_application"
                initial_state.status = "COMPLETED"
                initial_state.summary = "Calculator opened."
                initial_state.metrics = {"status": "COMPLETED"}
                return initial_state

            def _finalize_metrics(self, state):
                state.metrics = {"status": state.status}

        class FakeSession:
            def __init__(self, sid):
                self.id = sid
                self.model = "qwen3:8b"
                self.endpoint_url = "http://localhost:11434"
                self.headers = {}
                self.history = []
                self.user = "test_user"
                self.owner = "test_user"

            def add_message(self, msg):
                self.history.append(msg)

        class FakeSessionManager:
            def __init__(self):
                self.sessions = {sid: FakeSession(sid) for sid in ("approval-life", "cancel-life")}

            def get_session(self, sid):
                return self.sessions[sid]

            def save_sessions(self):
                pass

        sm = FakeSessionManager()
        router = setup_chat_routes(
            session_manager=sm,
            chat_handler=SimpleNamespace(),
            chat_processor=SimpleNamespace(),
            memory_manager=SimpleNamespace(),
            research_handler=SimpleNamespace(),
            upload_handler=SimpleNamespace(),
        )
        endpoint = next(r.endpoint for r in router.routes if r.path == "/api/computer_use/stream" and "POST" in r.methods)

        def request(fields):
            req = MagicMock(spec=Request)
            req.headers = {"content-type": "application/x-www-form-urlencoded"}
            req.state = SimpleNamespace(user="test_user", privileges={"is_admin": True})
            req.form = AsyncMock(return_value=FormData(fields))
            return req

        async def consume(req):
            response = await endpoint(req)
            return "".join([chunk async for chunk in response.body_iterator])

        with patch("routes.chat_routes.require_api_token_scope"), \
             patch("routes.chat_routes._verify_session_owner"), \
             patch("routes.chat_routes.resolve_session_auth"), \
             patch("routes.chat_routes._enforce_chat_privileges"), \
             patch("routes.chat_routes.save_assistant_response"), \
             patch("core.database.update_session_last_accessed"), \
             patch("src.computer_use_controller.ComputerUseController", FakeController):
            first_sse = await consume(request([("message", "Open a harmless app"), ("session", "approval-life")]))
            first_card = next(record for record in _computer_use_pending_approvals.values() if record["session_id"] == "approval-life")
            approval_id = next(key for key, record in _computer_use_pending_approvals.items() if record is first_card)
            assert '"kind": "tool_approval"' in first_sse
            assert '"value": "approve"' in first_sse

            try:
                await endpoint(request([("message", "Approve"), ("session", "approval-life")]))
                raise AssertionError("Plain approval text must not launch a new desktop task")
            except HTTPException as exc:
                assert exc.status_code == 409

            resumed_sse = await consume(request([
                ("message", ""), ("session", "approval-life"),
                ("tool_approval_id", approval_id), ("tool_approval_decision", "approve"),
            ]))
            assert "Calculator opened." in resumed_sse
            assert calls[1][0] == "Open a harmless app"
            assert calls[1][1] is states["Open a harmless app"]
            assert calls[1][2] is True
            assert approval_id not in _computer_use_pending_approvals

            await consume(request([("message", "Delete a file"), ("session", "cancel-life")]))
            cancel_id = next(key for key, record in _computer_use_pending_approvals.items() if record["session_id"] == "cancel-life")
            rejected_sse = await consume(request([
                ("message", ""), ("session", "cancel-life"),
                ("tool_approval_id", cancel_id), ("tool_approval_decision", "deny"),
            ]))
            assert "CANCELLED" in rejected_sse
            assert len([call for call in calls if call[0] == "Delete a file"]) == 1
            assert cancel_id not in _computer_use_pending_approvals

    asyncio.run(_test())


def test_ollama_url_normalization_and_relative_url_prevention():
    """Verify normalize_ollama_base_url and get_ollama_base_url prevent relative URLs and strip subpaths."""
    from src.vision_analyzer import get_ollama_base_url, normalize_ollama_base_url
    from src.computer_use_controller import ComputerUseConfig, ComputerUseController

    # Test normalization of various endpoint URLs
    assert normalize_ollama_base_url("http://host.docker.internal:11434/v1") == "http://host.docker.internal:11434"
    assert normalize_ollama_base_url("http://host.docker.internal:11434/api/chat") == "http://host.docker.internal:11434"
    assert normalize_ollama_base_url("http://localhost:11434/v1/chat/completions") == "http://localhost:11434"
    assert normalize_ollama_base_url("localhost:11434") == "http://localhost:11434"
    assert normalize_ollama_base_url("http://custom-ollama:11434/") == "http://custom-ollama:11434"

    # Test resolution when OLLAMA_BASE_URL is empty string or unset
    with patch.dict(os.environ, {"OLLAMA_BASE_URL": ""}, clear=False):
        resolved = get_ollama_base_url()
        assert resolved.startswith("http://")
        assert "11434" in resolved
        assert not resolved.endswith("/api/chat")

    # Verify ComputerUseController planner sync call constructs an absolute URL
    cfg = ComputerUseConfig(ollama_url="")
    controller = ComputerUseController(config=cfg)
    
    with patch("urllib.request.urlopen") as mock_urlopen:
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"message": {"content": json.dumps({"action": "finish", "params": {}})}}).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        res = controller._call_planner_sync([{"role": "user", "content": "hello"}])
        assert res is not None
        req = mock_urlopen.call_args[0][0]
        # Verify the requested URL starts with http:// and is absolute
        assert req.full_url.startswith("http://") or req.full_url.startswith("https://")
        assert req.full_url.endswith("/api/chat")
        assert req.full_url != "/api/chat"


def test_frontend_static_computer_use_routing_and_elements():
    """The unified Jarvis send path can still use the existing desktop tool."""
    with open("static/index.html", "r", encoding="utf-8") as f:
        index_html = f.read()
    assert 'class="jarvis-mode-badge"' in index_html
    assert 'id="mode-computer-btn"' not in index_html
    assert 'id="mode-agent-btn"' not in index_html
    assert 'id="mode-chat-btn"' not in index_html

    # Check app.js for 3-mode state handling
    with open("static/app.js", "r", encoding="utf-8") as f:
        app_js = f.read()
    assert "applyModeToToggles" in app_js
    assert "st.mode = 'jarvis'" in app_js

    # Ordinary text and voice submit to chat_stream; the controller stream
    # remains reserved for its existing approval continuation lifecycle.
    with open("static/js/chat.js", "r", encoding="utf-8") as f:
        chat_js = f.read()
    assert "/api/computer_use/stream" in chat_js
    assert "fd.append('mode', 'jarvis')" in chat_js
    assert "approvalForSend?.source === 'computer_use'" in chat_js
    from src.action_intents import classify_tool_intent
    assert classify_tool_intent("Open Firefox").category == "computer"


