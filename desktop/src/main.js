import { invoke } from '@tauri-apps/api/core';
import { listen } from '@tauri-apps/api/event';
import { ConversationController, STATES } from './conversation.js';
import { microphoneErrorMessage } from './microphone.js';
import { encodePcmWav } from './audio.js';
import { spokenText } from './speech.js';

const $ = id => document.getElementById(id);
const ENDPOINTING = Object.freeze({ speechRms: 0.022, quietMs: 1250, maxUtteranceMs: 15000, minAudioBytes: 1000 });
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
let voiceNotice = '';

$('backend').value = backend;
const controller = new ConversationController({
  onState(state, detail) {
    overlay.dataset.state = state;
    const labels = { idle:'Ready', listening:'Listening…', transcribing:'Transcribing…', thinking:'Thinking…', synthesizing:'Preparing voice…', speaking:'Speaking…', interrupted:'Starting microphone…', error:'Needs attention' };
    $('state-label').textContent = labels[state] || 'Ready';
    $('mic-label').textContent = state === STATES.LISTENING ? 'Stop listening' : [STATES.SYNTHESIZING, STATES.SPEAKING].includes(state) ? 'Interrupt' : 'Start listening';
    $('mic').setAttribute('aria-label', $('mic-label').textContent);
    $('error').textContent = state === STATES.ERROR ? detail : voiceNotice;
    if (state === STATES.ERROR && /sign in again/i.test(detail || '')) {
      loginPanel.hidden = false;
      overlay.hidden = true;
      $('login-error').textContent = detail;
    }
  },
  async startCapture() { await startCapture(); },
  async stopCapture(submit) { await stopCapture(submit); },
  async transcribe(blob, requestId) {
    return invoke('transcribe', { audioBase64: await blobToBase64(blob), mimeType: blob.type || 'audio/webm', requestId });
  },
  async ensureSession() {
    if (sessionId) return sessionId;
    if (!modelInfo) modelInfo = await invoke('get_voice_model');
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
  onTranscript(text) { $('transcript').textContent = text; voiceNotice = ''; $('error').textContent = ''; },
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
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
  } catch (error) {
    throw new Error(microphoneErrorMessage(error));
  }
  if (generation !== captureGeneration) {
    stream.getTracks().forEach(track => track.stop());
    return;
  }
  media = stream;

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
    analyser = audioContext.createAnalyser();
    analyser.fftSize = 512;
    source.connect(analyser);
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
  }

  listening = true;
  const audioTrack = stream.getAudioTracks()[0];
  audioTrack?.addEventListener('ended', () => {
    if (generation !== captureGeneration || !listening) return;
    void stopCapture(false).then(() => {
      if (controller.state === STATES.LISTENING) {
        controller.transition(STATES.ERROR, 'Microphone capture ended. Check the input device, then choose Start listening to retry or type a message.');
      }
    });
  }, { once: true });

  let hasVoice = !analyser;
  let silenceMs = 0;
  const startedAt = performance.now();
  if (analyser) {
    const samples = new Uint8Array(analyser.fftSize);
    let lastFrame = startedAt;
    const detect = () => {
      if (generation !== captureGeneration || !listening) return;
      const now = performance.now();
      const delta = now - lastFrame; lastFrame = now;
      analyser.getByteTimeDomainData(samples);
      let sum = 0;
      for (const sample of samples) { const value = (sample - 128) / 128; sum += value * value; }
      const rms = Math.sqrt(sum / samples.length);
      if (rms > ENDPOINTING.speechRms) { hasVoice = true; silenceMs = 0; }
      else if (hasVoice) silenceMs += delta;
      if (hasVoice && silenceMs > ENDPOINTING.quietMs) { void stopCapture(true); return; }
      if (now - startedAt > ENDPOINTING.maxUtteranceMs) {
        void stopCapture(hasVoice).then(() => { if (!hasVoice) void resumeAfterEmptyCapture(); });
        return;
      }
      requestAnimationFrame(detect);
    };
    requestAnimationFrame(detect);
  }
  captureTimeout = setTimeout(() => {
    if (generation === captureGeneration && listening) {
      void stopCapture(hasVoice).then(() => { if (!hasVoice) void resumeAfterEmptyCapture(); });
    }
  }, ENDPOINTING.maxUtteranceMs + 100);
}

async function resumeAfterEmptyCapture() {
  if (!controller.listeningWanted || controller.state !== STATES.LISTENING) return;
  controller.transition(STATES.IDLE);
  try { await controller.startListening(); }
  catch (error) { controller.transition(STATES.ERROR, error.message || String(error)); }
}

async function stopCapture(submit = false) {
  if (!listening && !media && recorder?.state !== 'recording' && !pcmProcessor && !captureStarting) return;
  captureGeneration++;
  listening = false;
  if (captureTimeout) { clearTimeout(captureTimeout); captureTimeout = null; }

  const activeRecorder = recorder;
  recorder = null;
  if (activeRecorder?.state === 'recording') {
    const chunks = recorderChunks || [];
    recorderChunks = null;
    if (submit) {
      activeRecorder.onstop = () => {
        const blob = new Blob(chunks, { type: activeRecorder.mimeType || 'audio/webm' });
        if (blob.size > ENDPOINTING.minAudioBytes) void controller.submitAudio(blob);
        else void resumeAfterEmptyCapture();
      };
    }
    activeRecorder.stop();
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

  if (wavBlob) {
    if (wavBlob.size > ENDPOINTING.minAudioBytes) void controller.submitAudio(wavBlob);
    else void resumeAfterEmptyCapture();
  }
}

// Login and startup
async function connect() {
  backend = $('backend').value.trim();
  localStorage.setItem('odysseus.backend', backend);
  $('login-error').textContent = '';
  try {
    const shortcutDiagnostic = await invoke('get_shortcut_diagnostic');
    if (shortcutDiagnostic) $('shortcut-hint').textContent = shortcutDiagnostic;
    const status = await invoke('backend_status', { backend });
    if (!status.authenticated) { loginPanel.hidden = false; overlay.hidden = true; return; }
    await enterAssistant();
  } catch (error) { loginPanel.hidden = false; overlay.hidden = true; $('login-error').textContent = error.message || String(error); }
}

async function enterAssistant() {
  modelInfo = await invoke('get_voice_model');
  loginPanel.hidden = true; overlay.hidden = false;
  $('error').textContent = '';
  try { await controller.startListening(); }
  catch (error) { controller.transition(STATES.ERROR, error.message || String(error)); }
}

$('login-button').addEventListener('click', async event => {
  event.preventDefault();
  try {
    backend = $('backend').value.trim();
    const result = await invoke('login', { backend, username: $('username').value, password: $('password').value, totpCode: $('totp').value || null });
    if (result.requires_totp) { $('totp-wrap').hidden = false; $('login-error').textContent = 'Enter your authenticator code to continue.'; return; }
    if (!result.ok) throw new Error('Sign in was not completed');
    $('password').value = ''; $('totp').value = '';
    await enterAssistant();
  } catch (error) { $('login-error').textContent = error.message || String(error); }
});

$('composer').addEventListener('submit', async event => {
  event.preventDefault();
  const value = $('message').value.trim();
  if (!value) return;
  voiceNotice = '';
  $('error').textContent = '';
  $('message').value = '';
  if ([STATES.TRANSCRIBING, STATES.THINKING, STATES.SYNTHESIZING, STATES.SPEAKING].includes(controller.state)) {
    await controller.interrupt({ resume: false });
  }
  await controller.sendText(value);
});
$('mic').addEventListener('click', async () => {
  if (controller.state === STATES.LISTENING) await controller.stopListening();
  else if ([STATES.SPEAKING, STATES.SYNTHESIZING, STATES.THINKING, STATES.TRANSCRIBING].includes(controller.state)) await controller.interrupt({ resume: true });
  else { try { await controller.startListening(); } catch (error) { controller.transition(STATES.ERROR, error.message || String(error)); } }
});
$('orb').addEventListener('click', async () => {
  if ([STATES.SPEAKING, STATES.SYNTHESIZING, STATES.THINKING, STATES.TRANSCRIBING].includes(controller.state)) await controller.interrupt({ resume: true });
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
window.addEventListener('keydown', event => { if (event.key === 'Escape') { event.preventDefault(); dismiss(); } });

async function initialize() {
  await listen('voice-activation', async () => {
    if (loginPanel.hidden) {
      if (controller.state === STATES.IDLE || controller.state === STATES.ERROR) {
        try { await controller.startListening(); } catch (error) { controller.transition(STATES.ERROR, error.message || String(error)); }
      }
    } else $('username').focus();
  });
  window.addEventListener('tauri://focus', () => {});
  window.addEventListener('beforeunload', () => { void controller.hide(); stopTts(); });
  await connect();
}

void initialize();
