const retryHint = 'Choose Start listening to retry, or type a message.';

export function microphoneErrorMessage(error) {
  switch (error?.name) {
    case 'NotAllowedError':
      return `WebKitGTK or the system blocked microphone access. Check Odysseus microphone permissions, then ${retryHint}`;
    case 'SecurityError':
      return `Microphone capture is blocked in this webview security context. Restart Odysseus, then ${retryHint}`;
    case 'NotFoundError':
      return `No microphone input device is available. Connect or enable a microphone, then ${retryHint}`;
    case 'NotReadableError':
    case 'AbortError':
      return `The microphone could not be opened. It may be busy or blocked by the system; close other apps using it, then ${retryHint}`;
    case 'OverconstrainedError':
    case 'ConstraintNotSatisfiedError':
      return `The microphone does not support the requested audio settings. Check the selected input, then ${retryHint}`;
    default: {
      const name = error?.name ? ` (${error.name})` : '';
      return `Microphone capture failed${name}. Check the input device and ${retryHint}`;
    }
  }
}
