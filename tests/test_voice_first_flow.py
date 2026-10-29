"""Voice-first wiring and request-scoped duplicate execution regressions."""

import asyncio
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_composer_has_visible_standalone_microphone_control_wired_to_existing_recorder():
    html = (ROOT / "static/index.html").read_text(encoding="utf-8")
    css = (ROOT / "static/style.css").read_text(encoding="utf-8")
    app = (ROOT / "static/app.js").read_text(encoding="utf-8")
    recorder = (ROOT / "static/js/voiceRecorder.js").read_text(encoding="utf-8")

    mic = html.index('id="voice-record-btn"')
    send = html.index('class="send-btn', mic)
    assert mic < send
    assert 'class="jarvis-mode-badge"' in html
    assert 'id="mode-agent-btn"' not in html
    assert 'id="mode-chat-btn"' not in html
    assert 'id="mode-computer-btn"' not in html
    assert "st.mode = 'jarvis'" in app
    assert ".voice-record-btn {" in css
    assert "flex: 0 0 32px" in css
    assert '.voice-record-btn[data-state="recording"]' in css
    assert "getElementById('voice-record-btn')" in app
    assert "voiceRecordBtn.addEventListener('click'" in app
    assert "voiceRecorderModule.startRecording(" in app
    assert "{ requireTranscription: true }" in app
    assert "voiceRecorderModule.init();" in app
    assert "form.requestSubmit()" in recorder
    assert "window.__odysseusVoiceRequestId" in recorder


def test_microphone_control_reports_unsupported_apis_and_permission_denial():
    app = (ROOT / "static/app.js").read_text(encoding="utf-8")
    recorder = (ROOT / "static/js/voiceRecorder.js").read_text(encoding="utf-8")
    assert "Microphone is not supported in this browser." in recorder
    assert "Audio recording is not supported in this browser." in recorder
    assert "Speech recognition is not supported in this browser." in recorder
    assert "Microphone permission denied." in recorder
    assert "window._setVoiceStatus('failed'" in recorder
    assert "window._setVoiceStatus = (state, message" in app


def test_voice_turn_transcribes_submits_and_reports_states():
    recorder = (ROOT / "static/js/voiceRecorder.js").read_text(encoding="utf-8")
    chat = (ROOT / "static/js/chat.js").read_text(encoding="utf-8")
    assert "submitVoiceRequest();" in recorder
    assert "form.requestSubmit()" in recorder
    assert "No speech was recognized." in recorder
    assert "Microphone permission denied." in recorder
    assert "X-Odysseus-Request-Id" in chat
    assert "fd.append('mode', 'jarvis')" in chat
    assert "submission_to_sse_done" in chat
    assert "window._setVoiceStatus?.('executing'" in chat
    assert "window._setVoiceStatus?.('speaking'" in chat
    assert "Promise.resolve(window.aiTTSManager.checkAvailability()).then" in chat
    assert "waitForCompletion: true" in chat  # status can track playback without awaiting it
    assert "await window.aiTTSManager.enqueue" not in chat
    assert "window.__odysseusChatBusyUntil = active ? Date.now() + 120000 : Date.now()" in chat
    assert "voiceTurn || _isComputerUse" in chat  # no automatic repost on uncertain voice runs
    chat_routes = (ROOT / "routes/chat_routes.py").read_text(encoding="utf-8")
    assert "_claim_computer_use_request_id(client_request_id)" in chat_routes
    assert "will not be repeated" in chat_routes


def test_voice_tts_diagnostics_distinguish_missing_answer_from_speech_failure():
    chat = (ROOT / "static/js/chat.js").read_text(encoding="utf-8")
    answer_extraction = chat.index("const voiceAnswer = markdownModule.extractThinkingBlocks")
    assert chat.index("markdownModule.renderMermaid(roundHolder)") < answer_extraction
    assert chat.index("footerTarget.dataset.raw = accumulated") < answer_extraction
    assert "tts_input_chars: voiceAnswer.length" in chat
    assert "request_id: requestExecutionId" in chat
    assert "Jarvis did not generate a final answer. Please try again." in chat
    assert "Response is ready, but speech failed:" in chat


def test_tts_queue_supports_completion_and_interrupt():
    tts = (ROOT / "static/js/tts-ai.js").read_text(encoding="utf-8")
    assert "waitForCompletion: true" not in tts  # caller opts in explicitly
    assert "item.resolveCompletion()" in tts
    assert "item.rejectCompletion(new DOMException('Speech stopped', 'AbortError'))" in tts
    assert "if (button)" in tts


def test_voice_terminal_cleanup_and_new_recording_interrupts_old_playback():
    recorder = (ROOT / "static/js/voiceRecorder.js").read_text(encoding="utf-8")
    app = (ROOT / "static/app.js").read_text(encoding="utf-8")
    chat = (ROOT / "static/js/chat.js").read_text(encoding="utf-8")
    assert "finally {\n          _resetRecordingUI();" in recorder
    assert "window.__odysseusVoiceExecutionId = '';" in recorder
    assert "window.aiTTSManager.stop();" in app
    assert "if (_isForegroundChatBusy())" in app
    assert "window.__odysseusChatBusy === true" in recorder
    assert "voiceRecordBtn.disabled = busy;" in app
    assert "createChatBusyState" in chat
    assert "_foregroundBusyState.finish(requestId, outcome)" in chat
    assert "stale_chat_cleanup_ignored" in chat
    assert "microphone_unavailable" in app


def test_repeated_post_with_same_request_id_reuses_single_agent_run():
    from src import agent_runs

    async def exercise():
        session_id = "voice-idempotency-test"
        request_id = "voice-request-idempotency-test"
        executions = []

        async def stream(label):
            executions.append(label)
            yield f"data: {label}\n\n"

        first = agent_runs.start(session_id, stream("first"), request_id=request_id)
        duplicate = agent_runs.start(session_id, stream("duplicate"), request_id=request_id)
        assert duplicate is first
        events = [event async for event in agent_runs.subscribe(session_id, first)]
        await first.task
        assert events == ["data: first\n\n"]
        assert executions == ["first"]
        assert first.request_id == request_id
        new_request = agent_runs.start(session_id, stream("new-request"), request_id=request_id + "-new")
        assert new_request is not first
        await new_request.task
        assert executions == ["first", "new-request"]
        if first.evict_task and not first.evict_task.done():
            first.evict_task.cancel()
        if new_request.evict_task and not new_request.evict_task.done():
            new_request.evict_task.cancel()
        agent_runs._RUNS.pop(session_id, None)
        agent_runs._REQUEST_RUNS.pop(request_id, None)
        agent_runs._REQUEST_RUNS.pop(request_id + "-new", None)

    asyncio.run(exercise())


def test_computer_use_reserves_request_id_before_desktop_work():
    from routes.chat_routes import _claim_computer_use_request_id, _computer_use_request_ids

    request_id = "computer-use-once-test"
    _computer_use_request_ids.pop(request_id, None)
    assert _claim_computer_use_request_id(request_id) is True
    assert _claim_computer_use_request_id(request_id) is False
    _computer_use_request_ids.pop(request_id, None)
