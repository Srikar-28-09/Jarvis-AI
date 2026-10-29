export const STATES = Object.freeze({
  IDLE: 'idle', LISTENING: 'listening', TRANSCRIBING: 'transcribing',
  THINKING: 'thinking', SYNTHESIZING: 'synthesizing', SPEAKING: 'speaking',
  INTERRUPTED: 'interrupted', ERROR: 'error',
});

const ALLOWED = {
  idle: ['listening', 'thinking', 'error'],
  listening: ['idle', 'transcribing', 'thinking', 'interrupted', 'error'],
  transcribing: ['thinking', 'listening', 'idle', 'interrupted', 'error'],
  thinking: ['synthesizing', 'listening', 'idle', 'interrupted', 'error'],
  synthesizing: ['speaking', 'listening', 'idle', 'interrupted', 'error'],
  speaking: ['listening', 'idle', 'interrupted', 'error'],
  interrupted: ['listening', 'idle', 'thinking', 'error'],
  error: ['listening', 'idle', 'thinking', 'interrupted'],
};

export class ConversationController {
  constructor(adapter) {
    this.adapter = adapter;
    this.state = STATES.IDLE;
    this.sessionId = null;
    this.activeTurn = null;
    this.turnCounter = 0;
    this.lastTurnKey = '';
    this.lastSpokenTurn = '';
    this.listeningWanted = false;
    this.runId = '';
    this._inFlight = false;
    this._lockToken = 0;
    this._lifecycleToken = 0;
    this._startPromise = null;
  }

  transition(next, detail = '') {
    if (next === this.state) return;
    if (!ALLOWED[this.state]?.includes(next)) throw new Error(`Invalid voice transition: ${this.state} → ${next}`);
    this.state = next;
    this.adapter.onState?.(next, detail);
  }

  async startListening() {
    this.listeningWanted = true;
    if (this.state === STATES.LISTENING || this._inFlight || this.state === STATES.SPEAKING) return;
    return this._beginCapture();
  }

  async _beginCapture() {
    const token = this._lifecycleToken;
    if (this._startPromise) {
      await this._startPromise;
      if (token === this._lifecycleToken && this.listeningWanted && this.state !== STATES.LISTENING) return this._beginCapture();
      return this.state === STATES.LISTENING;
    }
    const pending = (async () => {
      await this.adapter.startCapture();
      if (token !== this._lifecycleToken || !this.listeningWanted) {
        await this.adapter.stopCapture(false);
        return false;
      }
      this.transition(STATES.LISTENING);
      return true;
    })();
    this._startPromise = pending;
    try { return await pending; }
    finally { if (this._startPromise === pending) this._startPromise = null; }
  }

  async stopListening() {
    this.listeningWanted = false;
    this._lifecycleToken++;
    await this.adapter.stopCapture(false);
    if (this.state === STATES.LISTENING) this.transition(STATES.IDLE);
  }

  async submitAudio(blob) {
    if (this._inFlight || this.state !== STATES.LISTENING) return false;
    this._inFlight = true;
    const lockToken = ++this._lockToken;
    this.listeningWanted = true;
    const turn = ++this.turnCounter;
    this.activeTurn = this.adapter.createRequestId?.() || `voice-${globalThis.crypto?.randomUUID?.() || `${Date.now()}-${turn}`}`;
    const turnId = this.activeTurn;
    this.transition(STATES.TRANSCRIBING);
    try {
      await this.adapter.stopCapture(false);
      if (this.activeTurn !== turnId) return false;
      const text = (await this.adapter.transcribe(blob, turnId)).trim();
      if (!text) throw new Error('No speech was recognized. Try again or type a message.');
      return await this._send(text, turnId);
    } catch (error) {
      if (this.activeTurn === turnId) await this._recover(error);
      return false;
    } finally {
      if (this._lockToken === lockToken) this._inFlight = false;
    }
  }

  async sendText(text) {
    const cleaned = String(text || '').trim();
    if (!cleaned || this._inFlight) return false;
    this._inFlight = true;
    const lockToken = ++this._lockToken;
    this._lifecycleToken++;
    try {
      if (this.state === STATES.LISTENING) await this.adapter.stopCapture(false);
      if (this._lockToken !== lockToken) return false;
    } catch (error) {
      await this._recover(error);
      return false;
    }
    this.listeningWanted = true;
    const key = cleaned.toLocaleLowerCase();
    const now = Date.now();
    if (this.lastTurnKey === key && now - this._lastSentAt < 1200) { this._inFlight = false; return false; }
    this.lastTurnKey = key;
    this._lastSentAt = now;
    const turn = ++this.turnCounter;
    this.activeTurn = this.adapter.createRequestId?.() || `text-${globalThis.crypto?.randomUUID?.() || `${Date.now()}-${turn}`}`;
    const turnId = this.activeTurn;
    try { return await this._send(cleaned, turnId); }
    catch (error) { if (this.activeTurn === turnId) await this._recover(error); return false; }
    finally { if (this._lockToken === lockToken) this._inFlight = false; }
  }

  async _send(text, turnId) {
    if (!this.sessionId) this.sessionId = await this.adapter.ensureSession();
    if (this.activeTurn !== turnId) return false;
    this.adapter.onTranscript?.(text);
    this.adapter.onResponse?.('');
    this.transition(STATES.THINKING);
    const result = await this.adapter.chat(this.sessionId, text, turnId, {
      onRunId: id => { if (this.activeTurn === turnId) this.runId = id || ''; },
      onDelta: value => { if (this.activeTurn === turnId) this.adapter.onResponse?.(value, true); },
    });
    if (this.activeTurn !== turnId) return false;
    const answer = String(result || '').trim();
    this.adapter.onResponse?.(answer);
    if (!answer) throw new Error('Odysseus returned an empty response. You can try again.');
    if (this.lastSpokenTurn !== turnId && this.listeningWanted) {
      this.lastSpokenTurn = turnId;
      this.transition(STATES.SYNTHESIZING);
      try {
        await this.adapter.speak(answer, turnId, {
          onPlaybackStarted: () => {
            if (this.activeTurn === turnId && this.state === STATES.SYNTHESIZING) this.transition(STATES.SPEAKING);
          },
        });
      } catch (error) {
        if (this.activeTurn === turnId) this.adapter.onNotice?.(`Jarvis voice could not play: ${error.message || error}`);
      }
    }
    if (this.activeTurn !== turnId) return false;
    this.activeTurn = null;
    this.runId = '';
    if (this.listeningWanted) {
      try { await this._beginCapture(); }
      catch (error) { await this._recover(error); }
    } else this.transition(STATES.IDLE);
    return true;
  }

  async interrupt({ resume = true } = {}) {
    const oldTurn = this.activeTurn;
    const session = this.sessionId;
    const runId = this.runId;
    this.listeningWanted = resume;
    this.activeTurn = null;
    this.runId = '';
    this._inFlight = false;
    this._lockToken++;
    this._lifecycleToken++;
    await this.adapter.stopCapture(false).catch(() => {});
    await this.adapter.stopTts().catch(() => {});
    if (session && oldTurn) await this.adapter.cancelChat(session, runId, oldTurn).catch(() => {});
    if (resume) {
      this.transition(STATES.INTERRUPTED);
      try { await this.adapter.startCapture(); this.transition(STATES.LISTENING); }
      catch (error) { await this._recover(error); }
    } else if (this.state !== STATES.IDLE) this.transition(STATES.IDLE);
  }

  async hide() { await this.interrupt({ resume: false }); }

  async _recover(error) {
    this.activeTurn = null;
    this.runId = '';
    this._inFlight = false;
    this._lifecycleToken++;
    await this.adapter.stopCapture(false).catch(() => {});
    await this.adapter.stopTts().catch(() => {});
    this.transition(STATES.ERROR, error?.message || String(error));
  }
}
