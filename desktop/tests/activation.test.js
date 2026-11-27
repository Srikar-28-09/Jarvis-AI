import test from 'node:test';
import assert from 'node:assert/strict';
import {
  focusActivatedInterface,
  hostBridgeNotice,
  resolveActivationShortcut,
  retainUnsentMessage,
  voiceInputNotice,
} from '../src/activation.js';

test('UI activation focuses the message field without starting voice capture', () => {
  const focused = [];
  focusActivatedInterface(
    { hidden: true },
    { focus: () => focused.push('username') },
    { focus: () => focused.push('message') },
  );
  assert.deepEqual(focused, ['message']);
});

test('UI activation focuses sign-in when authentication is required', () => {
  const focused = [];
  focusActivatedInterface(
    { hidden: false },
    { focus: () => focused.push('username') },
    { focus: () => focused.push('message') },
  );
  assert.deepEqual(focused, ['username']);
});

test('legacy activation shortcut migrates to the registered default', () => {
  assert.equal(resolveActivationShortcut('Shift+Enter', 'Ctrl+Alt+Shift+J'), 'Ctrl+Alt+Shift+J');
  assert.equal(resolveActivationShortcut(null, 'Ctrl+Alt+Shift+J'), 'Ctrl+Alt+Shift+J');
  assert.equal(resolveActivationShortcut('Ctrl+Alt+K', 'Ctrl+Alt+Shift+J'), 'Ctrl+Alt+K');
});

test('text readiness is independent of host bridge and voice availability', () => {
  assert.match(hostBridgeNotice('inactive'), /Text chat remains available/);
  assert.match(hostBridgeNotice('listener_reachable'), /authenticated desktop-control readiness/);
  assert.match(voiceInputNotice({
    captureSupported: true,
    sttStatus: { available: false, reason: 'STT is disabled' },
  }), /Text chat remains available/);
  assert.match(voiceInputNotice({
    captureSupported: true,
    sttStatus: { available: false, provider: 'browser' },
  }), /browser speech recognition is not supported/);
  assert.match(voiceInputNotice({
    captureSupported: true,
    sttStatus: { available: true, provider: 'local' },
  }), /configured with local/);
});

test('failed text submissions can be restored without discarding a newer draft', () => {
  assert.equal(retainUnsentMessage('unsent message', 'new draft'), 'unsent message\nnew draft');
});
