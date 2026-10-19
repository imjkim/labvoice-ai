// Hands-free listening: microphone -> 20 ms frames -> voice activity detection -> WAV per question.
// The browser only allows the microphone after a click, so start() must be called from one.

import { EnergyVad, encodeWav, resample } from "./vad.js";

const WHISPER_RATE = 16000;

export class HandsFreeListener {
  /**
   * callbacks:
   *   onSpeechStart()                     someone started talking
   *   onSpeech({ blob, filename, durationMs })   a finished question, as 16 kHz WAV
   *   onLevel(level)                      0..1 loudness above the room, ~50 times a second
   */
  constructor(callbacks = {}) {
    this.cb = callbacks;
    this.ctx = null;
    this.stream = null;
    this.node = null;
    this.vad = null;
    this.paused = false;
  }

  get supported() {
    return Boolean(navigator.mediaDevices?.getUserMedia && (globalThis.AudioContext || globalThis.webkitAudioContext));
  }

  get active() {
    return Boolean(this.ctx);
  }

  async start() {
    if (this.ctx) return;
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 },
    });
    const Ctx = globalThis.AudioContext || globalThis.webkitAudioContext;
    this.ctx = new Ctx();
    await this.ctx.audioWorklet.addModule(new URL("./capture-worklet.js", import.meta.url));
    const source = this.ctx.createMediaStreamSource(this.stream);
    this.node = new AudioWorkletNode(this.ctx, "capture");
    this.vad = new EnergyVad(this.ctx.sampleRate);
    this.node.port.onmessage = (e) => this._onFrame(e.data);
    source.connect(this.node);
    this.node.connect(this.ctx.destination); // outputs silence; connecting keeps the worklet running
    await this.ctx.resume();
  }

  async stop() {
    this.node?.port && (this.node.port.onmessage = null);
    this.stream?.getTracks().forEach((t) => t.stop()); // turns off the browser's mic indicator
    await this.ctx?.close().catch(() => {});
    this.ctx = this.stream = this.node = this.vad = null;
    this.cb.onLevel?.(0);
  }

  /** Stop reacting to sound (while the assistant talks, so it doesn't hear itself). */
  pause() {
    this.paused = true;
    this.vad?.cancel();
    this.cb.onLevel?.(0);
  }

  resume() {
    this.paused = false;
  }

  _onFrame(frame) {
    if (!this.vad || this.paused) return;
    const event = this.vad.push(frame);
    this.cb.onLevel?.(this.vad.level);
    if (!event) return;
    if (event.type === "start") this.cb.onSpeechStart?.();
    if (event.type === "end") {
      const pcm = resample(event.samples, this.ctx.sampleRate, WHISPER_RATE);
      const blob = new Blob([encodeWav(pcm, WHISPER_RATE)], { type: "audio/wav" });
      this.cb.onSpeech?.({ blob, filename: "speech.wav", durationMs: event.durationMs });
    }
  }
}
