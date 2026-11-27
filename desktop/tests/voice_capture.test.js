import test from 'node:test';
import assert from 'node:assert/strict';
import {
  DEFAULT_MAX_UTTERANCE_MS,
  DEFAULT_SILENCE_MS,
  audioCaptureConstraints,
  audioProcessingNotice,
  readVoiceCaptureSettings,
  SpeechEndpointing,
} from '../../static/js/voice_capture.mjs';

function feed(detector, rms, durationMs) {
  let state;
  for (let elapsed = 0; elapsed < durationMs; elapsed += 20) {
    state = detector.update(rms, Math.min(20, durationMs - elapsed));
  }
  return state;
}

test('capture defaults require two seconds of silence and allow a 45-second utterance', () => {
  assert.equal(DEFAULT_SILENCE_MS, 2000);
  assert.equal(DEFAULT_MAX_UTTERANCE_MS, 45000);
  assert.deepEqual(readVoiceCaptureSettings({ getItem: () => null }), {
    maxUtteranceMs: 45000,
    noiseSuppression: 'standard',
  });
});

test('speech resuming after a short pause resets the silence interval', () => {
  const detector = new SpeechEndpointing();
  feed(detector, 0.01, 100);
  assert.equal(detector.speechDetected, true);
  feed(detector, 0, 1500);
  const resumed = feed(detector, 0.01, 100);
  assert.equal(resumed.finalized, false);
  assert.equal(resumed.silenceMs, 0);
  assert.equal(feed(detector, 0, 1980).finalized, false);
  assert.equal(feed(detector, 0, 20).finalized, true);
});

test('multiple pauses do not finalize until two continuous seconds have elapsed', () => {
  const detector = new SpeechEndpointing();
  feed(detector, 0.01, 100);
  feed(detector, 0, 700);
  feed(detector, 0.012, 80);
  feed(detector, 0, 1100);
  feed(detector, 0.01, 60);
  assert.equal(feed(detector, 0, 1990).finalized, false);
  assert.equal(feed(detector, 0, 20).finalized, true);
});

test('quiet speech is retained while low-level background noise alone does not start an utterance', () => {
  const noise = new SpeechEndpointing();
  feed(noise, 0.003, 1500);
  assert.equal(noise.speechDetected, false);

  const quietSpeech = new SpeechEndpointing();
  feed(quietSpeech, 0.005, 100);
  assert.equal(quietSpeech.speechDetected, true);
});

test('speech remains detectable above a modest changing noise floor', () => {
  const detector = new SpeechEndpointing();
  feed(detector, 0.003, 1000);
  feed(detector, 0.008, 100);
  assert.equal(detector.speechDetected, true);
});

test('capture preferences are bounded and audio processing constraints are explicit', () => {
  const preferences = readVoiceCaptureSettings({
    getItem: key => key.endsWith('maxUtteranceSeconds') ? '999' : 'off',
  });
  assert.deepEqual(preferences, { maxUtteranceMs: 120000, noiseSuppression: 'off' });
  assert.deepEqual(audioCaptureConstraints('standard'), {
    audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
  });
  assert.deepEqual(audioCaptureConstraints('off').audio.noiseSuppression, false);
  assert.match(audioProcessingNotice({ echoCancellation: false, noiseSuppression: false, autoGainControl: false }), /echo cancellation.*automatic gain control.*noise suppression/);
});
