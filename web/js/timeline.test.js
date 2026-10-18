// Run from web/: node --test    (Node 18+, no dependencies)
import assert from "node:assert/strict";
import { test } from "node:test";

import { bareTool, describeEvent, formatArgs, formatMs, latencySegments, toolLabel, totalMs } from "./timeline.js";

const TRACE = [
  { kind: "user", payload: { text: "why isn't 312 ready", user: { id: "mchen" } } },
  { kind: "llm", name: "qwen3:4b-instruct", latency_ms: 610.4, ok: true, payload: { tool_calls: ["lims__get_specimen"] } },
  { kind: "tool", name: "lims__get_specimen", latency_ms: 12.6, ok: true, payload: { args: { accession: "312" }, attempts: 1 } },
  { kind: "llm", name: "qwen3:4b-instruct", latency_ms: 540.1, ok: true, payload: { tool_calls: [] } },
  { kind: "answer", latency_ms: 1190.0, ok: true, payload: "S26-00312 is waiting on HER2." },
];

test("tool names are shortened and labelled for people", () => {
  assert.equal(bareTool("lims__get_specimen"), "get_specimen");
  assert.equal(toolLabel("lims__get_specimen"), "Looked up specimen");
  assert.equal(toolLabel("other__make_coffee"), "make coffee");
});

test("arguments drop empty values and read naturally", () => {
  assert.equal(formatArgs({ accession: "312", block: "A2", reason: null, dry_run: false }), "accession 312, block A2");
  assert.equal(formatArgs({ available: true }), "available yes");
  assert.equal(formatArgs(null), "");
});

test("events become timeline rows with the right tone", () => {
  assert.equal(describeEvent(TRACE[0]).detail, "why isn't 312 ready");
  assert.match(describeEvent(TRACE[1]).detail, /looked up specimen/);
  assert.equal(describeEvent(TRACE[3]).detail, "wrote the answer");
  const tool = describeEvent(TRACE[2]);
  assert.equal(tool.tone, "ok");
  assert.equal(tool.detail, "accession 312");
  assert.equal(describeEvent({ kind: "tool", name: "lims__get_specimen", ok: false, payload: { error: "timeout" } }).tone, "bad");
  assert.equal(describeEvent({ kind: "grounding_retry", payload: { problem: "x" } }).tone, "warn");
  assert.equal(describeEvent({ kind: "grounding_blocked", payload: { problem: "x" } }).title, "Refused to guess");
  const waiting = describeEvent({ kind: "approval_requested", payload: { approvers: ["lead_tech", "supervisor"] } });
  assert.equal(waiting.detail, "needs a lead tech or supervisor");
  assert.equal(describeEvent({ kind: "something_new", name: "x" }).title, "something new");
});

test("latency segments add up to the wait, with stages in order", () => {
  const segs = latencySegments({ sttMs: 420, trace: TRACE, ttsStartMs: 95 });
  assert.deepEqual(segs.map((s) => s.key), ["stt", "model", "tools", "other", "tts"]);
  const byKey = Object.fromEntries(segs.map((s) => [s.key, s.ms]));
  assert.equal(byKey.model, 1151);
  assert.equal(byKey.tools, 13);
  assert.equal(byKey.other, Math.round(1190 - 610.4 - 540.1 - 12.6));
  // Segments cover speech-to-text + the agent turn + voice start, give or take per-segment rounding.
  assert.ok(Math.abs(totalMs(segs) - (420 + 1190 + 95)) <= segs.length);
});

test("typed turns have no speech-to-text segment and never go negative", () => {
  const segs = latencySegments({ trace: [{ kind: "llm", latency_ms: 900 }, { kind: "answer", latency_ms: 800 }] });
  assert.deepEqual(segs.map((s) => s.key), ["model", "tools"]);
  assert.ok(segs.every((s) => s.ms >= 0));
});

test("durations format for humans", () => {
  assert.equal(formatMs(87.4), "87 ms");
  assert.equal(formatMs(1234), "1.2 s");
  assert.equal(formatMs(null), "");
});
