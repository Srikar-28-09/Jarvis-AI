import test from 'node:test';
import assert from 'node:assert/strict';
import { ConversationController, STATES } from '../src/conversation.js';

function fixture({ chat, speak, capture, stopCapture } = {}) {
  const calls = { capture: 0, stopCapture: 0, transcribe: 0, chat: 0, speak: 0, cancel: 0, stopTts: 0, states: [] };
  const adapter = {
    onState: state => calls.states.push(state),
    async startCapture() { calls.capture++; if (capture) return capture(); },
    async stopCapture(...args) { calls.stopCapture++; if (stopCapture) return stopCapture(...args); },
    async transcribe() { calls.transcribe++; return 'spoken request'; },
    async ensureSession() { return 'session-1'; },
    async chat(...args) { calls.chat++; return chat ? chat(...args) : 'answer'; },
    async speak(...args) {
      calls.speak++;
      if (speak) return speak(...args);
      args[2]?.onPlaybackStarted?.();
    },
    async stopTts() { calls.stopTts++; },
    async cancelChat() { calls.cancel++; },
    onTranscript() {}, onResponse() {},
  };
  return { controller: new ConversationController(adapter), calls };
}

test('transitions through transcription, thinking, synthesis, playback and automatically listens again', async () => {
  const { controller, calls } = fixture();
  await controller.startListening();
  await controller.submitAudio(new Blob(['audio']));
  assert.deepEqual(calls.states, [STATES.LISTENING, STATES.TRANSCRIBING, STATES.THINKING, STATES.SYNTHESIZING, STATES.SPEAKING, STATES.LISTENING]);
  assert.equal(calls.transcribe, 1);
  assert.equal(calls.chat, 1);
  assert.equal(calls.speak, 1);
  await controller.sendText('follow up');
  assert.equal(calls.chat, 2);
  assert.equal(calls.speak, 2);
  assert.equal(controller.state, STATES.LISTENING);
});

test('ignores duplicate active submissions and same immediate typed request', async () => {
  let release;
  let first = true;
  const { controller, calls } = fixture({ chat: () => first ? (first = false, new Promise(resolve => { release = resolve; })) : 'answer' });
  await controller.startListening();
  const turn = controller.submitAudio(new Blob(['audio']));
  assert.equal(await controller.submitAudio(new Blob(['again'])), false);
  while (!release) await new Promise(resolve => setImmediate(resolve));
  release('answer');
  await turn;
  controller.listeningWanted = false;
  await controller.sendText('same message');
  assert.equal(await controller.sendText('same message'), false);
  assert.equal(calls.chat, 2);
});

test('cancellation ignores a stale response and stops TTS/request once', async () => {
  let release;
  const { controller, calls } = fixture({ chat: () => new Promise(resolve => { release = resolve; }) });
  await controller.startListening();
  const turn = controller.sendText('cancel me');
  await new Promise(resolve => setImmediate(resolve));
  await controller.interrupt({ resume: true });
  release('late answer');
  await turn;
  assert.equal(calls.cancel, 1);
  assert.equal(calls.stopTts, 1);
  assert.equal(calls.speak, 0);
  assert.equal(controller.state, STATES.LISTENING);
});

test('interrupting playback clears audio before the next turn', async () => {
  let finishSpeech;
  const { controller, calls } = fixture({ speak: (_text, _id, events) => {
    events.onPlaybackStarted();
    return new Promise(resolve => { finishSpeech = resolve; });
  } });
  await controller.startListening();
  const turn = controller.sendText('say something');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(controller.state, STATES.SPEAKING);
  await controller.interrupt({ resume: true });
  finishSpeech();
  await turn;
  assert.equal(calls.stopTts, 1);
  assert.equal(controller.state, STATES.LISTENING);
});

test('hiding releases microphone and does not terminate backend session', async () => {
  const { controller, calls } = fixture();
  await controller.startListening();
  await controller.hide();
  assert.equal(calls.stopCapture, 1);
  assert.equal(calls.cancel, 0);
  assert.equal(controller.state, STATES.IDLE);
});

test('TTS failure leaves the response available and resumes listening', async () => {
  const { controller, calls } = fixture({ speak: async () => { throw new Error('audio unavailable'); } });
  await controller.startListening();
  await controller.sendText('read the answer');
  assert.equal(controller.state, STATES.LISTENING);
  assert.equal(calls.chat, 1);
  assert.equal(calls.capture, 2);
});

test('synthesis is not shown as playback before the playback-start callback', async () => {
  let finishSynthesis;
  let markPlaybackStarted;
  const { controller, calls } = fixture({ speak: (_text, _id, events) => {
    markPlaybackStarted = events.onPlaybackStarted;
    return new Promise(resolve => { finishSynthesis = resolve; });
  } });
  await controller.startListening();
  const turn = controller.sendText('wait for audio');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(controller.state, STATES.SYNTHESIZING);
  markPlaybackStarted();
  assert.equal(controller.state, STATES.SPEAKING);
  finishSynthesis();
  await turn;
  assert.equal(controller.state, STATES.LISTENING);
  assert.ok(calls.states.includes(STATES.SYNTHESIZING));
});

test('chat errors enter recovery and a later activation can listen again', async () => {
  let fail = true;
  const { controller } = fixture({ chat: async () => { if (fail) throw new Error('backend unavailable'); return 'back again'; } });
  await controller.startListening();
  assert.equal(await controller.sendText('first attempt'), false);
  assert.equal(controller.state, STATES.ERROR);
  fail = false;
  await controller.startListening();
  assert.equal(controller.state, STATES.LISTENING);
  await controller.sendText('second attempt');
  assert.equal(controller.state, STATES.LISTENING);
});

test('overlapping activations share one pending microphone start', async () => {
  let release;
  const { controller, calls } = fixture({ capture: () => new Promise(resolve => { release = resolve; }) });
  const first = controller.startListening();
  const second = controller.startListening();
  while (!release) await new Promise(resolve => setImmediate(resolve));
  assert.equal(calls.capture, 1);
  release();
  await Promise.all([first, second]);
  assert.equal(controller.state, STATES.LISTENING);
});

test('typing during microphone shutdown submits exactly one request', async () => {
  let release;
  const { controller, calls } = fixture({ stopCapture: () => new Promise(resolve => { release = resolve; }) });
  await controller.startListening();
  const first = controller.sendText('typed once');
  const second = controller.sendText('typed once');
  assert.equal(await second, false);
  release();
  await first;
  assert.equal(calls.chat, 1);
});

test('dismissing while microphone permission is pending closes the late stream', async () => {
  let release;
  const { controller, calls } = fixture({ capture: () => new Promise(resolve => { release = resolve; }) });
  const starting = controller.startListening();
  while (!release) await new Promise(resolve => setImmediate(resolve));
  await controller.hide();
  release();
  await starting;
  assert.equal(controller.state, STATES.IDLE);
  assert.ok(calls.stopCapture >= 2);
});
