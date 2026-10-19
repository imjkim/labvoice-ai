// Run from web/: node --test
import assert from "node:assert/strict";
import { test } from "node:test";

import { EnergyVad, encodeWav, frameDb, resample } from "./vad.js";

const RATE = 16000;
const FRAME = 320; // 20 ms at 16 kHz

/** Deterministic pseudo-noise so tests don't flake. */
function rng(seed) {
  let s = seed;
  return () => ((s = (s * 1664525 + 1013904223) % 4294967296) / 4294967296) * 2 - 1;
}

/** ms of audio at a given amplitude: noise-like (room) or tonal (voice-ish). */
function audio(ms, amp, { tone = false, seed = 1 } = {}) {
  const n = Math.round((RATE * ms) / 1000);
  const r = rng(seed);
  const out = new Float32Array(n);
  for (let i = 0; i < n; i++) out[i] = tone ? amp * Math.sin((2 * Math.PI * 180 * i) / RATE) + 0.002 * r() : amp * r();
  return out;
}

function run(vad, ...chunks) {
  const all = new Float32Array(chunks.reduce((n, c) => n + c.length, 0));
  let o = 0;
  for (const c of chunks) { all.set(c, o); o += c.length; }
  const events = [];
  for (let i = 0; i + FRAME <= all.length; i += FRAME) {
    const e = vad.push(all.subarray(i, i + FRAME));
    if (e) events.push({ ...e, atMs: (i / RATE) * 1000 });
  }
  return events;
}

const room = (ms, seed) => audio(ms, 0.003, { seed }); // about -55 dBFS of noise
const voice = (ms) => audio(ms, 0.2, { tone: true }); // about -17 dBFS

test("frameDb measures loudness", () => {
  assert.ok(frameDb(new Float32Array(FRAME)) < -150);
  const loud = frameDb(voice(20));
  assert.ok(loud > -20 && loud < -14, String(loud));
});

test("a spoken question produces one start and one end, with the first word kept", () => {
  const vad = new EnergyVad(RATE);
  const events = run(vad, room(1000, 1), voice(1500), room(1200, 2));
  assert.deepEqual(events.map((e) => e.type), ["start", "end"]);
  const [start, end] = events;
  assert.ok(start.atMs >= 1000 && start.atMs < 1300, `start at ${start.atMs}`);
  // ends after ~750 ms of trailing silence
  assert.ok(end.atMs >= 2500 + 700 && end.atMs < 2500 + 900, `end at ${end.atMs}`);
  // the clip includes pre-roll before the voice began, so nothing was clipped
  const clipMs = (end.samples.length / RATE) * 1000;
  assert.ok(clipMs >= 1500 + 250, `clip ${clipMs} ms`);
});

test("short clicks and bumps are ignored", () => {
  const vad = new EnergyVad(RATE);
  const events = run(vad, room(1000, 3), voice(60), room(800, 4), voice(100), room(800, 5));
  assert.deepEqual(events, []);
});

test("a steady noisy lab doesn't trigger, but speech above it does", () => {
  const vad = new EnergyVad(RATE);
  const loudRoom = (ms, seed) => audio(ms, 0.03, { seed }); // centrifuge, fans: about -35 dBFS
  assert.deepEqual(run(vad, loudRoom(3000, 6)), [], "steady noise alone never starts");
  const events = run(vad, voice(1200), loudRoom(1500, 7));
  assert.deepEqual(events.map((e) => e.type), ["start", "end"]);
});

test("a monologue is cut at the maximum length", () => {
  const vad = new EnergyVad(RATE, { maxUtteranceMs: 2000 });
  const events = run(vad, room(500, 8), voice(5000));
  assert.equal(events[0].type, "start");
  assert.equal(events[1].type, "end");
  assert.ok(events[1].durationMs <= 2000 + 20);
});

test("cancel abandons an utterance in progress", () => {
  const vad = new EnergyVad(RATE);
  run(vad, room(800, 9), voice(400));
  assert.deepEqual(vad.cancel(), { type: "discard" });
  assert.equal(vad.cancel(), null, "nothing left to cancel");
  assert.deepEqual(run(vad, room(1000, 10)), []);
});

test("resample shortens 48 kHz audio to a third, preserving the waveform", () => {
  const src = new Float32Array(4800).map((_, i) => Math.sin((2 * Math.PI * 100 * i) / 48000));
  const out = resample(src, 48000, 16000);
  assert.equal(out.length, 1600);
  assert.ok(Math.abs(out[400] - Math.sin((2 * Math.PI * 100 * 400) / 16000)) < 1e-3);
  assert.equal(resample(src, 16000, 16000), src);
});

test("encodeWav writes a valid mono 16-bit header and clamps samples", () => {
  const bytes = encodeWav(new Float32Array([0, 0.5, -1, 2]), 16000);
  const v = new DataView(bytes);
  const tag = (o) => String.fromCharCode(...new Uint8Array(bytes, o, 4));
  assert.equal(tag(0), "RIFF");
  assert.equal(tag(8), "WAVE");
  assert.equal(v.getUint16(22, true), 1, "mono");
  assert.equal(v.getUint32(24, true), 16000);
  assert.equal(v.getUint16(34, true), 16, "16-bit");
  assert.equal(v.getUint32(40, true), 8, "4 samples x 2 bytes");
  assert.equal(v.getInt16(44 + 6, true), 0x7fff, "2.0 clamps to full scale");
  assert.equal(v.getInt16(44 + 4, true), -0x8000);
});
