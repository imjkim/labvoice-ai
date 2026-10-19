// Microphone capture (hold-to-talk) and speech output, with no dependencies.
// Speech output uses the browser's built-in voices: free, offline-capable on most systems,
// and good enough to judge the interaction. A local neural TTS can replace it later.

const MIME_CANDIDATES = [
  ["audio/webm;codecs=opus", "webm"],
  ["audio/ogg;codecs=opus", "ogg"],
  ["audio/mp4", "mp4"], // Safari
];

export class Recorder {
  constructor() {
    this.stream = null;
    this.recorder = null;
    this.chunks = [];
    this.startedAt = 0;
    const found = MIME_CANDIDATES.find(([m]) => globalThis.MediaRecorder?.isTypeSupported?.(m));
    [this.mimeType, this.extension] = found || ["", "webm"];
  }

  get supported() {
    return Boolean(navigator.mediaDevices?.getUserMedia && globalThis.MediaRecorder);
  }

  /** Ask for the microphone once and keep the stream, so later presses start instantly. */
  async open() {
    if (this.stream) return;
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, channelCount: 1 },
    });
  }

  async start() {
    await this.open();
    this.chunks = [];
    this.recorder = new MediaRecorder(this.stream, this.mimeType ? { mimeType: this.mimeType } : undefined);
    this.recorder.ondataavailable = (e) => e.data.size && this.chunks.push(e.data);
    this.recorder.start();
    this.startedAt = performance.now();
  }

  /** Stops recording. Resolves to { blob, filename, durationMs }. */
  stop() {
    return new Promise((resolve) => {
      if (!this.recorder || this.recorder.state === "inactive") {
        resolve({ blob: null, filename: "", durationMs: 0 });
        return;
      }
      const durationMs = performance.now() - this.startedAt;
      this.recorder.onstop = () => {
        const blob = new Blob(this.chunks, { type: this.mimeType || "audio/webm" });
        resolve({ blob, filename: `speech.${this.extension}`, durationMs });
      };
      this.recorder.stop();
    });
  }

  get recording() {
    return this.recorder?.state === "recording";
  }
}

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
