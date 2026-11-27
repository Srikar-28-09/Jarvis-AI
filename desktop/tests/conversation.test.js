import test from 'node:test';
import assert from 'node:assert/strict';
import { ConversationController, STATES } from '../src/conversation.js';

function fixture({ chat, speak, capture, stopCapture, transcribe } = {}) {
  const calls = { capture: 0, stopCapture: 0, transcribe: 0, transcripts: [], chat: 0, chatMessages: [], session: 0, sessionIds: [], speak: 0, cancel: 0, stopTts: 0, states: [] };
  const adapter = {
    onState: state => calls.states.push(state),
    async startCapture() { calls.capture++; if (capture) return capture(); },
    async stopCapture(...args) { calls.stopCapture++; if (stopCapture) return stopCapture(...args); },
    async transcribe(...args) { calls.transcribe++; return transcribe ? transcribe(...args) : 'spoken request'; },
    async ensureSession() { calls.session++; return 'session-1'; },
    async chat(...args) { calls.chat++; calls.sessionIds.push(args[0]); calls.chatMessages.push(args[1]); return chat ? chat(...args) : 'answer'; },
    async speak(...args) {
      calls.speak++;
      if (speak) return speak(...args);
      args[2]?.onPlaybackStarted?.();
    },
    async stopTts() { calls.stopTts++; },
    async cancelChat() { calls.cancel++; },
    onTranscript() {},
    onVoiceTranscript(text) { calls.transcripts.push(text); },
    onResponse() {},
  };
  return { controller: new ConversationController(adapter), calls };
}

test('voice transcription is reviewed before it can enter the chat pipeline', async () => {
  const { controller, calls } = fixture();
  await controller.startListening();
  await controller.submitAudio(new Blob(['audio']));
  assert.deepEqual(calls.states, [STATES.LISTENING, STATES.PROCESSING, STATES.TRANSCRIBING, STATES.IDLE]);
  assert.equal(calls.transcribe, 1);
  assert.deepEqual(calls.transcripts, ['spoken request']);
  assert.equal(calls.chat, 0);
  assert.equal(await controller.sendText('spoken request'), true);
  assert.equal(calls.chat, 1);
  assert.equal(calls.speak, 0);
  assert.equal(controller.state, STATES.IDLE);
});

test('ignores duplicate active submissions and same immediate typed request', async () => {
  let release;
  const { controller, calls } = fixture({ transcribe: () => new Promise(resolve => { release = resolve; }) });
  await controller.startListening();
  const turn = controller.submitAudio(new Blob(['audio']));
  assert.equal(await controller.submitAudio(new Blob(['again'])), false);
  while (!release) await new Promise(resolve => setImmediate(resolve));
  release('spoken request');
  await turn;
  await controller.sendText('same message');
  assert.equal(await controller.sendText('same message'), false);
  assert.equal(calls.transcribe, 1);
  assert.equal(calls.chat, 1);
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

test('text chat works without activating microphone capture or TTS', async () => {
  const { controller, calls } = fixture();

  assert.equal(await controller.sendText('text only'), true);

  assert.equal(calls.chat, 1);
  assert.equal(calls.capture, 0);
  assert.equal(calls.speak, 0);
  assert.equal(controller.state, STATES.IDLE);
});

test('five consecutive text turns reuse one session and reach the chat adapter', async () => {
  const { controller, calls } = fixture();

  for (let turn = 1; turn <= 5; turn++) {
    assert.equal(await controller.sendText(`request ${turn}`), true);
  }

  assert.equal(calls.session, 1);
  assert.equal(calls.chat, 5);
  assert.deepEqual(calls.sessionIds, Array(5).fill('session-1'));
});

test('desktop contextual follow-ups reuse the conversation session', async () => {
  const { controller, calls } = fixture();

  assert.equal(await controller.sendText('What is software engineering?'), true);
  assert.equal(await controller.sendText('Explain it simply.'), true);

  assert.deepEqual(calls.chatMessages, [
    'What is software engineering?',
    'Explain it simply.',
  ]);
  assert.deepEqual(calls.sessionIds, ['session-1', 'session-1']);
  assert.equal(calls.session, 1);
});

test('reviewed voice transcription enters the same session when explicitly sent', async () => {
  const { controller, calls } = fixture();
  await controller.startListening();
  assert.equal(await controller.submitAudio(new Blob(['voice audio'])), true);
  assert.equal(calls.chat, 0);
  assert.equal(await controller.sendText('spoken request'), true);
  assert.equal(await controller.sendText('Explain it simply.'), true);

  assert.equal(calls.transcribe, 1);
  assert.equal(calls.session, 1);
  assert.deepEqual(calls.sessionIds, ['session-1', 'session-1']);
  assert.deepEqual(calls.chatMessages, ['spoken request', 'Explain it simply.']);
});

test('manual stop finalizes audio and waits for transcription instead of discarding it', async () => {
  const recording = new Blob(['recording']);
  const { controller, calls } = fixture({
    stopCapture: submit => submit ? recording : null,
  });
  await controller.startListening();
  assert.equal(await controller.stopListening(), true);
  assert.equal(calls.transcribe, 1);
  assert.deepEqual(calls.transcripts, ['spoken request']);
  assert.equal(calls.chat, 0);
  assert.equal(controller.state, STATES.IDLE);
});

test('a reviewed voice request can retain spoken replies after explicit send', async () => {
  const { controller, calls } = fixture();
  await controller.startListening();
  assert.equal(await controller.submitAudio(new Blob(['voice audio'])), true);
  assert.equal(await controller.sendText('spoken request', { speakResponse: true }), true);
  assert.equal(calls.chat, 1);
  assert.equal(calls.speak, 1);
  assert.equal(controller.state, STATES.LISTENING);
});

test('empty manual stop does not submit an utterance', async () => {
  const { controller, calls } = fixture({ stopCapture: () => null });
  await controller.startListening();
  assert.equal(await controller.stopListening(), false);
  assert.equal(calls.transcribe, 0);
  assert.equal(calls.chat, 0);
  assert.equal(controller.state, STATES.IDLE);
});

test('empty or unrecognized audio never reaches the chat adapter', async () => {
  const { controller, calls } = fixture({ transcribe: async () => '   ' });
  await controller.startListening();
  assert.equal(await controller.submitAudio(new Blob()), false);
  assert.equal(calls.transcribe, 0);
  assert.equal(await controller.submitAudio(new Blob(['audio'])), false);
  assert.equal(calls.transcribe, 1);
  assert.equal(calls.chat, 0);
  assert.equal(controller.state, STATES.ERROR);
});

test('simultaneous manual stops share one finalized recording', async () => {
  let release;
  const recording = new Blob(['recording']);
  const { controller, calls } = fixture({
    stopCapture: submit => submit ? new Promise(resolve => { release = () => resolve(recording); }) : null,
  });
  await controller.startListening();
  const first = controller.stopListening();
  const second = controller.stopListening();
  while (!release) await new Promise(resolve => setImmediate(resolve));
  release();
  assert.deepEqual(await Promise.all([first, second]), [true, true]);
  assert.equal(calls.transcribe, 1);
  assert.equal(calls.chat, 0);
});

test('cancelling transcription prevents a stale transcript from reaching the composer', async () => {
  let release;
  const { controller, calls } = fixture({
    transcribe: () => new Promise(resolve => { release = resolve; }),
  });
  await controller.startListening();
  const turn = controller.submitAudio(new Blob(['audio']));
  while (!release) await new Promise(resolve => setImmediate(resolve));
  await controller.interrupt({ resume: false });
  release('stale transcript');
  assert.equal(await turn, false);
  assert.deepEqual(calls.transcripts, []);
  assert.equal(calls.chat, 0);
  assert.equal(controller.state, STATES.IDLE);
});

test('a stream disconnect recovers and a later request reuses the same session', async () => {
  let disconnect = true;
  const { controller, calls } = fixture({
    chat: async () => {
      if (disconnect) {
        disconnect = false;
        throw new Error('Chat stream was interrupted');
      }
      return 'recovered response';
    },
  });

  assert.equal(await controller.sendText('first request'), false);
  assert.equal(controller.state, STATES.ERROR);
  assert.equal(await controller.sendText('retry request'), true);
  assert.equal(calls.session, 1);
  assert.deepEqual(calls.sessionIds, ['session-1', 'session-1']);
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
