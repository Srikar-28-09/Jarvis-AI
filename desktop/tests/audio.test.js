import test from 'node:test';
import assert from 'node:assert/strict';
import { encodePcmWav } from '../src/audio.js';

test('PCM capture is encoded as mono 16-bit little-endian WAV', async () => {
  const blob = encodePcmWav([new Float32Array([-1, 0, 1])], 16000);
  const bytes = new Uint8Array(await blob.arrayBuffer());
  const view = new DataView(bytes.buffer);
  assert.equal(blob.type, 'audio/wav');
  assert.equal(new TextDecoder().decode(bytes.slice(0, 4)), 'RIFF');
  assert.equal(new TextDecoder().decode(bytes.slice(8, 12)), 'WAVE');
  assert.equal(view.getUint16(20, true), 1);
  assert.equal(view.getUint16(22, true), 1);
  assert.equal(view.getUint32(24, true), 16000);
  assert.equal(view.getUint16(34, true), 16);
  assert.deepEqual([view.getInt16(44, true), view.getInt16(46, true), view.getInt16(48, true)], [-32768, 0, 32767]);
});

test('PCM WAV encoding rejects empty samples and invalid sample rates', () => {
  assert.throws(() => encodePcmWav([], 16000), /requires samples/);
  assert.throws(() => encodePcmWav([new Float32Array([0])], 0), /valid sample rate/);
});
