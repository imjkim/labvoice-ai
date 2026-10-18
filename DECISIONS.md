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
- **Context:** A LIMS audit trail that says "VoiceOps did it" is useless to a lab director or an inspector.
- **Decision:** Write tools take an `actor` parameter that only the host sets (hidden from the model's schema and stripped from model arguments, like `dry_run`). On execution the host passes the approving human, and the LIMS records "Dr. Carmen Ruiz (pathologist) via VoiceOps" on the event, the stain order, and the instrument log.
- **Consequences:** The model can't forge attribution (tested).

---

## Open questions
- Redacting PHI from STT transcripts before traces are written.
- Structured transient-error signaling from MCP servers instead of a regex.
- Voice framework (Pipecat / LiveKit Agents) vs hand-rolled streaming + barge-in. Decide with latency numbers, not taste.

---

## What broke (and what we learned)

### B1. Leaked SQLite connections, invisible on Linux, fatal on Windows
- **Symptom:** On Windows, 11 tests failed with `PermissionError: [WinError 32]` when deleting temp databases. All passed on Linux.
- **Cause:** `with sqlite3.connect(...) as c:` commits the transaction but does *not* close the connection. In CPython the connection sits in a reference cycle, so it's only closed when the cyclic GC runs. Linux lets you delete open files; Windows doesn't.
- **Fix:** One `_conn()` context manager per component that commits/rolls back *and* closes. `tests/test_store.py` disables the GC and checks for open handles (`/proc/self/fd` on Linux, delete-the-file on Windows); it fails on the old code.
- **Lesson:** Resource lifetimes shouldn't depend on the garbage collector, and CI should include the OS you deploy on.
