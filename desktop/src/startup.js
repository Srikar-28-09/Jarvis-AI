export function singleFlight(operation) {
  let pending = null;
  return (...args) => {
    if (!pending) {
      pending = Promise.resolve()
        .then(() => operation(...args))
        .finally(() => { pending = null; });
    }
    return pending;
  };
}

export async function pollBackendReadiness({
  probe,
  attempts = 180,
  intervalMs = 1000,
  onRetry = () => {},
  isTerminalError = () => false,
  wait = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds)),
}) {
  let lastError;
  for (let attempt = 0; attempt < attempts; attempt++) {
    try {
      const status = await probe();
      if (status?.healthy) return status;
      lastError = new Error('Backend readiness endpoint has not reported ready.');
    } catch (error) {
      lastError = error;
      if (isTerminalError(error)) throw error;
    }
    if (attempt + 1 < attempts) {
      onRetry(attempt + 1);
      await wait(intervalMs);
    }
  }
  throw new Error(
    `Backend did not become ready after ${attempts} checks: ${lastError?.message || 'readiness check failed'}`,
  );
}
