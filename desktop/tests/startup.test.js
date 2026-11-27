import test from 'node:test';
import assert from 'node:assert/strict';
import { pollBackendReadiness, singleFlight } from '../src/startup.js';

test('startup calls are coalesced while one connection attempt is pending', async () => {
  let release;
  let calls = 0;
  const connect = singleFlight(() => {
    calls++;
    return new Promise(resolve => { release = resolve; });
  });

  const first = connect();
  const second = connect();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(calls, 1);
  release('ready');
  assert.deepEqual(await Promise.all([first, second]), ['ready', 'ready']);
});

test('readiness polling retries failures and recovers when the backend becomes healthy', async () => {
  let attempts = 0;
  const retries = [];
  const status = await pollBackendReadiness({
    attempts: 4,
    intervalMs: 1,
    probe: async () => {
      attempts++;
      if (attempts === 1) throw new Error('backend unavailable');
      if (attempts === 2) return { healthy: false };
      return { healthy: true, authenticated: true };
    },
    onRetry: attempt => retries.push(attempt),
    wait: async () => {},
  });

  assert.deepEqual(status, { healthy: true, authenticated: true });
  assert.equal(attempts, 3);
  assert.deepEqual(retries, [1, 2]);
});

test('readiness polling reports the final failure instead of claiming success', async () => {
  await assert.rejects(
    pollBackendReadiness({
      attempts: 2,
      probe: async () => { throw new Error('bridge unavailable'); },
      wait: async () => {},
    }),
    /did not become ready after 2 checks: bridge unavailable/,
  );
});

test('readiness polling stops immediately when startup reports a terminal service failure', async () => {
  const failure = Object.assign(new Error('Docker Compose failed'), { terminal: true });
  let probes = 0;
  await assert.rejects(
    pollBackendReadiness({
      attempts: 180,
      probe: async () => { probes++; throw failure; },
      isTerminalError: error => Boolean(error.terminal),
      wait: async () => {},
    }),
    /Docker Compose failed/,
  );
  assert.equal(probes, 1);
});
