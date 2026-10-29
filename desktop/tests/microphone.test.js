import test from 'node:test';
import assert from 'node:assert/strict';
import { microphoneErrorMessage } from '../src/microphone.js';

test('permission denial explains the webview/system cause and offers retry and text fallback', () => {
  const message = microphoneErrorMessage({ name: 'NotAllowedError' });
  assert.match(message, /WebKitGTK or the system blocked microphone access/);
  assert.match(message, /Start listening to retry/);
  assert.match(message, /type a message/);
});

test('microphone failures are distinguished by browser error type', () => {
  assert.match(microphoneErrorMessage({ name: 'NotFoundError' }), /No microphone input device/);
  assert.match(microphoneErrorMessage({ name: 'NotReadableError' }), /could not be opened/);
  assert.match(microphoneErrorMessage({ name: 'SecurityError' }), /security context/);
  assert.match(microphoneErrorMessage({ name: 'OverconstrainedError' }), /audio settings/);
});

test('unknown microphone errors retain their type and provide retry guidance', () => {
  const message = microphoneErrorMessage({ name: 'UnknownError' });
  assert.match(message, /Microphone capture failed \(UnknownError\)/);
  assert.match(message, /Start listening to retry/);
});
