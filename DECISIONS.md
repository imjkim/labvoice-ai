# Decisions

Short records of why things are the way they are: context, decision, what was rejected, consequences.
This file is for future maintainers, and for anyone who asks "why didn't you just…?"

> **Josh:** these first entries were drafted while scaffolding. Rewrite each one in your own words
> as you work through the code, and add new entries as you go. If you can't defend an entry, change
> the code or change the entry.

---

### D1. Tools live behind MCP servers, not as in-process functions
- **Context:** A lab assistant needs the LIMS today and SOPs, paging, and instrument vendors later. Each is owned by a different team or vendor.
- **Decision:** Each system is an MCP server; the agent is an MCP *host* that discovers tools at startup.
- **Rejected:** Python functions registered with a framework. Simpler, but every new system means changing and redeploying the agent.
- **Consequences:** New systems plug in without touching the agent. Costs a subprocess per server and an extra serialization hop (~ms). Trust becomes a real question (see D3).

### D2. The model proposes; trusted code executes
- **Context:** Prompt injection and plain bad generations are inevitable with any model, and worse with small local ones.
- **Decision:** Non-read-only tools never execute inside a turn. They become pending actions in SQLite. Only `resolve_action`, triggered by a human, executes them: the stored payload, at most once (atomic `UPDATE … WHERE status='pending'`), within a TTL.
- **Rejected:** Asking the model to "confirm before acting." That's a prompt, not a control.
- **Consequences:** The worst a compromised turn can do is create a pending request. Writes take one extra human step, which is the point.

### D3. MCP annotations are hints, not policy
- **Context:** MCP tools self-declare `read_only_hint`, `destructive_hint`, etc. A malicious or sloppy server can lie.
- **Decision:** Honor `read_only_hint` only from servers marked `trusted` in host config. Missing annotations fail closed (approval required).
- **Consequences:** Adding a third-party server is safe by default; every one of its tools needs approval until reviewed.

### D4. Data minimization at the tool boundary
- **Context:** Patient identifiers in model context can leak through answers, logs, traces, or a hosted model provider.
- **Decision:** No LIMS tool returns patient name, DOB or MRN. The model works with accession numbers only. A test scans every read tool's output against the patients table.
- **Rejected:** Redacting PHI from model outputs after the fact. Detection is probabilistic; never sending it is deterministic.
- **Consequences:** The assistant can't answer "who's the patient on 312?" That's intended and is an eval case. Spoken transcripts can still contain PHI (people say names aloud), so trace redaction is still needed for STT text (open item).

### D5. Dry-run preflight before any human sees an approval
- **Context:** Schema validation can't know that specimen 312 only has blocks A1–A2. Without preflight, a supervisor would approve an order that then fails, which trains people to rubber-stamp.
- **Decision:** Trusted write tools accept `dry_run`: full server-side validation plus a preview (summary, warnings such as duplicate orders), no write. The host preflights; invalid requests go back to the model as errors. `dry_run` is hidden from the model's schema and stripped from its args.
- **Consequences:** Approvals are rare, valid, and informative. Execution *still* re-validates, because state can change between request and approval (tested: a QC failure lands in between).

### D6. Retry only transient failures, and never writes
- **Context:** Voice has a tight latency budget. Retrying "no such specimen" wastes a round trip; retrying a timed-out write may double-execute it.
- **Decision:** Read-only tools retry once with backoff, and only on transient-looking errors (timeouts, 502/503, rate limits). Writes are never auto-retried.
- **Consequences:** The transient classifier is a regex. Fine for one first-party server; a structured error code from servers would be better (open item).

### D7. Approval confirmations are deterministic text from the server's normalized preview
- **Context:** STT mishears numbers. If the assistant confirms using the model's words, a mis-heard "321" for "312" survives into approval.
- **Decision:** When a write is held, the spoken reply is built from the server's preview summary, which contains the canonical ID ("order HER2 on S26-00312 block A1"), not model-written prose.
- **Consequences:** The user hears exactly what's awaiting approval, with normalized IDs, and can catch errors early.

### D8. Accession normalization lives in the LIMS server
- **Context:** People say "312", "S 26 312", "twenty-six dash three twelve".
- **Decision:** The server accepts digit-level variants ("312" means the current year). Turning *words* into digits is the STT layer's job (Phase 2).
- **Consequences:** Every client of the server benefits, not just this agent. A bare "312" late in December may mean last year's case; acceptable for now, and an eval case should cover it.

### D9. A hand-written agent loop instead of a framework
- **Context:** The loop is ~150 lines: call model, validate, gate, execute, repeat.
- **Decision:** Own it. The approval gate and preflight are the product; they shouldn't be framework callbacks.
- **Rejected (for now):** LangGraph. Revisit if the workflow becomes a real graph (multi-agent, long-running human steps).
- **Consequences:** Full control and easy testing with a scripted LLM; we own the bugs.

### D10. One OpenAI-compatible client for every model
- **Decision:** Ollama, vLLM and hosted providers all go through `/chat/completions`.
- **Consequences:** Model swaps are a `.env` change, so the model comparison table is cheap to produce. Provider-specific features (e.g. prompt caching) are out of scope.

### D11. Synthetic data with planted scenarios
- **Decision:** A seeded generator for realistic volume, plus a few fixed scenarios (e.g. `S26-00312` blocked by a failed-QC IHC stainer) that demos and evals depend on.
- **Consequences:** Reproducible demos and evals; timestamps are relative to "now", so the lab always looks live.

### D12. Role-based approvals with four eyes
- **Context:** "A human approved it" isn't enough in a lab. Pathologists own stain orders; only supervisors should change instrument status; nobody should approve their own request.
- **Decision:** A per-tool policy (`voiceops/auth.py`) lists the roles that may approve each write tool. Unlisted tools (new ones, third-party servers) default to the most senior role. Approval also requires a different person from the requester. Requesters may withdraw their own requests. The allowed roles are stored on the action when it's created, and the spoken confirmation names them.
- **Authorization happens before the action is claimed,** so an unauthorized attempt can't consume or block it, and it's written to the trace as `approval_forbidden`.
- **Rejected:** Roles enforced only in the UI (bypassable), and role checks inside the MCP server (the server shouldn't need to know the agent's org chart, and third-party servers can't be trusted to check).

### D13. Identity is a demo header, deliberately
- **Decision:** The API identifies users by an `X-User` header against a small demo directory. That's enough to demonstrate and test the policy.
- **Consequences:** Not authentication. A real deployment takes identity from the lab's SSO (OIDC/SAML) and roles from its directory; the policy and enforcement points stay the same.

### D14. The approver is written into the system of record
- **Context:** A LIMS audit trail that says "the assistant did it" is useless to a lab director or an inspector.
- **Decision:** Write tools take an `actor` parameter that only the host sets (hidden from the model's schema and stripped from model arguments, like `dry_run`). On execution the host passes the approving human, and the LIMS records "Dr. Carmen Ruiz (pathologist) via LabVoice" on the event, the stain order, and the instrument log.
- **Consequences:** The model can't forge attribution (tested).

### D15. A non-thinking model for the voice path
- **Context:** On Ollama, `qwen3:4b` is the *thinking* build: it reasons at length before every answer, ~40 s per turn on an RTX 3060. Voice needs a reply in about 2 s.
- **Decision:** Default to `qwen3:4b-instruct` (same family and size, no thinking). First baseline: 81% pass, p50 1.8 s, p95 3.6 s, 0 unsafe writes.
- **Rejected:** Switching thinking off through Ollama's OpenAI-compatible endpoint. Reports show `think`/`reasoning_effort` there either don't reach the model or barely shorten the reasoning; changing the model is deterministic.
- **Consequences:** Any accuracy the thinking model has over instruct is out of reach within the latency budget. Closing that gap is what the Phase 3 fine-tune is for.

### D16. Design tools for the model you have
- **Context:** In the first baseline, 3 of 4 failures traced to tool design, not model ability: no status value meant "down", spoken numbers ("stainer one") didn't resolve, and the blocker behind a late specimen took a second call the model didn't make.
- **Decision:** Make the tools carry the reasoning a small model is bad at: explicit semantics (`available`, a `down` list), tolerant inputs (spoken numbers and filler words), joined context (each stain order carries its instrument's status), and a precomputed `headline` the model can read back.
- **Rejected:** Prompt patches per failure. They're brittle, cost tokens every turn, and don't transfer to the next model.
- **Consequences:** The remaining failures are closer to real reasoning gaps, which is the right target for fine-tuning.

### D17. Don't tune against the test you report
- **Context:** Fixing what the eval set exposes and then re-running the same cases inflates the score.
- **Decision:** Fixes target *classes* of failure, not individual cases. New cases were written alongside the fixes (multi-hop, spoken numbers, scope, ETA). Cases use `{YY}` instead of a hard-coded year prefix so they keep working next year. For the fine-tune, training data and the reported eval set will be strictly separate. Scorer fixes are allowed only for measurement errors (a correct answer marked wrong), and are disclosed in the commit and the results history: e.g. `instrument-qc` was failing "did not pass QC" because the check required the word "fail".

### D18. A grounding guard, in code
- **Context:** In the second eval run the model answered "Why isn't 312 ready yet?" **without calling any tool**: "S26-00312 is overdue and blocked by a labeling discrepancy." Fluent, specific, false. The invented details echoed words the system prompt had emphasized (OVERDUE, BLOCKED, FLAGGED).
- **Decision:** Before an answer is spoken, the host checks it against this turn's tool results: every accession or instrument ID it names must appear in a tool result, and an answer stating lab facts must have at least one. On failure the answer is discarded, the model is told what was wrong and gets one retry; a second failure is replaced with "I couldn't confirm that in the LIMS, so I won't guess." Facts from earlier turns don't count, because lab state changes.
- **Rejected:** More prompt rules ("never answer without a tool") — the prompt already said "Never guess lab data". An LLM-as-judge check — adds a second model call (~1 s) to every turn, itself unreliable at this size.
- **Consequences:** Heuristic: it can't catch a wrong *claim* about an ID that the tool did return (e.g. the wrong status), and a non-lab answer containing a lab word may cost one extra round trip. The eval report counts retries and blocks, so the guard's activity is measured, not assumed. Emphasized prompt words were also removed, since they leaked into the confabulation.

### D19. Tell small models where to act, at the parameter
- **Context:** Two failures came from instructions read too literally: "always say the full accession" made the model ask for the full number instead of acting, and despite "pass it as-is" it rewrote "287" as `S28-0287`.
- **Decision:** Separate *what to pass* from *what to say* in the prompt, and put input guidance in the tool schema's parameter descriptions, where the model writes the argument. The server also rejects future-year prefixes with a "did you mean" that names the right call.

### D20. A no-build web client served by the API
- **Context:** The bench client needs a microphone button, spoken answers, the agent's steps and an approval queue. It will run on a lab workstation, possibly offline.
- **Decision:** Plain ES modules and CSS in `web/`, served by FastAPI at `/`. No framework, bundler or `npm install`. Pure logic (timeline, latency breakdown) lives in its own module with Node built-in tests.
- **Rejected:** React + Vite. Better for a large UI, but it adds a toolchain and a build step to a one-screen client, and a second process to the demo.
- **Consequences:** One command runs everything. Model output reaches the page only through `textContent`. FastAPI's static serving relies on Python's `mimetypes`, which on Windows can map `.js` to `text/plain` (browsers then refuse to run the modules), so the types are pinned explicitly.

### D21. Voice I/O: hold-to-talk, browser speech, interruptible
- **Decision:** Hold-to-talk (button or space bar) rather than always-listening voice activity detection; Whisper on the server with a lab-vocabulary prompt; spoken answers through the browser's built-in voices. Pressing talk while the assistant speaks stops it (barge-in); pressing while it's still thinking abandons that answer.
- **Why:** Hold-to-talk makes "when is it listening?" unambiguous in a shared lab and keeps bench chatter out of the transcript. Browser speech is free and starts quickly; a local neural voice can replace it behind the same interface.
- **Consequences:** An abandoned turn still finishes on the server and stays in that session's history, though the client stops showing it. Writes are unaffected, since they only ever become pending approvals. Cancelling server-side work is an open item.

### D22. Hands-free listening (supersedes the hold-to-talk part of D21)
- **Context:** Holding a button defeats the point at a bench where both hands are busy.
- **Decision:** One click turns the microphone on (browsers require a click before audio capture, and it makes "the mic is live" an explicit choice in a shared room); after that, an energy-based voice activity detector finds each question. It compares every 20 ms frame against an adaptive noise floor (falls fast, rises slowly), needs 160 ms of sustained speech to start (ignores clicks and dropped racks), ends after 750 ms of silence, keeps 300 ms of pre-roll so the first word isn't clipped, and caps a question at 15 s. Audio is captured with an AudioWorklet and sent as 16 kHz WAV.
- **Echo:** With an always-open mic, the assistant would hear its own voice. Listening pauses while it speaks; Esc or a click stops it mid-answer. Browser echo cancellation doesn't reliably cover speech-synthesis output, so voice barge-in waits for a better signal (planned: a raised-hand gesture or a stop word).
- **Phantom transcripts:** Always-on listening sends coughs and clatter to Whisper, which tends to "hear" stock phrases ("Thank you.") or echo its vocabulary prompt. The server drops segments Whisper scores as non-speech, whole-transcript phantom phrases, and prompt echoes; an empty transcript quietly returns to listening.
- **Rejected:** A neural VAD (e.g. Silero) in the browser. More accurate in noise, but it needs a model runtime and download, and the energy detector is small, offline and unit-tested with synthetic audio. Revisit if false triggers in a real lab are a problem.

---

## Open questions
- Redacting PHI from STT transcripts before traces are written.
- Structured transient-error signaling from MCP servers instead of a regex.
- Voice framework (Pipecat / LiveKit Agents) vs hand-rolled streaming + barge-in. Decide with latency numbers, not taste.
- Cancel the server-side turn when the user interrupts (D21).
- Voice barge-in during speech: a raised-hand gesture or a stop word (D22).
- Streaming answers (speak the first sentence while the rest is generated).

---

## What broke (and what we learned)

### B1. Leaked SQLite connections, invisible on Linux, fatal on Windows
- **Symptom:** On Windows, 11 tests failed with `PermissionError: [WinError 32]` when deleting temp databases. All passed on Linux.
- **Cause:** `with sqlite3.connect(...) as c:` commits the transaction but does *not* close the connection. In CPython the connection sits in a reference cycle, so it's only closed when the cyclic GC runs. Linux lets you delete open files; Windows doesn't.
- **Fix:** One `_conn()` context manager per component that commits/rolls back *and* closes. `tests/test_store.py` disables the GC and checks for open handles (`/proc/self/fd` on Linux, delete-the-file on Windows); it fails on the old code.
- **Lesson:** Resource lifetimes shouldn't depend on the garbage collector, and CI should include the OS you deploy on.

### B2. The model confabulated a lab status without looking it up
- **Symptom:** "S26-00312 is overdue and blocked by a labeling discrepancy" — no tool call, every detail wrong. Pass rate still rose that run (81% → 85%), which is exactly why per-case answers have to be read, not just the score.
- **Fix:** The grounding guard (D18), plus removing emphasized words from the prompt that the confabulation echoed.
- **Lesson:** Instructions reduce failure rates; checks bound them. Anything that must never happen needs code, not prompt text.

### B3. The model reused a schema example as an argument, and stitched true facts into a false claim
- **Symptom:** Asked "Are any STAT cases stuck because of an instrument problem?", the model looked up `S26-00271`, then called `get_instrument("IHC stainer one")` with no reason from the user, and answered "S26-00271 is stuck because IHC stainer one is on qc_hold." Both facts were real lookups; the link between them was invented (271 has no stain on that instrument).
- **Cause:** `'IHC stainer one'` was the example in the instrument parameter's description. Every example in the prompt and tool schemas was a real specimen or instrument.
- **Fix:** Examples now use entities that don't exist (`'4821'`, `'microtome three'`, `'MT-07'`), the prompt says to look up only what the user or a tool result named, and a test fails if any quoted example or ID in model-visible text resolves to a real entity (it catches 17 leaks in the old text). The grounding guard (D18) passed this answer because every ID was real: it checks entities, not relationships, as documented. Relationship-level errors remain an open problem and a fine-tuning target.
- **Lesson:** Everything in the model's context is a suggestion, examples most of all.
