// Pure functions that turn an agent trace into what the "What happened" panel shows.
// No DOM access here, so these are unit-tested with `node --test web/`.

const TOOL_LABELS = {
  get_specimen: "Looked up specimen",
  find_specimens: "Searched specimens",
  get_instrument: "Checked instrument",
  list_instruments: "Listed instruments",
  order_stain: "Stain order",
  flag_specimen: "Specimen flag",
  set_instrument_status: "Instrument status change",
};

/** "lims__get_specimen" -> "get_specimen" */
export function bareTool(name) {
  return String(name || "").split("__").pop();
}

export function toolLabel(name) {
  const bare = bareTool(name);
  return TOOL_LABELS[bare] || bare.replaceAll("_", " ");
}

/** {accession: "312", block: "A2"} -> "accession 312, block A2" (nulls and empties dropped) */
export function formatArgs(args) {
  if (!args || typeof args !== "object") return String(args ?? "");
  return Object.entries(args)
    .filter(([, v]) => v !== null && v !== undefined && v !== "" && v !== false)
    .map(([k, v]) => `${k.replaceAll("_", " ")} ${v === true ? "yes" : v}`)
    .join(", ");
}

/**
 * One trace event -> one timeline row.
 * tone: "neutral" | "ok" | "pending" | "warn" | "bad"
 */
export function describeEvent(e) {
  const p = e.payload || {};
  switch (e.kind) {
    case "user":
      return { title: "Heard", detail: typeof p === "string" ? p : p.text || "", tone: "neutral", ms: null };
    case "llm": {
      if (e.ok === false) return { title: "Model unavailable", detail: p.error || "", tone: "bad", ms: e.latency_ms };
      const calls = (p.tool_calls || []).map(toolLabel);
      return {
        title: "Model",
        detail: calls.length ? `decided: ${calls.join(", ").toLowerCase()}` : "wrote the answer",
        tone: "neutral",
        ms: e.latency_ms,
      };
    }
    case "tool": {
      const ok = e.ok !== false;
      const retried = p.attempts > 1 ? ` (after ${p.attempts} attempts)` : "";
      return {
        title: toolLabel(e.name),
        detail: ok ? formatArgs(p.args) + retried : `failed: ${p.error || "error"}`,
        tone: ok ? "ok" : "bad",
        ms: e.latency_ms,
        code: true,
      };
    }
    case "preflight":
      return e.ok
        ? { title: "Checked with the LIMS first", detail: (p.preview && p.preview.summary) || formatArgs(p.args), tone: "ok", ms: e.latency_ms }
        : { title: "Rejected before approval", detail: p.error || "", tone: "bad", ms: e.latency_ms };
    case "approval_requested": {
      const who = (p.approvers || []).map((r) => r.replaceAll("_", " ")).join(" or ");
      return { title: "Waiting for approval", detail: who ? `needs a ${who}` : "", tone: "pending", ms: null };
    }
    case "grounding_retry":
      return { title: "Caught an unsupported answer", detail: p.problem || "", tone: "warn", ms: null };
    case "grounding_blocked":
      return { title: "Refused to guess", detail: p.problem || "", tone: "bad", ms: null };
    case "answer": // the answer itself is already in the conversation
      return { title: "Answered", detail: "", tone: "neutral", ms: e.latency_ms };
    default:
      return { title: e.kind.replaceAll("_", " "), detail: e.name || "", tone: "neutral", ms: e.latency_ms ?? null };
  }
}

/**
 * Where the time went for one turn. Segments sum to the total the user waited
 * (before speech playback starts).
 */
export function latencySegments({ sttMs = null, trace = [], ttsStartMs = null } = {}) {
  const sum = (kinds) =>
    trace.filter((e) => kinds.includes(e.kind)).reduce((acc, e) => acc + (e.latency_ms || 0), 0);
  const answer = trace.find((e) => e.kind === "answer");
  const turnMs = answer ? answer.latency_ms || 0 : 0;
  const modelMs = sum(["llm"]);
  const toolMs = sum(["tool", "preflight"]);
  const otherMs = Math.max(0, turnMs - modelMs - toolMs);
  const segs = [];
  if (sttMs !== null) segs.push({ key: "stt", label: "Speech to text", ms: sttMs });
  segs.push({ key: "model", label: "Model", ms: modelMs });
  segs.push({ key: "tools", label: "LIMS", ms: toolMs });
  if (otherMs >= 1) segs.push({ key: "other", label: "Other", ms: otherMs });
  if (ttsStartMs !== null) segs.push({ key: "tts", label: "Voice start", ms: ttsStartMs });
  return segs.map((s) => ({ ...s, ms: Math.round(s.ms) }));
}

export function totalMs(segments) {
  return segments.reduce((acc, s) => acc + s.ms, 0);
}

/** 1234 -> "1.2 s", 87 -> "87 ms" */
export function formatMs(ms) {
  if (ms === null || ms === undefined) return "";
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)} s` : `${Math.round(ms)} ms`;
}
