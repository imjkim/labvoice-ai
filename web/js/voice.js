// Speech output, with no dependencies. (Microphone capture lives in listener.js.)
// It uses the browser's built-in voices: free, offline-capable on most systems,
// and good enough to judge the interaction. A local neural TTS can replace it later.

export class Speaker {
  constructor() {
    this.synth = globalThis.speechSynthesis || null;
    this.voice = null;
    if (this.synth) {
      const pick = () => (this.voice = pickVoice(this.synth.getVoices()));
      pick();
      this.synth.addEventListener?.("voiceschanged", pick);
    }
  }

  get supported() {
    return Boolean(this.synth);
  }

  get speaking() {
    return Boolean(this.synth && (this.synth.speaking || this.synth.pending));
  }

  /**
   * Speak `text`. Callbacks: onstart(), onend({ interrupted }).
   * Returns immediately; use cancel() to barge in.
   */
  speak(text, { onstart, onend } = {}) {
    if (!this.synth || !text) {
      onend?.({ interrupted: false });
      return;
    }
    this.synth.cancel();
    const u = new SpeechSynthesisUtterance(text);
    if (this.voice) u.voice = this.voice;
    u.rate = 1.05;
    let interrupted = false;
    this._interrupt = () => (interrupted = true);
    u.onstart = () => onstart?.();
    u.onend = () => onend?.({ interrupted });
    u.onerror = () => onend?.({ interrupted: true });
    this.synth.speak(u);
  }

  /** Barge-in: stop talking immediately. */
  cancel() {
    if (!this.synth) return;
    this._interrupt?.();
    this.synth.cancel();
  }
}

/** Prefer a natural-sounding local English voice when the system has one. */
export function pickVoice(voices) {
  const english = (voices || []).filter((v) => /^en(-|_|$)/i.test(v.lang));
  const score = (v) =>
    (/natural|neural|enhanced|premium/i.test(v.name) ? 4 : 0) +
    (/en-US/i.test(v.lang) ? 2 : 0) +
    (v.localService ? 1 : 0);
  return english.sort((a, b) => score(b) - score(a))[0] || null;
}
