export const DEFAULT_SILENCE_MS = 2000;
export const DEFAULT_MAX_UTTERANCE_MS = 45000;

const MIN_RMS_THRESHOLD = 0.0045;
const MAX_RMS_THRESHOLD = 0.018;

export function readVoiceCaptureSettings(storage = globalThis.localStorage) {
  const storedSeconds = storage?.getItem('odysseus.voice.maxUtteranceSeconds');
  const rawSeconds = storedSeconds === null || storedSeconds === undefined || storedSeconds === ''
    ? Number.NaN
    : Number(storedSeconds);
  const seconds = Number.isFinite(rawSeconds) ? Math.min(120, Math.max(15, rawSeconds)) : 45;
  const noiseSuppression = storage?.getItem('odysseus.voice.noiseSuppression') === 'off' ? 'off' : 'standard';
  return {
    maxUtteranceMs: seconds * 1000,
    noiseSuppression,
  };
}

export function audioCaptureConstraints(noiseSuppression = 'standard') {
  return {
    audio: {
      echoCancellation: true,
      noiseSuppression: noiseSuppression !== 'off',
      autoGainControl: true,
    },
  };
}

export function audioProcessingNotice(settings, requestedNoiseSuppression = 'standard') {
  if (!settings) return 'Microphone processing capabilities were not reported by the audio device.';
  const unavailable = [];
  if (settings.echoCancellation === false) unavailable.push('echo cancellation');
  if (settings.autoGainControl === false) unavailable.push('automatic gain control');
  if (requestedNoiseSuppression !== 'off' && settings.noiseSuppression === false) unavailable.push('noise suppression');
  return unavailable.length
    ? `The microphone does not provide ${unavailable.join(', ')}; audio quality may be reduced.`
    : '';
}

export class SpeechEndpointing {
  constructor({ silenceMs = DEFAULT_SILENCE_MS, onsetMs = 40 } = {}) {
    this.silenceLimitMs = silenceMs;
    this.onsetLimitMs = onsetMs;
    this.noiseFloor = 0.002;
    this.onsetMs = 0;
    this.silenceMs = 0;
    this.speechDetected = false;
  }

  update(rms, elapsedMs) {
    const level = Number.isFinite(rms) ? Math.max(0, rms) : 0;
    const elapsed = Number.isFinite(elapsedMs) ? Math.min(100, Math.max(0, elapsedMs)) : 0;
    const threshold = Math.min(MAX_RMS_THRESHOLD, Math.max(MIN_RMS_THRESHOLD, this.noiseFloor * 2.4));

    if (!this.speechDetected) {
      if (level >= threshold) {
        this.onsetMs += elapsed;
        if (this.onsetMs >= this.onsetLimitMs) {
          this.speechDetected = true;
          this.silenceMs = 0;
        }
      } else {
        this.onsetMs = 0;
        const ambient = Math.min(level, 0.02);
        this.noiseFloor = Math.min(0.01, Math.max(0.001, this.noiseFloor + (ambient - this.noiseFloor) * 0.02));
      }
    } else if (level >= Math.max(0.0035, this.noiseFloor * 1.5)) {
      this.silenceMs = 0;
    } else {
      this.silenceMs += elapsed;
    }

    return {
      speechDetected: this.speechDetected,
      silenceMs: this.silenceMs,
      finalized: this.speechDetected && this.silenceMs >= this.silenceLimitMs,
    };
  }
}
