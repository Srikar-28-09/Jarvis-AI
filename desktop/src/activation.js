export function focusActivatedInterface(loginPanel, usernameInput, messageInput) {
  if (loginPanel.hidden) messageInput.focus();
  else usernameInput.focus();
}

export function resolveActivationShortcut(savedShortcut, activeShortcut) {
  if (!savedShortcut || savedShortcut === 'Shift+Enter') return activeShortcut;
  return savedShortcut;
}

export function voiceInputNotice({ captureSupported, sttStatus }) {
  if (!captureSupported) {
    return 'Voice input is unavailable: this desktop webview does not support microphone capture. Text chat remains available.';
  }
  if (!sttStatus) {
    return 'Speech-to-text readiness could not be checked. Text chat remains available.';
  }
  if (sttStatus.provider === 'browser') {
    return 'Voice input is unavailable: browser speech recognition is not supported by this desktop client. Configure local or API speech-to-text; text chat remains available.';
  }
  if (!sttStatus.available) {
    return `Voice input is unavailable: ${sttStatus.reason || 'speech-to-text is not ready.'} Text chat remains available.`;
  }
  return `Voice input is configured with ${sttStatus.provider || 'the selected provider'}. Microphone permission is requested when you start listening; voice output is checked when a response is ready.`;
}

export function hostBridgeNotice(status) {
  if (status === 'listener_reachable') {
    return 'A service is listening on the host-bridge port; authenticated desktop-control readiness is verified when an action runs.';
  }
  if (status === 'starting') return 'Host bridge is still starting; desktop-control actions may not be ready yet.';
  return 'Host bridge is unavailable; desktop-control actions will not work. Text chat remains available.';
}

export function retainUnsentMessage(message, currentDraft) {
  return [String(message || '').trim(), String(currentDraft || '').trim()]
    .filter(Boolean)
    .join('\n');
}
