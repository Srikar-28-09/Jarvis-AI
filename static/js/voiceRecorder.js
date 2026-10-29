// static/js/voiceRecorder.js

/**
 * Voice recording with optional Speech-to-Text transcription.
 *
 * STT providers:
 *   "disabled"       — record audio as file attachment (original behavior)
 *   "browser"        — use Web Speech API for real-time transcription
 *   "local"          — send recording to server /api/stt/transcribe (Whisper)
 *   "endpoint:<id>"  — send recording to server /api/stt/transcribe (API)
 */

let mediaRecorder = null;
let audioChunks = [];
let isRecording = false;
let recordingStartTime = null;
let recordingInterval = null;

// Browser STT state
let _recognition = null;
let _browserTranscript = '';
let _browserRecognitionDone = Promise.resolve();
let _recordingStartPending = false;

// Cached STT provider — refreshed on settings change
let _sttProvider = 'disabled';

function logVoiceLifecycle(event, fields = {}) {
  try {
    console.info('[voice_lifecycle]', JSON.stringify({
      event,
      request_id: window.__odysseusVoiceRequestId || '-',
      recording: isRecording,
      recording_start_pending: _recordingStartPending,
      ...fields,
    }));
  } catch (_) {}
}

/**
 * Fetch current STT provider from server settings
 */
async function refreshSttProvider() {
  try {
    const res = await fetch('/api/stt/stats', { credentials: 'same-origin' });
    if (res.ok) {
      const stats = await res.json();
      _sttProvider = stats.provider || 'disabled';
      // Use built-in browser recognition as a no-configuration fallback when
      // server STT is disabled. This does not change the saved preference.
      if (_sttProvider === 'disabled' && (window.SpeechRecognition || window.webkitSpeechRecognition)) {
        _sttProvider = 'browser';
      }
      // Notify the send button to update its icon
      if (window._updateSendBtnIcon) window._updateSendBtnIcon();
    }
  } catch (e) {
    console.warn('Failed to fetch STT stats:', e);
    if (window.SpeechRecognition || window.webkitSpeechRecognition) {
      _sttProvider = 'browser';
      if (window._updateSendBtnIcon) window._updateSendBtnIcon();
    }
  }
}

/**
 * Format seconds as MM:SS
 */
function formatTime(seconds) {
  const mins = Math.floor(seconds / 60).toString().padStart(2, '0');
  const secs = (seconds % 60).toString().padStart(2, '0');
  return `${mins}:${secs}`;
}

/**
 * Reset UI state after recording ends
 */
function _resetRecordingUI() {
  const wasActive = isRecording || _recordingStartPending;
  isRecording = false;
  _recordingStartPending = false;
  if (window._setVoiceMicState) window._setVoiceMicState('idle');
  if (recordingInterval) {
    clearInterval(recordingInterval);
    recordingInterval = null;
  }
  // Reset send button via global callback
  const sendBtn = document.querySelector('.send-btn');
  if (sendBtn) {
    sendBtn.classList.remove('recording');
    sendBtn.dataset.mode = '';
  }
  if (window._updateSendBtnIcon) {
    setTimeout(window._updateSendBtnIcon, 50);
  }
  if (wasActive) logVoiceLifecycle('voice_cleanup_completed');
}

/**
 * Start browser speech recognition alongside recording
 */
function startBrowserSTT() {
  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRecognition) throw new Error('Browser speech recognition is not supported. Enable an STT provider in Settings.');

  _browserTranscript = '';
  _recognition = new SpeechRecognition();
  const recognition = _recognition;
  recognition.continuous = true;
  recognition.interimResults = false;
  recognition.lang = '';

  recognition.onresult = (event) => {
    for (let i = event.resultIndex; i < event.results.length; i++) {
      if (event.results[i].isFinal) {
        _browserTranscript += event.results[i][0].transcript + ' ';
      }
    }
  };

  recognition.onerror = (e) => {
    console.warn('Browser STT error:', e.error);
    recognition._sttError = e.error || 'Speech recognition failed';
  };

  _browserRecognitionDone = new Promise(resolve => { recognition.onend = resolve; });

  recognition.start();
}

async function stopBrowserSTT() {
  const started = performance.now();
  const recognition = _recognition;
  if (_recognition) {
    try { _recognition.stop(); } catch (e) { /* ignore */ }
    _recognition = null;
  }
  await Promise.race([_browserRecognitionDone, new Promise(resolve => setTimeout(resolve, 1200))]);
  try {
    if (recognition && recognition._sttError) throw new Error(`Speech recognition failed: ${recognition._sttError}`);
    return _browserTranscript.trim();
  } finally {
    const payload = JSON.stringify({
      type: 'voice_stt',
      request_id: window.__odysseusVoiceRequestId || '',
      total_ms: Math.round(performance.now() - started),
      stages: [{ name: 'browser_stt', delta_ms: Math.round(performance.now() - started) }],
    });
    try { navigator.sendBeacon('/api/client-perf', new Blob([payload], { type: 'application/json' })); } catch (_) {}
  }
}

/**
 * Send audio to server for transcription
 */
async function transcribeOnServer(audioBlob) {
  if (!window.__odysseusVoiceRequestId) {
    window.__odysseusVoiceRequestId = (crypto.randomUUID && crypto.randomUUID()) || `voice-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  }
  const formData = new FormData();
  formData.append('file', audioBlob, 'audio.webm');

  const res = await fetch('/api/stt/transcribe', {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'X-Odysseus-Request-Id': window.__odysseusVoiceRequestId },
    body: formData,
  });

  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail?.message || 'Transcription failed');
  }

  const data = await res.json();
  return data.text || '';
}

/**
 * Insert transcribed text into the chat input
 */
function insertTranscription(text, showToast) {
  if (!text) return;
  const input = document.getElementById('message');
  if (!input) return;

  const existing = input.value.trim();
  input.value = existing ? existing + ' ' + text : text;

  // Trigger auto-resize and icon update
  input.dispatchEvent(new Event('input', { bubbles: true }));
  input.focus();

  if (showToast) showToast('Transcribed');
}

function submitVoiceRequest() {
  const input = document.getElementById('message');
  const form = document.getElementById('chat-form');
  if (!input || !form || !input.value.trim()) {
    if (window._setVoiceStatus) window._setVoiceStatus('failed', 'No request was recognized.');
    return;
  }
  if (window.__odysseusChatBusy === true) {
    logVoiceLifecycle('voice_submission_blocked', { reason: 'chat_request_active', chat_busy: true });
    if (window._setVoiceStatus) window._setVoiceStatus('failed', 'Wait for the current response to finish before sending this voice request.');
    return;
  }
  if (window.__odysseusVoiceSubmitPending || window.__odysseusVoiceTurnPending) return;
  window.__odysseusVoiceTurnPending = true;
  if (!window.__odysseusVoiceRequestId) {
    window.__odysseusVoiceRequestId = (crypto.randomUUID && crypto.randomUUID()) || `voice-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  }
  if (window._setVoiceStatus) window._setVoiceStatus('thinking', 'Thinking…');
  window.__odysseusVoiceSubmitPending = true;
  logVoiceLifecycle('voice_transcript_submitted');
  queueMicrotask(() => {
    window.__odysseusVoiceSubmitPending = false;
    if (window.__odysseusVoiceTurnPending && form.isConnected) form.requestSubmit();
  });
}

/**
 * Start voice recording
 */
export async function startRecording(onFileCreated, showToast, showError, options = {}) {
  if (isRecording || _recordingStartPending) {
    logVoiceLifecycle('microphone_activation_blocked', { reason: 'recorder_already_active' });
    return;
  }
  if (window.__odysseusChatBusy === true) {
    logVoiceLifecycle('microphone_activation_blocked', {
      reason: 'chat_request_active',
      chat_busy: true,
    });
    if (showError) showError('Wait for the current response to finish before recording another request.');
    return;
  }
  window.__odysseusVoiceRequestId = (crypto.randomUUID && crypto.randomUUID()) || `voice-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  window.__odysseusVoiceExecutionId = '';
  _recordingStartPending = true;
  logVoiceLifecycle('microphone_activation_allowed', { chat_busy: false });
  if (window._setVoiceStatus) window._setVoiceStatus('microphone', 'Microphone…');
  // Check for secure context (getUserMedia requires HTTPS or localhost)
  if (!window.isSecureContext) {
    if (showError) showError('Microphone requires HTTPS. Use a reverse proxy with SSL or access via localhost.');
    if (window._setVoiceStatus) window._setVoiceStatus('failed', 'Microphone requires a secure connection.');
    _recordingStartPending = false;
    _resetRecordingUI();
    return;
  }

  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    if (showError) showError('Microphone not supported in this browser.');
    if (window._setVoiceStatus) window._setVoiceStatus('failed', 'Microphone is not supported in this browser.');
    _recordingStartPending = false;
    _resetRecordingUI();
    return;
  }
  if (!window.MediaRecorder) {
    const message = 'Audio recording is not supported in this browser.';
    if (showError) showError(message);
    if (window._setVoiceStatus) window._setVoiceStatus('failed', message);
    _resetRecordingUI();
    return;
  }

  // A dedicated voice button must fail clearly when transcription is not
  // configured or supported; it must not silently turn a voice request into
  // an audio-file attachment.
  if (options.requireTranscription && _sttProvider === 'disabled') {
    await refreshSttProvider();
    if (_sttProvider === 'disabled') {
      const message = (window.SpeechRecognition || window.webkitSpeechRecognition)
        ? 'Speech recognition is not configured. Enable an STT provider in Settings.'
        : 'Speech recognition is not supported in this browser. Enable an STT provider in Settings or use a supported browser.';
      if (showError) showError(message);
      if (window._setVoiceStatus) window._setVoiceStatus('failed', message);
      _resetRecordingUI();
      return;
    }
  }

  audioChunks = [];

  navigator.mediaDevices.getUserMedia({ audio: true })
    .then(stream => {
      if (window.__odysseusChatBusy === true) {
        stream.getTracks().forEach(track => track.stop());
        logVoiceLifecycle('microphone_activation_blocked', {
          reason: 'chat_started_while_permission_pending', chat_busy: true,
        });
        if (showError) showError('Wait for the current response to finish before recording another request.');
        _resetRecordingUI();
        return;
      }
      try {
        mediaRecorder = new MediaRecorder(stream, { mimeType: 'audio/webm' });
      } catch (error) {
        stream.getTracks().forEach(track => track.stop());
        throw error;
      }

      mediaRecorder.ondataavailable = event => {
        if (event.data.size > 0) {
          audioChunks.push(event.data);
        }
      };

      mediaRecorder.onstop = async () => {
        stream.getTracks().forEach(track => track.stop());
        logVoiceLifecycle('voice_recording_stopped', { audio_chunks: audioChunks.length });

        try {
        const audioBlob = new Blob(audioChunks, { type: 'audio/webm' });
        const provider = _sttProvider;
        logVoiceLifecycle('transcription_started', { provider });
        if (window._setVoiceStatus) window._setVoiceStatus('thinking', 'Transcribing…');

        if (provider === 'browser') {
          let transcript = '';
          try { transcript = await stopBrowserSTT(); }
          catch (e) {
            logVoiceLifecycle('transcription_failed', { provider, error_name: e.name || 'Error' });
            if (showError) showError(e.message);
            if (window._setVoiceStatus) window._setVoiceStatus('failed', e.message);
          }
          if (transcript) {
            logVoiceLifecycle('transcription_completed', { provider, transcript_chars: transcript.length });
            insertTranscription(transcript, showToast);
            submitVoiceRequest();
          } else {
            logVoiceLifecycle('transcription_completed', { provider, transcript_chars: 0 });
            if (showToast) showToast('No speech detected');
            if (window._setVoiceStatus) window._setVoiceStatus('failed', 'No speech was recognized.');
          }
        } else if (provider === 'local' || provider.startsWith('endpoint:')) {
          // Show "Transcribing..." feedback
          if (showToast) showToast('Transcribing...', 5000);
          try {
            const transcript = await transcribeOnServer(audioBlob);
            if (transcript) {
              logVoiceLifecycle('transcription_completed', { provider, transcript_chars: transcript.length });
              insertTranscription(transcript, showToast);
              submitVoiceRequest();
            } else {
              logVoiceLifecycle('transcription_completed', { provider, transcript_chars: 0 });
              if (showToast) showToast('No speech detected');
              if (window._setVoiceStatus) window._setVoiceStatus('failed', 'No speech was recognized.');
            }
          } catch (e) {
            logVoiceLifecycle('transcription_failed', { provider, error_name: e.name || 'Error' });
            console.error('STT transcription error:', e);
            if (showError) showError('Transcription failed: ' + e.message);
            if (window._setVoiceStatus) window._setVoiceStatus('failed', `Transcription failed: ${e.message}`);
          }
        } else {
          // STT disabled — attach audio file
          const audioFile = new File([audioBlob], `voice-message-${Date.now()}.webm`, { type: 'audio/webm' });
          if (onFileCreated) onFileCreated(audioFile);
        }

        } catch (error) {
          logVoiceLifecycle('voice_recording_completion_failed', { error_name: error.name || 'Error' });
          console.error('Voice recording completion failed:', error);
          if (window._setVoiceStatus) window._setVoiceStatus('failed', error.message || 'Voice request failed.');
        } finally {
          _resetRecordingUI();
        }
      };

      mediaRecorder.start();
      isRecording = true;
      logVoiceLifecycle('voice_recording_started');
      if (window._setVoiceMicState) window._setVoiceMicState('recording');
      recordingStartTime = new Date();
      if (window._setVoiceStatus) window._setVoiceStatus('listening', 'Listening…');

      // Start browser STT if that's the provider
      if (_sttProvider === 'browser') {
        try { startBrowserSTT(); }
        catch (error) {
          stream.getTracks().forEach(track => track.stop());
          mediaRecorder.stop();
          if (showError) showError(error.message);
          if (window._setVoiceStatus) window._setVoiceStatus('failed', error.message);
          _resetRecordingUI();
          return;
        }
      }

      if (showToast) {
        showToast('Recording...');
      }
    })
    .catch(error => {
      logVoiceLifecycle('microphone_activation_failed', { error_name: error.name || 'Error' });
      console.error('Microphone access error:', error);
      if (showError) {
        if (error.name === 'NotAllowedError') {
          showError('Microphone access denied. Check browser permissions.');
        } else if (error.name === 'NotFoundError') {
          showError('No microphone found.');
        } else {
          showError('Microphone error: ' + error.message);
        }
      }
      if (window._setVoiceStatus) window._setVoiceStatus('failed', error.name === 'NotAllowedError' ? 'Microphone permission denied.' : (error.message || 'Microphone failed.'));
      _resetRecordingUI();
    });
}

/**
 * Stop voice recording
 */
export function stopRecording() {
  if (mediaRecorder && mediaRecorder.state === 'recording') {
    logVoiceLifecycle('voice_recording_stop_requested');
    mediaRecorder.stop();
    // isRecording will be set to false in _resetRecordingUI called from onstop
  } else {
    _resetRecordingUI();
  }
}

/**
 * Check if currently recording
 */
export function getIsRecording() {
  return isRecording;
}

/**
 * Initialize recording state
 */
export function init() {
  isRecording = false;
  refreshSttProvider();
}

const voiceRecorderModule = {
  startRecording,
  stopRecording,
  getIsRecording,
  init,
  refreshSttProvider,
  get _sttProvider() { return _sttProvider; },
  set _sttProvider(v) { _sttProvider = v; },
};

export default voiceRecorderModule;
