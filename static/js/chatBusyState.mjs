/** Request-owned foreground busy state; stale terminal callbacks cannot unlock a newer send. */
export function isChatRequestBusy(authoritativeState, fallbackCheck = () => false) {
  if (typeof authoritativeState === 'boolean') return authoritativeState;
  return !!fallbackCheck();
}

export function shouldDisableVoiceMic(recorderState, chatRequestBusy) {
  const recorderBusy = recorderState === 'starting' || recorderState === 'transcribing';
  const recordingCanBeStopped = recorderState === 'recording';
  return recorderBusy || (!!chatRequestBusy && !recordingCanBeStopped);
}

export function createChatBusyState(onChange = () => {}) {
  let ownerId = null;

  return {
    begin(requestId) {
      const nextId = String(requestId || '').trim();
      if (!nextId) return false;
      if (ownerId === nextId) return true;
      ownerId = nextId;
      onChange(true, ownerId, 'started');
      return true;
    },
    finish(requestId, outcome = 'completed') {
      const terminalId = String(requestId || '').trim();
      if (!ownerId || !terminalId || terminalId !== ownerId) return false;
      const finishedId = ownerId;
      ownerId = null;
      onChange(false, finishedId, outcome);
      return true;
    },
    isBusy() {
      return ownerId !== null;
    },
    ownerId() {
      return ownerId;
    },
  };
}
