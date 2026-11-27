import { invoke } from '@tauri-apps/api/core';
import { listen } from '@tauri-apps/api/event';
import { ConversationController, STATES } from './conversation.js';
import { microphoneErrorMessage } from './microphone.js';
import { encodePcmWav } from './audio.js';
import { spokenText } from './speech.js';
import {
  audioCaptureConstraints,
  audioProcessingNotice,
  readVoiceCaptureSettings,
  SpeechEndpointing,
} from '../../static/js/voice_capture.mjs';
import {
  focusActivatedInterface,
  hostBridgeNotice,
  resolveActivationShortcut,
  retainUnsentMessage,
  voiceInputNotice,
} from './activation.js';
import { pollBackendReadiness, singleFlight } from './startup.js';

const $ = id => document.getElementById(id);
const frontendStartedAt = performance.now();
const MIN_AUDIO_BYTES = 1000;
const overlay = $('overlay');
const loginPanel = $('login');
let backend = localStorage.getItem('odysseus.backend') || 'http://127.0.0.1:7000';
let modelInfo = null;
let sessionId = null;
let sessionPromise = null;
let media = null;
let recorder = null;
let recorderChunks = null;
let pcmProcessor = null;
let pcmChunks = null;
let pcmSampleRate = 0;
let audioContext = null;
let activeAudio = null;
let audioUrl = null;
let cancelPlayback = null;
let listening = false;
const chatRuns = new Map();
const cancelledChatRequests = new Set();
let hideInProgress = false;
let captureGeneration = 0;
let captureStarting = null;
let captureTimeout = null;
let voiceSettings = readVoiceCaptureSettings();
let voiceNotice = '';
let backendReady = false;
let voiceInputAvailable = false;
let pendingVoiceReply = false;

$('backend').value = backend;
const maxUtterance = $('max-utterance');
const noiseSuppression = $('noise-suppression');
maxUtterance.value = String(voiceSettings.maxUtteranceMs / 1000);
noiseSuppression.value = voiceSettings.noiseSuppression;
maxUtterance.addEventListener('change', () => {
  localStorage.setItem('odysseus.voice.maxUtteranceSeconds', maxUtterance.value);
  voiceSettings = readVoiceCaptureSettings();
});
noiseSuppression.addEventListener('change', () => {
  localStorage.setItem('odysseus.voice.noiseSuppression', noiseSuppression.value);
  voiceSettings = readVoiceCaptureSettings();
});
const controller = new ConversationController({
  onState(state, detail) {
    overlay.dataset.state = state;
    const labels = { idle:'Ready', listening:'Listening…', processing:'Processing audio…', transcribing:'Transcribing…', thinking:'Thinking…', synthesizing:'Preparing voice…', speaking:'Speaking…', interrupted:'Starting microphone…', error:'Needs attention' };
    $('state-label').textContent = labels[state] || 'Ready';
    $('mic-label').textContent = state === STATES.LISTENING ? 'Stop listening' : [STATES.PROCESSING, STATES.TRANSCRIBING, STATES.THINKING, STATES.SYNTHESIZING, STATES.SPEAKING].includes(state) ? 'Interrupt' : 'Start listening';
    $('mic').setAttribute('aria-label', $('mic-label').textContent);
    $('error').textContent = state === STATES.ERROR ? detail : voiceNotice;
    if (state === STATES.ERROR && /sign in again/i.test(detail || '')) {
      loginPanel.hidden = false;
      overlay.hidden = true;
      $('login-error').textContent = detail;
    }
  },
  async startCapture() { await startCapture(); },
  async stopCapture(submit) { return stopCapture(submit); },
  async transcribe(blob, requestId) {
    return invoke('transcribe', { audioBase64: await blobToBase64(blob), mimeType: blob.type || 'audio/webm', requestId });
  },
  async ensureSession() {
    if (sessionId) return sessionId;
    if (!modelInfo) modelInfo = await invoke('get_default_chat_model');
    if (!sessionPromise) {
      sessionPromise = invoke('create_voice_session', { endpointId: modelInfo.endpoint_id, model: modelInfo.model })
        .then(id => { sessionId = id; return id; })
        .finally(() => { sessionPromise = null; });
    }
    return sessionPromise;
  },
  async chat(sid, message, requestId, events) {
    const unlisten = await listen('voice-chat-event', event => {
      const data = event.payload;
      if (data.request_id !== requestId) return;
      if (data.type === 'started') {
        const runId = data.run_id || '';
        chatRuns.set(requestId, runId);
        if (controller.activeTurn === requestId) events.onRunId(runId);
        if (runId && cancelledChatRequests.delete(requestId)) void invoke('cancel_chat', { sessionId: sid, runId }).catch(() => {});
      }
      if (data.type === 'delta' && controller.activeTurn === requestId) events.onDelta((window.__voiceResponse || '') + data.text);
    });
    if (controller.activeTurn !== requestId) { unlisten(); return ''; }
    chatRuns.set(requestId, '');
    window.__voiceResponse = '';
    try {
      const result = await invoke('chat', { sessionId: sid, message, requestId });
      window.__voiceResponse = '';
      return result;
    } finally {
      unlisten();
      chatRuns.delete(requestId);
      cancelledChatRequests.delete(requestId);
    }
  },
  async cancelChat(sid, runId, requestId) {
    if (runId) { await invoke('cancel_chat', { sessionId: sid, runId }); return; }
    if (chatRuns.has(requestId)) cancelledChatRequests.add(requestId);
  },
  async speak(text, requestId, events) { await speak(text, requestId, events); },
  async stopTts() { stopTts(); },
  onTranscript(text) { $('transcript').textContent = text; },
  onVoiceTranscript(text) {
    $('transcript').textContent = text;
    $('message').value = retainUnsentMessage(text, $('message').value);
    $('message').dispatchEvent(new Event('input', { bubbles: true }));
    $('message').focus();
    pendingVoiceReply = true;
    voiceNotice = 'Review or correct the transcript, then send it. Voice input is not speaker-verified.';
    $('error').textContent = voiceNotice;
  },
  onResponse(text, isDelta = false) {
    if (isDelta) { window.__voiceResponse = text; $('response').textContent = text; }
    else { window.__voiceResponse = ''; $('response').textContent = text; }
  },
  onNotice(message) { voiceNotice = message; $('error').textContent = message; },
});

async function blobToBase64(blob) {
  const data = await blob.arrayBuffer();
  let binary = '';
  const bytes = new Uint8Array(data);
  for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(binary);
}

function reportDiagnostic(category, message) {
  const safeMessage = String(message || '').replace(/[\r\n\u0000-\u001f]/g, ' ').slice(0, 600);
  console.error(`[odysseus:${category}] ${safeMessage}`);
  void invoke('voice_diagnostic', { category, message: safeMessage }).catch(() => {});
}

function recordActivationMetric(name, startedAt) {
  const elapsedMs = performance.now() - startedAt;
  void invoke('record_activation_metric', { name, elapsedMs }).catch(() => {});
}

function errorSummary(error) {
  const name = error?.name || 'Error';
  const message = String(error?.message || error || '').replace(/https?:\/\/\S+/gi, '<url>')
    .replace(/\bBearer\s+\S+/gi, 'Bearer <redacted>').slice(0, 400);
  return message ? `${name}: ${message}` : name;
}

window.addEventListener('error', event => {
  reportDiagnostic('webview', `uncaught ${event.message || 'script error'} at line ${event.lineno || 0}`);
});
window.addEventListener('unhandledrejection', event => {
  reportDiagnostic('webview', `unhandled rejection (${errorSummary(event.reason)})`);
});

async function speak(text, turnId, playbackEvents = {}) {
  const clean = spokenText(text);
  if (!clean) return;
  stopTts();
  let tts;
  try { tts = await invoke('tts_status'); }
  catch (error) {
    reportDiagnostic('tts', `status request failed (${errorSummary(error)})`);
    throw error;
  }
  if (!tts.available || !tts.ready) {
    reportDiagnostic('tts', `voice service unavailable (provider=${tts.provider || 'unknown'})`);
    throw new Error('Jarvis voice is disabled or unavailable in Odysseus settings');
  }
  if (tts.provider === 'browser') {
    if (!window.speechSynthesis) throw new Error('This webview does not support the configured browser voice');
    await new Promise((resolve, reject) => {
      let settled = false;
      let timer;
      const finish = (callback, value) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        if (cancelPlayback === cancel) cancelPlayback = null;
        callback(value);
      };
      const cancel = () => { window.speechSynthesis.cancel(); finish(reject, new Error('Playback interrupted')); };
      cancelPlayback = cancel;
      timer = setTimeout(() => { window.speechSynthesis.cancel(); finish(reject, new Error('Browser voice playback timed out')); }, playbackLimit(clean));
      const utterance = new SpeechSynthesisUtterance(clean);
      const voices = window.speechSynthesis.getVoices();
      const selected = voices.find(voice => voice.name.toLowerCase() === String(tts.voice || '').toLowerCase())
        || voices.find(voice => voice.name.toLowerCase().includes(String(tts.voice || '').toLowerCase()));
      if (selected) utterance.voice = selected;
      utterance.rate = Number(tts.speed) || 1;
      utterance.onstart = () => {
        reportDiagnostic('audio', 'browser speech playback started');
        playbackEvents.onPlaybackStarted?.();
      };
      utterance.onend = () => { reportDiagnostic('audio', 'browser speech playback ended'); finish(resolve); };
      utterance.onerror = event => {
        reportDiagnostic('audio', `browser speech playback failed (${event.error || 'unknown'})`);
        finish(reject, new Error(`Browser voice playback failed (${event.error || 'unknown'})`));
      };
      window.speechSynthesis.speak(utterance);
    });
    return;
  }
  let synthesized;
  try { synthesized = await invoke('synthesize', { text: clean, requestId: turnId }); }
  catch (error) {
    reportDiagnostic('tts', `synthesis command failed (${errorSummary(error)})`);
    throw error;
  }
  if (controller.activeTurn !== turnId) return;
  const data = new Uint8Array(synthesized.bytes || []);
  const mimeType = synthesized.mime_type;
  if (!data.length || !['audio/mpeg', 'audio/wav'].includes(mimeType)) {
    reportDiagnostic('tts', `invalid audio returned (bytes=${data.length}, mime=${mimeType || 'missing'})`);
    throw new Error('Jarvis voice service returned invalid audio');
  }
  reportDiagnostic('tts', `synthesis complete (bytes=${data.length}, mime=${mimeType})`);
  const blob = new Blob([data], { type: mimeType });
  const playbackUrl = URL.createObjectURL(blob);
  const audio = new Audio();
  audio.preload = 'auto';
  audio.src = playbackUrl;
  audio.load();
  audioUrl = playbackUrl;
  activeAudio = audio;
  try {
    await new Promise((resolve, reject) => {
      let settled = false;
      let timer;
      const finish = (callback, value) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        if (cancelPlayback === cancel) cancelPlayback = null;
        callback(value);
      };
      const cancel = () => { audio.pause(); finish(reject, new Error('Playback interrupted')); };
      cancelPlayback = cancel;
      timer = setTimeout(() => { audio.pause(); finish(reject, new Error('Jarvis audio playback timed out')); }, playbackLimit(clean));
      audio.onplaying = () => {
        reportDiagnostic('audio', 'WebKit audio playback started');
        playbackEvents.onPlaybackStarted?.();
      };
      audio.onended = () => { reportDiagnostic('audio', 'WebKit audio playback ended'); finish(resolve); };
      audio.onerror = () => {
        const code = audio.error?.code || 0;
        reportDiagnostic('audio', `WebKit audio playback failed (media error ${code})`);
        finish(reject, new Error(`Audio playback failed (media error ${code})`));
      };
      try { audio.play().catch(error => {
        reportDiagnostic('audio', `WebKit play request rejected (${errorSummary(error)})`);
        finish(reject, error);
      }); }
      catch (error) {
        reportDiagnostic('audio', `WebKit play request threw (${errorSummary(error)})`);
        finish(reject, error);
      }
    });
  } finally {
    audio.onplaying = null;
    audio.onended = null;
    audio.onerror = null;
    audio.pause();
    audio.removeAttribute('src');
    audio.load();
    if (activeAudio === audio) activeAudio = null;
    if (audioUrl === playbackUrl) audioUrl = null;
    URL.revokeObjectURL(playbackUrl);
  }
}

function playbackLimit(text) { return Math.min(120000, Math.max(10000, text.length * 130)); }

function stopTts() {
  if (cancelPlayback) cancelPlayback();
  if (window.speechSynthesis) window.speechSynthesis.cancel();
  if (activeAudio) { activeAudio.pause(); activeAudio.removeAttribute('src'); activeAudio.load(); activeAudio = null; }
  if (audioUrl) { URL.revokeObjectURL(audioUrl); audioUrl = null; }
}

function startCapture() {
  if (listening || recorder?.state === 'recording' || pcmProcessor) return Promise.resolve();
  if (captureStarting) return captureStarting;
  const pending = beginCapture().finally(() => {
    if (captureStarting === pending) captureStarting = null;
  });
  captureStarting = pending;
  return pending;
}

async function beginCapture() {
  if (!navigator.mediaDevices?.getUserMedia) {
    throw new Error('This WebKitGTK webview does not expose microphone capture. Type a message or use another supported microphone interface.');
  }

  const generation = ++captureGeneration;
  const captureSettings = voiceSettings;
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia(audioCaptureConstraints(captureSettings.noiseSuppression));
  } catch (error) {
    throw new Error(microphoneErrorMessage(error));
  }
  if (generation !== captureGeneration) {
    stream.getTracks().forEach(track => track.stop());
    return;
  }
  media = stream;
  const audioTrack = stream.getAudioTracks()[0];
  const processingNotice = audioProcessingNotice(audioTrack?.getSettings?.(), captureSettings.noiseSuppression);
  if (processingNotice) {
    voiceNotice = [voiceNotice, processingNotice].filter(Boolean).join(' ');
    $('error').textContent = voiceNotice;
  }

  const mimeType = typeof window.MediaRecorder?.isTypeSupported === 'function'
    ? ['audio/webm;codecs=opus', 'audio/webm'].find(type => {
      try { return window.MediaRecorder.isTypeSupported(type); } catch { return false; }
    })
    : '';
  if (mimeType) {
    try {
      const webmRecorder = new window.MediaRecorder(stream, { mimeType });
      const chunks = [];
      recorderChunks = chunks;
      webmRecorder.ondataavailable = event => { if (event.data.size) chunks.push(event.data); };
      webmRecorder.start(250);
      recorder = webmRecorder;
    } catch {
      recorder = null;
      recorderChunks = null;
    }
  }

  const usePcm = !recorder;
  if (usePcm) {
    pcmChunks = [];
    pcmSampleRate = 0;
  }

  let analyser = null;
  try {
    const AudioContextConstructor = window.AudioContext || window.webkitAudioContext;
    if (!AudioContextConstructor) throw new Error('Web Audio is unavailable');
    audioContext = new AudioContextConstructor();
    if (audioContext.state === 'suspended') await audioContext.resume();
    if (generation !== captureGeneration) {
      await stopCapture(false);
      return;
    }

    const source = audioContext.createMediaStreamSource(stream);
    const vadFilter = audioContext.createBiquadFilter();
    vadFilter.type = 'highpass';
    vadFilter.frequency.value = 80;
    vadFilter.Q.value = 0.707;
    analyser = audioContext.createAnalyser();
    analyser.fftSize = 512;
    source.connect(vadFilter);
    vadFilter.connect(analyser);
    const silentOutput = audioContext.createGain();
    silentOutput.gain.value = 0;
    analyser.connect(silentOutput);
    silentOutput.connect(audioContext.destination);

    if (usePcm) {
      if (audioContext.state !== 'running' || typeof audioContext.createScriptProcessor !== 'function') {
        throw new Error('This WebKitGTK build has neither a usable MediaRecorder nor Web Audio PCM capture.');
      }
      pcmSampleRate = audioContext.sampleRate;
      pcmProcessor = audioContext.createScriptProcessor(4096, 1, 1);
      pcmProcessor.onaudioprocess = event => {
        if (generation !== captureGeneration || !listening) return;
        pcmChunks.push(new Float32Array(event.inputBuffer.getChannelData(0)));
      };
      source.connect(pcmProcessor);
      pcmProcessor.connect(silentOutput);
    }
  } catch (error) {
    if (usePcm) {
      await stopCapture(false);
      throw new Error(error.message || 'Web Audio PCM microphone capture could not start.');
    }
    if (audioContext) { await audioContext.close().catch(() => {}); audioContext = null; }
    analyser = null;
    voiceNotice = 'Automatic silence detection is unavailable; use Stop listening to finish the recording.';
    $('error').textContent = voiceNotice;
  }

  listening = true;
  audioTrack?.addEventListener('ended', () => {
    if (generation !== captureGeneration || !listening) return;
    void stopCapture(false).then(() => {
      if (controller.state === STATES.LISTENING) {
        controller.transition(STATES.ERROR, 'Microphone capture ended. Check the input device, then choose Start listening to retry or type a message.');
      }
    });
  }, { once: true });

  const endpointing = new SpeechEndpointing();
  let hasVoice = !analyser;
  const startedAt = performance.now();
  if (analyser) {
    const samples = typeof analyser.getFloatTimeDomainData === 'function'
      ? new Float32Array(analyser.fftSize)
      : new Uint8Array(analyser.fftSize);
    let lastFrame = startedAt;
    const detect = () => {
      if (generation !== captureGeneration || !listening) return;
      const now = performance.now();
      const delta = now - lastFrame; lastFrame = now;
      if (samples instanceof Float32Array) analyser.getFloatTimeDomainData(samples);
      else analyser.getByteTimeDomainData(samples);
      let sum = 0;
      for (const sample of samples) {
        const value = samples instanceof Float32Array ? sample : (sample - 128) / 128;
        sum += value * value;
      }
      const rms = Math.sqrt(sum / samples.length);
      const state = endpointing.update(rms, delta);
      hasVoice = state.speechDetected;
      if (state.finalized) { void finalizeCapture(); return; }
      if (now - startedAt >= captureSettings.maxUtteranceMs) {
        void finishAtMaximumDuration(hasVoice);
        return;
      }
      requestAnimationFrame(detect);
    };
    requestAnimationFrame(detect);
  }
  captureTimeout = setTimeout(() => {
    if (generation === captureGeneration && listening) {
      void finishAtMaximumDuration(hasVoice);
    }
  }, captureSettings.maxUtteranceMs + 100);
}

async function finalizeCapture() {
  try {
    const blob = await stopCapture(true);
    if (blob) void controller.submitAudio(blob);
    else void resumeAfterEmptyCapture();
  } catch (error) {
    controller.transition(STATES.ERROR, error.message || String(error));
  }
}

async function finishAtMaximumDuration(hasVoice) {
  if (hasVoice) return finalizeCapture();
  await stopCapture(false);
  await resumeAfterEmptyCapture();
}

async function resumeAfterEmptyCapture() {
  if (!controller.listeningWanted || controller.state !== STATES.LISTENING) return;
  controller.transition(STATES.IDLE);
  try { await controller.startListening(); }
  catch (error) { controller.transition(STATES.ERROR, error.message || String(error)); }
}

async function stopCapture(submit = false) {
  if (!listening && !media && recorder?.state !== 'recording' && !pcmProcessor && !captureStarting) return null;
  captureGeneration++;
  listening = false;
  if (captureTimeout) { clearTimeout(captureTimeout); captureTimeout = null; }

  const activeRecorder = recorder;
  recorder = null;
  let recorderStopped = null;
  if (activeRecorder?.state === 'recording') {
    const chunks = recorderChunks || [];
    recorderChunks = null;
    if (submit) {
      let resolveStopped;
      let rejectStopped;
      recorderStopped = new Promise((resolve, reject) => {
        resolveStopped = resolve;
        rejectStopped = reject;
      });
      activeRecorder.addEventListener('stop', () => {
        resolveStopped(new Blob(chunks, { type: activeRecorder.mimeType || 'audio/webm' }));
      }, { once: true });
      activeRecorder.addEventListener('error', event => {
        rejectStopped(event.error || new Error('Microphone recording could not be finalized.'));
      }, { once: true });
    }
    try { activeRecorder.stop(); }
    catch (error) {
      if (rejectStopped) rejectStopped(error);
      else throw error;
    }
  } else {
    recorderChunks = null;
  }

  let wavBlob = null;
  if (submit && pcmChunks?.length) wavBlob = encodePcmWav(pcmChunks, pcmSampleRate);
  pcmChunks = null;
  pcmSampleRate = 0;
  if (pcmProcessor) {
    pcmProcessor.onaudioprocess = null;
    pcmProcessor.disconnect();
    pcmProcessor = null;
  }
  if (media) { media.getTracks().forEach(track => track.stop()); media = null; }
  if (audioContext) { await audioContext.close().catch(() => {}); audioContext = null; }

  const recordedBlob = recorderStopped ? await recorderStopped : null;
  const blob = recordedBlob || wavBlob;
  return submit && blob?.size > MIN_AUDIO_BYTES ? blob : null;
}

// Login and startup
const connect = singleFlight(connectBackend);

async function connectBackend() {
  const activationStartedAt = performance.now();
  backendReady = false;
  backend = $('backend').value.trim();
  localStorage.setItem('odysseus.backend', backend);
  $('login-error').textContent = '';
  loginPanel.hidden = true;
  overlay.hidden = false;
  $('state-label').textContent = 'Starting desktop…';
  $('message').focus();
  $('retry-connection').hidden = true;
  let shortcutMessage = '';
  let serviceErrors = [];
  try {
    const shortcutDiagnostic = await invoke('get_shortcut_diagnostic');
    shortcutMessage = shortcutDiagnostic;
    const activeShortcut = await invoke('get_activation_shortcut');
    const storedShortcut = localStorage.getItem('odysseus.activationShortcut');
    let savedShortcut = resolveActivationShortcut(storedShortcut, activeShortcut);
    if (savedShortcut !== activeShortcut) {
      try { await invoke('set_activation_shortcut', { shortcut: savedShortcut }); }
      catch (error) {
        shortcutMessage = error.message || String(error);
        savedShortcut = storedShortcut || activeShortcut;
      }
    }
    if (storedShortcut === 'Shift+Enter' && savedShortcut === activeShortcut) {
      try {
        await invoke('set_activation_shortcut', { shortcut: activeShortcut });
        localStorage.setItem('odysseus.activationShortcut', activeShortcut);
      } catch (error) {
        shortcutMessage = error.message || String(error);
        savedShortcut = storedShortcut;
      }
    }
    $('shortcut-hint').replaceChildren(
      document.createTextNode(`${savedShortcut.toUpperCase()} `),
      Object.assign(document.createElement('i'), { textContent: 'to open · configure' }),
    );
    $('state-label').textContent = 'Starting backend and host bridge…';
    const serviceStartStartedAt = performance.now();
    let startup = {};
    try {
      startup = await invoke('start_background_services', { backend });
      serviceErrors = startup.errors || [];
    } catch (error) {
      serviceErrors = [error.message || String(error)];
    }
    recordActivationMetric('backend_service_start_request', serviceStartStartedAt);

    if (startup.backend_error) {
      $('state-label').textContent = 'Backend startup blocked';
      $('error').textContent = [
        startup.backend_error,
        'Start or enable Docker Engine, then choose Retry connection.',
        startup.host_bridge_error,
        shortcutMessage,
      ].filter(Boolean).join(' ');
      $('retry-connection').hidden = false;
      return;
    }
    let status;
    try {
      status = await pollBackendReadiness({
        probe: async () => {
          try {
            return await invoke('backend_status', { backend });
          } catch (error) {
            const serviceStatus = await invoke('background_service_status').catch(() => ({}));
            if (serviceStatus.backend === 'failed') {
              const failure = new Error(
                'Docker Compose backend startup failed. Inspect it with: journalctl --user -u odysseus-backend.service',
              );
              failure.cause = error;
              failure.terminal = true;
              throw failure;
            }
            throw error;
          }
        },
        isTerminalError: error => Boolean(error?.terminal),
        onRetry: () => { $('state-label').textContent = 'Waiting for backend readiness…'; },
      });
    } catch (error) {
      recordActivationMetric('backend_startup_timeout_after', activationStartedAt);
      const serviceStatus = await invoke('background_service_status').catch(() => ({}));
      $('state-label').textContent = 'Backend startup failed';
      $('error').textContent = [
        error.message,
        serviceStatus.backend && serviceStatus.backend !== 'failed'
          ? `Backend service: ${serviceStatus.backend}.`
          : '',
        hostBridgeNotice(serviceStatus.host_bridge),
        ...serviceErrors,
        shortcutMessage,
      ].filter(Boolean).join(' ');
      $('retry-connection').hidden = false;
      return;
    }
    recordActivationMetric('backend_ready_after_activation', activationStartedAt);
    if (!status.authenticated) {
      backendReady = false;
      loginPanel.hidden = false;
      overlay.hidden = true;
      $('username').focus();
      return;
    }
    const bridgeStatusStartedAt = performance.now();
    const services = await invoke('background_service_status').catch(() => ({}));
    recordActivationMetric('host_bridge_service_status', bridgeStatusStartedAt);
    const notices = [
      ...serviceErrors,
      hostBridgeNotice(services.host_bridge),
      shortcutMessage,
    ].filter(Boolean);
    await enterAssistant(notices);
  } catch (error) {
    $('state-label').textContent = 'Backend connection failed';
    $('error').textContent = error.message || String(error);
    $('retry-connection').hidden = false;
  }
}

async function enterAssistant(notices = []) {
  loginPanel.hidden = true; overlay.hidden = false;
  backendReady = true;
  $('state-label').textContent = 'Ready';
  $('message').focus();
  const captureSupported = Boolean(
    navigator.mediaDevices?.getUserMedia
    && (window.MediaRecorder || window.AudioContext || window.webkitAudioContext),
  );
  let sttStatus = null;
  if (captureSupported) {
    try { sttStatus = await invoke('stt_status'); }
    catch (error) { sttStatus = { available: false, reason: error.message || String(error) }; }
  }
  voiceInputAvailable = captureSupported
    && Boolean(sttStatus?.available)
    && sttStatus?.provider !== 'browser';
  voiceNotice = [
    ...notices,
    voiceInputNotice({ captureSupported, sttStatus }),
    'Speaker verification is unavailable; voice is not identity-verified. Sign-in, permissions, and approvals still apply.',
  ].filter(Boolean).join(' ');
  $('error').textContent = voiceNotice;
}

$('login-button').addEventListener('click', async event => {
  event.preventDefault();
  try {
    backend = $('backend').value.trim();
    const result = await invoke('login', { backend, username: $('username').value, password: $('password').value, totpCode: $('totp').value || null });
    if (result.requires_totp) { $('totp-wrap').hidden = false; $('login-error').textContent = 'Enter your authenticator code to continue.'; return; }
    if (!result.ok) throw new Error('Sign in was not completed');
    $('password').value = ''; $('totp').value = '';
    const status = await invoke('backend_status', { backend });
    if (!status.authenticated) throw new Error('Sign in again to continue');
    await enterAssistant();
  } catch (error) { $('login-error').textContent = error.message || String(error); }
});

$('composer').addEventListener('submit', async event => {
  event.preventDefault();
  const value = $('message').value.trim();
  if (!value) return;
  if (!backendReady) {
    $('error').textContent = 'Jarvis is still connecting. Choose Retry connection before sending a message.';
    return;
  }
  if ([STATES.PROCESSING, STATES.TRANSCRIBING, STATES.THINKING, STATES.SYNTHESIZING, STATES.SPEAKING].includes(controller.state)) {
    $('error').textContent = 'A request is already in progress. Wait for it to finish before sending another message.';
    return;
  }
  voiceNotice = '';
  $('error').textContent = '';
  $('message').value = '';
  const sent = await controller.sendText(value, { speakResponse: pendingVoiceReply });
  if (!sent) {
    $('message').value = retainUnsentMessage(value, $('message').value);
    $('error').textContent = [
      $('error').textContent,
      'The message was not completed and remains in the composer. Review it before retrying.',
    ].filter(Boolean).join(' ');
    $('message').focus();
  } else {
    pendingVoiceReply = false;
  }
});
$('mic').addEventListener('click', async () => {
  if (!backendReady) { $('error').textContent = 'Jarvis is still connecting. Retry before using voice.'; return; }
  if (!voiceInputAvailable) { $('error').textContent = voiceNotice; return; }
  if (controller.state === STATES.LISTENING) await controller.stopListening();
  else if ([STATES.SPEAKING, STATES.SYNTHESIZING, STATES.THINKING, STATES.TRANSCRIBING, STATES.PROCESSING].includes(controller.state)) await controller.interrupt({ resume: true });
  else { try { await controller.startListening(); } catch (error) { controller.transition(STATES.ERROR, error.message || String(error)); } }
});
$('orb').addEventListener('click', async () => {
  if (!backendReady) { $('error').textContent = 'Jarvis is still connecting. Retry before using voice.'; return; }
  if (!voiceInputAvailable) { $('error').textContent = voiceNotice; return; }
  if ([STATES.SPEAKING, STATES.SYNTHESIZING, STATES.THINKING, STATES.TRANSCRIBING, STATES.PROCESSING].includes(controller.state)) await controller.interrupt({ resume: true });
  else if (controller.state === STATES.LISTENING) await controller.stopListening();
  else { try { await controller.startListening(); } catch (error) { controller.transition(STATES.ERROR, error.message || String(error)); } }
});

async function dismiss() {
  if (hideInProgress) return;
  hideInProgress = true;
  try { await controller.hide(); await invoke('hide_overlay'); }
  finally { hideInProgress = false; }
}
$('dismiss').addEventListener('click', dismiss);
$('retry-connection').addEventListener('click', () => { void connect(); });
$('shortcut-hint').addEventListener('click', async () => {
  const shortcut = window.prompt('Enter a global activation shortcut (for example Ctrl+Alt+Shift+J):',
    localStorage.getItem('odysseus.activationShortcut') || $('shortcut-hint').textContent.split(' ')[0]);
  if (shortcut === null) return;
  try {
    await invoke('set_activation_shortcut', { shortcut });
    localStorage.setItem('odysseus.activationShortcut', shortcut.trim());
    $('shortcut-hint').replaceChildren(
      document.createTextNode(`${shortcut.trim().toUpperCase()} `),
      Object.assign(document.createElement('i'), { textContent: 'to open · configure' }),
    );
    $('error').textContent = '';
  } catch (error) {
    $('error').textContent = error.message || String(error);
  }
});
window.addEventListener('keydown', event => { if (event.key === 'Escape') { event.preventDefault(); dismiss(); } });

async function initialize() {
  await listen('ui-activation', () => {
    const activationStartedAt = performance.now();
    focusActivatedInterface(loginPanel, $('username'), $('message'));
    recordActivationMetric('warm_activation_to_focus', activationStartedAt);
  });
  window.addEventListener('tauri://focus', () => {});
  window.addEventListener('beforeunload', () => { void controller.hide(); stopTts(); });
  $('state-label').textContent = 'Starting desktop…';
  const focusStartedAt = performance.now();
  $('message').focus();
  recordActivationMetric('initial_input_focus', focusStartedAt);
  void invoke('desktop_rendered').catch(() => {});
  requestAnimationFrame(() => recordActivationMetric('frontend_first_frame', frontendStartedAt));
  await connect();
}

void initialize();
