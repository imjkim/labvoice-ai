// Voice activity detection and WAV encoding. Pure functions and classes with no browser APIs,
// so they're unit-tested with Node (web/js/vad.test.js).
//
// The detector is energy-based with an adaptive noise floor: a lab has steady background noise
// (fans, stainers, a centrifuge), so "speech" means "clearly louder than the recent floor",
// not "louder than a fixed level". It needs no model download, which matters offline.

/** Loudness of one frame in dBFS (0 = full scale; silence is around -90). */
export function frameDb(frame) {
  let sum = 0;
  for (let i = 0; i < frame.length; i++) sum += frame[i] * frame[i];
  const rms = Math.sqrt(sum / Math.max(frame.length, 1));
  return 20 * Math.log10(rms + 1e-9);
}

export const VAD_DEFAULTS = {
  frameMs: 20,
  startAboveFloorDb: 12, // a frame counts as speech when this much louder than the floor...
  minSpeechDb: -52, //       ...and louder than this absolute level (ignores hiss in a silent room)
  endAboveFloorDb: 7, //    once talking, frames quieter than floor + this count as silence
  minSpeechMs: 160, //      sustained speech needed to start (ignores clicks, coughs, a dropped rack)
  silenceMs: 750, //        trailing silence that ends a question
  preRollMs: 300, //        audio kept from just before the start, so the first word isn't clipped
  maxUtteranceMs: 15000, // hard stop for one question
};

/**
 * Feed fixed-size frames with push(). It returns at most one event per frame:
 *   { type: "start" }                       speech began
 *   { type: "end", samples, durationMs }    speech ended; samples include the pre-roll
 *   { type: "discard" }                     a start that turned out too short to keep
 * `level` (0..1) is how far the latest frame is above the floor, for a live meter.
 */
export class EnergyVad {
  constructor(sampleRate, options = {}) {
    this.o = { ...VAD_DEFAULTS, ...options };
    this.sampleRate = sampleRate;
    this.frameMs = this.o.frameMs;
    this.floorDb = null;
    this.level = 0;
    this.reset();
  }

  reset() {
    this.state = "silence"; // silence -> candidate -> speech
    this.preRoll = []; // recent frames while silent
    this.frames = []; // frames of the current utterance
    this.loudRun = 0;
    this.quietRun = 0;
  }

  get frameSamples() {
    return Math.round((this.sampleRate * this.frameMs) / 1000);
  }

  _updateFloor(db) {
    if (this.floorDb === null) this.floorDb = db;
    // Fall fast (the room got quieter), rise slowly (so speech doesn't become the new floor).
    this.floorDb = db < this.floorDb ? 0.7 * this.floorDb + 0.3 * db : 0.995 * this.floorDb + 0.005 * db;
    this.floorDb = Math.max(this.floorDb, -90);
  }

  push(frame) {
    const o = this.o;
    const db = frameDb(frame);
    if (this.floorDb === null) this.floorDb = db;
    const aboveFloor = db - this.floorDb;
    this.level = Math.max(0, Math.min(1, aboveFloor / 30));
    const loud = aboveFloor >= o.startAboveFloorDb && db >= o.minSpeechDb;
    const quiet = aboveFloor < o.endAboveFloorDb || db < o.minSpeechDb;

    if (this.state === "silence") {
      this._updateFloor(db);
      if (loud) {
        this.state = "candidate";
        this.frames = [...this.preRoll, frame];
        this.loudRun = 1;
        return null;
      }
      this.preRoll.push(frame);
      const keep = Math.ceil(o.preRollMs / this.frameMs);
      if (this.preRoll.length > keep) this.preRoll.splice(0, this.preRoll.length - keep);
      return null;
    }

    this.frames.push(frame);

    if (this.state === "candidate") {
      if (loud) {
        this.loudRun += 1;
        if (this.loudRun * this.frameMs >= o.minSpeechMs) {
          this.state = "speech";
          this.quietRun = 0;
          return { type: "start" };
        }
      } else {
        // Too short to be speech: go back to waiting. The frames rejoin the pre-roll.
        this.preRoll = this.frames.slice(-Math.ceil(o.preRollMs / this.frameMs));
        this.frames = [];
        this.state = "silence";
        this.loudRun = 0;
      }
      return null;
    }

    // state === "speech"
    this.quietRun = quiet ? this.quietRun + 1 : 0;
    const durationMs = this.frames.length * this.frameMs;
    if (this.quietRun * this.frameMs >= o.silenceMs || durationMs >= o.maxUtteranceMs) {
      const samples = concat(this.frames);
      this.reset();
      return { type: "end", samples, durationMs };
    }
    return null;
  }

  /** Abandon any utterance in progress (e.g. while the assistant is speaking). */
  cancel() {
    const wasActive = this.state !== "silence";
    this.reset();
    return wasActive ? { type: "discard" } : null;
  }
}

export function concat(frames) {
  const total = frames.reduce((n, f) => n + f.length, 0);
  const out = new Float32Array(total);
  let offset = 0;
  for (const f of frames) {
    out.set(f, offset);
    offset += f.length;
  }
  return out;
}

/** Linear-interpolation resample. Whisper works at 16 kHz, so sending 48 kHz is 3x wasted upload. */
export function resample(samples, fromRate, toRate) {
  if (fromRate === toRate) return samples;
  const ratio = fromRate / toRate;
  const out = new Float32Array(Math.floor(samples.length / ratio));
  for (let i = 0; i < out.length; i++) {
    const pos = i * ratio;
    const j = Math.floor(pos);
    const frac = pos - j;
    const a = samples[j];
    const b = j + 1 < samples.length ? samples[j + 1] : a;
    out[i] = a + (b - a) * frac;
  }
  return out;
}

/** Mono 16-bit PCM WAV bytes. */
export function encodeWav(samples, sampleRate) {
  const bytes = new ArrayBuffer(44 + samples.length * 2);
  const v = new DataView(bytes);
  const text = (offset, s) => [...s].forEach((c, i) => v.setUint8(offset + i, c.charCodeAt(0)));
  text(0, "RIFF");
  v.setUint32(4, 36 + samples.length * 2, true);
  text(8, "WAVE");
  text(12, "fmt ");
  v.setUint32(16, 16, true); // fmt chunk size
  v.setUint16(20, 1, true); // PCM
  v.setUint16(22, 1, true); // mono
  v.setUint32(24, sampleRate, true);
  v.setUint32(28, sampleRate * 2, true); // byte rate
  v.setUint16(32, 2, true); // block align
  v.setUint16(34, 16, true); // bits per sample
  text(36, "data");
  v.setUint32(40, samples.length * 2, true);
  for (let i = 0; i < samples.length; i++) {
    const s = Math.max(-1, Math.min(1, samples[i]));
    v.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
  }
  return bytes;
}
