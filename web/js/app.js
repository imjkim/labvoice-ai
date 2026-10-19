// VoiceOps bench client: hold-to-talk, spoken answers with barge-in, the agent's steps,
// and the approval queue. Model output is untrusted, so it only ever reaches the page via
// textContent, never innerHTML.

import { ApiError, createApi } from "./api.js";
import { describeEvent, formatArgs, formatMs, latencySegments, toolLabel, totalMs } from "./timeline.js";
import { Recorder, Speaker } from "./voice.js";

const $ = (id) => document.getElementById(id);
const store = {
  get(key) { try { return localStorage.getItem(key); } catch { return null; } },
  set(key, value) { try { localStorage.setItem(key, value); } catch { /* storage unavailable */ } },
};

const PHASES = {
  idle: ["Hold to talk", "or hold the space bar"],
  listening: ["Listening", "release to send"],
  thinking: ["Checking the LIMS", "press to interrupt"],
  speaking: ["Speaking", "press to interrupt"],
};
const MIN_RECORDING_MS = 350;
const QUEUE_POLL_MS = 4000;

const state = {
  phase: "idle",
  userId: store.get("voiceops.user") || "mchen",
  sessionId: null,
  abort: null,
  users: [],
};
const api = createApi(() => state.userId);
const recorder = new Recorder();
const speaker = new Speaker();

// ---------------------------------------------------------------- helpers ---
function el(tag, { className, text, attrs } = {}, children = []) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = text;
  for (const [k, v] of Object.entries(attrs || {})) node.setAttribute(k, v);
  for (const child of children) if (child) node.append(child);
  return node;
}

function setPhase(phase) {
  state.phase = phase;
  document.body.dataset.phase = phase;
  const [label, hint] = PHASES[phase];
  $("talk-state").textContent = label;
  $("talk-hint").textContent = hint;
  $("talk").setAttribute("aria-pressed", String(phase === "listening"));
}

let statusTimer = null;
function setStatus(message, { error = false, sticky = false } = {}) {
  const node = $("status");
  node.textContent = message;
  node.classList.toggle("is-error", error);
  clearTimeout(statusTimer);
  if (message && !sticky) statusTimer = setTimeout(() => (node.textContent = ""), error ? 8000 : 4000);
}

function micErrorMessage(err) {
  if (!window.isSecureContext) {
    return "The browser only allows the microphone on localhost or https. Open http://localhost:8000.";
  }
  if (err && err.name === "NotAllowedError") {
    return "Microphone access is blocked. Allow it in this site's browser settings, then try again.";
  }
  if (err && err.name === "NotFoundError") return "No microphone found. Plug one in, or type your question.";
  return `Couldn't start the microphone (${(err && err.message) || "unknown error"}).`;
}

// ----------------------------------------------------------- conversation ---
function addMessage(role, text, meta = "") {
  $("empty").hidden = true;
  const item = el("li", { className: `msg msg-${role}` }, [
    el("p", { className: "msg-who", text: { you: "You", assistant: "VoiceOps", note: "Approvals" }[role] }),
    el("p", { className: "msg-text", text }),
    el("p", { className: "msg-meta", text: meta }),
  ]);
  $("convo").append(item);
  item.scrollIntoView({ block: "end", behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
  return item;
}

function updateMessage(item, { text, meta, className }) {
  if (text !== undefined) item.querySelector(".msg-text").textContent = text;
  if (meta !== undefined) item.querySelector(".msg-meta").textContent = meta;
  if (className) item.classList.add(className);
}

function resetConversation() {
  state.abort?.abort();
  speaker.cancel();
  state.sessionId = null;
  $("convo").replaceChildren();
  $("trace").replaceChildren();
  $("latency").replaceChildren();
  $("trace-empty").hidden = false;
  $("empty").hidden = false;
  setPhase("idle");
}

// ------------------------------------------------------------------ turns ---
async function runTurn(call, { spokenInput, typedText }) {
  setPhase("thinking");
  const controller = new AbortController();
  state.abort = controller;
  const yours = addMessage("you", spokenInput ? "…" : typedText, spokenInput ? "transcribing" : "typed");
  if (spokenInput) yours.classList.add("is-pending");

  try {
    const res = await call(controller.signal);
    let turn = res;
    let sttMs = null;
    if (spokenInput) {
      sttMs = res.timings?.stt_ms ?? null;
      if (!res.transcript?.text) {
        yours.remove();
        setPhase("idle");
        setStatus("Didn't catch that. Try again a little closer to the microphone.");
        return;
      }
      yours.classList.remove("is-pending");
      updateMessage(yours, { text: res.transcript.text, meta: `heard in ${formatMs(sttMs)}` });
      turn = res.turn;
    }
    state.sessionId = turn.session_id;
    const waiting = (turn.pending_actions || []).length > 0;
    const answer = addMessage("assistant", turn.answer, waiting ? "waiting for approval" : "");
    if (waiting) {
      answer.classList.add("is-waiting");
      answer.dataset.actions = turn.pending_actions.map((a) => a.action_id).join(" ");
      refreshQueue();
    }
    renderTrace(turn.trace, sttMs);

    const respondedAt = performance.now();
    setPhase("speaking");
    speaker.speak(turn.answer, {
      onstart: () => renderTrace(turn.trace, sttMs, performance.now() - respondedAt),
      onend: () => { if (state.phase === "speaking") setPhase("idle"); },
    });
  } catch (err) {
    if (err.name === "AbortError") {
      // The user pressed talk again while we were waiting: they've moved on.
      updateMessage(yours, { meta: "interrupted" });
      yours.classList.add("is-interrupted");
      return;
    }
    yours.classList.remove("is-pending");
    updateMessage(yours, { meta: "not sent" });
    setStatus(err instanceof ApiError ? err.message : `Something went wrong: ${err.message}`, { error: true });
    setPhase("idle");
  } finally {
    if (state.abort === controller) state.abort = null;
  }
}

async function press() {
  if (state.phase === "listening") return;
  if (!recorder.supported) {
    setStatus("This browser can't record audio. Type your question instead.", { error: true });
    return;
  }
  // Barge-in: talking over the assistant stops it, and a new question cancels a pending one.
  if (state.phase === "speaking") speaker.cancel();
  if (state.phase === "thinking") state.abort?.abort();
  try {
    await recorder.start();
    setPhase("listening");
  } catch (err) {
    setStatus(micErrorMessage(err), { error: true });
    setPhase("idle");
  }
}

async function release() {
  if (state.phase !== "listening") return;
  const { blob, filename, durationMs } = await recorder.stop();
  if (!blob || durationMs < MIN_RECORDING_MS) {
    setPhase("idle");
    setStatus("Hold the button for as long as you're speaking.");
    return;
  }
  await runTurn((signal) => api.voice(blob, filename, state.sessionId, signal), { spokenInput: true });
}

async function ask(text) {
  const question = text.trim();
  if (!question) return;
  if (state.phase === "speaking") speaker.cancel();
  if (state.phase === "thinking") state.abort?.abort();
  await runTurn((signal) => api.ask(question, state.sessionId, signal), { typedText: question });
}

// ------------------------------------------------------------------ trace ---
function renderTrace(trace, sttMs, ttsStartMs = null) {
  $("trace-empty").hidden = true;
  const segments = latencySegments({ sttMs, trace, ttsStartMs });
  const total = totalMs(segments) || 1;

  const bar = el("div", { className: "latency-bar", attrs: { role: "img", "aria-label": "Time per stage" } },
    segments.map((s) => {
      const seg = el("span", { className: `seg seg-${s.key}`, attrs: { title: `${s.label}: ${formatMs(s.ms)}` } });
      seg.style.flexGrow = String(Math.max(s.ms, 1));
      return seg;
    }));
  const legend = el("ul", { className: "latency-legend" },
    segments.map((s) => el("li", {}, [
      el("span", { className: `dot seg-${s.key}`, attrs: { "aria-hidden": "true" } }),
      el("span", { text: s.label }),
      el("span", { className: "num", text: formatMs(s.ms) }),
    ])));
  $("latency").replaceChildren(
    el("p", { className: "latency-total" }, [
      el("span", { text: ttsStartMs === null ? "Waited so far" : "Time until speech" }),
      el("span", { className: "num", text: formatMs(total) }),
    ]),
    bar,
    legend,
  );

  $("trace").replaceChildren(...trace.map((event) => {
    const d = describeEvent(event);
    return el("li", { className: `step tone-${d.tone}` }, [
      el("p", { className: "step-title" }, [
        el("span", { text: d.title }),
        d.ms !== null && d.ms !== undefined ? el("span", { className: "num", text: formatMs(d.ms) }) : null,
      ]),
      d.detail ? el("p", { className: d.code ? "step-detail is-code" : "step-detail", text: d.detail }) : null,
    ]);
  }));
}

// ------------------------------------------------------------------ queue ---
async function refreshQueue() {
  let actions;
  try {
    actions = await api.actions();
  } catch {
    return; // the status line already reports connection problems from turns
  }
  $("queue-count").textContent = actions.length ? String(actions.length) : "";
  $("queue-empty").hidden = actions.length > 0;
  $("queue").replaceChildren(...actions.map(renderAction));
}

function renderAction(a) {
  const preview = a.preview || {};
  const summary = preview.summary
    ? preview.summary.charAt(0).toUpperCase() + preview.summary.slice(1)
    : `${toolLabel(a.tool)}: ${formatArgs(a.args)}`;
  const roles = (a.approvers || []).map((r) => r.replaceAll("_", " ")).join(" or ");
  const mine = a.requested_by === state.userId;
  const error = el("p", { className: "card-error", attrs: { role: "alert" } });

  const decide = async (decision, button) => {
    button.disabled = true;
    error.textContent = "";
    try {
      const out = await api.decide(a.action_id, decision);
      showDecision(a, summary, decision, out);
      refreshQueue();
    } catch (err) {
      error.textContent = err.message;
      button.disabled = false;
    }
  };

  const buttons = [];
  if (a.can_approve) {
    const approve = el("button", { className: "btn btn-primary", text: "Approve" });
    approve.addEventListener("click", () => decide("approve", approve));
    const deny = el("button", { className: "btn", text: "Deny" });
    deny.addEventListener("click", () => decide("deny", deny));
    buttons.push(approve, deny);
  } else if (mine) {
    const withdraw = el("button", { className: "btn", text: "Withdraw request" });
    withdraw.addEventListener("click", () => decide("deny", withdraw));
    buttons.push(withdraw);
  }

  return el("li", { className: "card" }, [
    el("p", { className: "card-summary", text: summary }),
    preview.warning ? el("p", { className: "card-warning", text: preview.warning }) : null,
    el("p", { className: "card-meta", text: `Requested by ${mine ? "you" : a.requested_by_name || "an unknown user"}` }),
    el("p", {
      className: "card-meta",
      text: a.can_approve ? "You can approve this." : mine ? `Needs a ${roles}, and not you.` : `Needs a ${roles}.`,
    }),
    buttons.length ? el("div", { className: "card-actions" }, buttons) : null,
    error,
  ]);
}

/** Put the outcome of an approval where people are looking: in the conversation. */
function showDecision(action, summary, decision, out) {
  const who = state.users.find((u) => u.id === state.userId)?.name || "Someone";
  const withdrawn = decision === "deny" && action.requested_by === state.userId;
  let line;
  let meta;
  if (decision === "approve" && out.status === "executed") {
    line = `${who} approved: ${summary}. Done.`;
    meta = `approved by ${who}, done`;
  } else if (decision === "approve") {
    line = `${who} approved: ${summary}, but the LIMS refused it: ${out.result?.error || "unknown error"}`;
    meta = "approved, but not done";
  } else {
    line = withdrawn ? `${who} withdrew the request: ${summary}.` : `${who} denied: ${summary}. Nothing was changed.`;
    meta = withdrawn ? "withdrawn" : `denied by ${who}`;
  }
  addMessage("note", line);
  for (const msg of document.querySelectorAll(".msg.is-waiting")) {
    if ((msg.dataset.actions || "").split(" ").includes(action.action_id)) {
      msg.classList.remove("is-waiting");
      msg.classList.add("is-resolved");
      updateMessage(msg, { meta });
    }
  }
}

// ------------------------------------------------------------------ setup ---
async function loadUsers() {
  try {
    state.users = await api.users();
  } catch (err) {
    setStatus(err.message, { error: true, sticky: true });
    return;
  }
  if (!state.users.some((u) => u.id === state.userId)) state.userId = state.users[0]?.id;
  const select = $("user-select");
  select.replaceChildren(...state.users.map((u) =>
    el("option", { text: `${u.name}, ${u.role.replaceAll("_", " ")}`, attrs: { value: u.id } })));
  select.value = state.userId;
}

async function loadHealth() {
  try {
    const h = await api.health();
    const stt = h.stt_device ? `speech to text on ${h.stt_device.toUpperCase()}` : "speech to text loads on first use";
    $("system").textContent = `${h.model}, ${h.tools} LIMS tools, ${stt}`;
  } catch {
    $("system").textContent = "Server not reachable";
  }
}

function isTyping(target) {
  return target instanceof HTMLElement && (target.matches("input, textarea, select") || target.isContentEditable);
}

function wire() {
  const talk = $("talk");
  talk.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    talk.setPointerCapture(e.pointerId);
    press();
  });
  talk.addEventListener("pointerup", release);
  talk.addEventListener("pointercancel", release);
  talk.addEventListener("contextmenu", (e) => e.preventDefault()); // long-press on touch screens

  // Hold the space bar anywhere except while typing.
  document.addEventListener("keydown", (e) => {
    if (e.code !== "Space" || isTyping(e.target)) return;
    e.preventDefault();
    if (!e.repeat) press();
  });
  document.addEventListener("keyup", (e) => {
    if (e.code !== "Space" || isTyping(e.target)) return;
    e.preventDefault();
    release();
  });

  $("type-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const input = $("type-input");
    const text = input.value;
    input.value = "";
    ask(text);
  });
  for (const button of document.querySelectorAll("[data-ask]")) {
    button.addEventListener("click", () => ask(button.dataset.ask));
  }
  $("new-convo").addEventListener("click", resetConversation);
  $("user-select").addEventListener("change", (e) => {
    state.userId = e.target.value;
    store.set("voiceops.user", state.userId);
    const user = state.users.find((u) => u.id === state.userId);
    if (user) setStatus(`Now acting as ${user.name} (${user.role.replaceAll("_", " ")}).`);
    refreshQueue();
  });
  document.addEventListener("visibilitychange", () => document.visibilityState === "visible" && refreshQueue());
  setInterval(() => document.visibilityState === "visible" && refreshQueue(), QUEUE_POLL_MS);
}

setPhase("idle");
wire();
await loadUsers();
loadHealth();
refreshQueue();
