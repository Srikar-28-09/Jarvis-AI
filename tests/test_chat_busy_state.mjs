import test from 'node:test';
import assert from 'node:assert/strict';
import {
  createChatBusyState,
  isChatRequestBusy,
  shouldDisableVoiceMic,
} from '../static/js/chatBusyState.mjs';

test('terminal completion unlocks the microphone independently of TTS state', () => {
  const transitions = [];
  const busy = createChatBusyState((active, requestId, outcome) => {
    transitions.push({ active, requestId, outcome });
  });

  busy.begin('text-request');
  assert.equal(busy.isBusy(), true);
  busy.finish('text-request', 'completed');

  assert.equal(busy.isBusy(), false);
  assert.deepEqual(transitions.at(-1), {
    active: false, requestId: 'text-request', outcome: 'completed',
  });
});

test('a stale request terminal callback cannot clear the newer request owner', () => {
  const busy = createChatBusyState();
  busy.begin('older-request');
  busy.begin('newer-request');

  assert.equal(busy.finish('older-request', 'aborted'), false);
  assert.equal(busy.isBusy(), true);
  assert.equal(busy.ownerId(), 'newer-request');
  assert.equal(busy.finish('newer-request', 'completed'), true);
  assert.equal(busy.isBusy(), false);
});

test('error, abort, repeated cleanup, and immediate consecutive voice turns settle safely', () => {
  const busy = createChatBusyState();
  busy.begin('voice-one');
  assert.equal(busy.finish('voice-one', 'error'), true);
  assert.equal(busy.finish('voice-one', 'aborted'), false);

  busy.begin('voice-two');
  assert.equal(busy.ownerId(), 'voice-two');
  assert.equal(busy.finish('voice-two', 'aborted'), true);
  assert.equal(busy.isBusy(), false);
});

test('authoritative completed chat state overrides stale streaming button presentation', () => {
  const staleButtonLooksBusy = () => true;
  assert.equal(isChatRequestBusy(false, staleButtonLooksBusy), false);
  assert.equal(isChatRequestBusy(true, () => false), true);
  assert.equal(isChatRequestBusy(undefined, staleButtonLooksBusy), true);
});

test('microphone UI follows chat/recorder state but ignores TTS playback state', () => {
  assert.equal(shouldDisableVoiceMic('idle', true), true);
  assert.equal(shouldDisableVoiceMic('idle', false), false);
  assert.equal(shouldDisableVoiceMic('recording', true), false); // stop remains available
  assert.equal(shouldDisableVoiceMic('transcribing', false), true);
  // TTS is intentionally not an input: completed chat state leaves the mic available.
  const ttsPlaying = true;
  assert.equal(shouldDisableVoiceMic('idle', false && ttsPlaying), false);
});
