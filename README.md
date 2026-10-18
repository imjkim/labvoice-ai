**A hands-free assistant for histology lab benches.** 

> **Thesis:** can a small, locally hosted model safely operate lab systems by voice?
> This repo answers with a working system, an eval suite, and (in progress) a fine-tune that
> measurably closes the gap between small local models and large hosted ones.

**Why voice:** histotechs work in gloves, at microtomes and stainers; typing means de-gloving.
**Why local:** lab data shouldn't leave the building. **Why approvals:** a wrong stain order or an
instrument wrongly taken offline costs a patient time.
> All data is synthetic (`servers/synth.py`). No real patient information is used anywhere.

```
 bench mic ─► Whisper (local) ─► agent loop ─► local LLM (any OpenAI-compatible endpoint)
                                     │  ▲
                       tool schemas  │  │ tool results
                                     ▼  │
                                 MCP host ──stdio──► LIMS MCP server   (SOP/RAG server next)
                                     │
               read-only (trusted)? ─yes─► execute: timeout, retry transient errors once
                                     │no
                                     ▼
                     dry-run preflight on the server: full validation + preview, no write
                          │invalid ─► error back to the model (no human bothered)
                          ▼valid
             pending action (SQLite) ─► spoken read-back with normalized IDs
                          ▼
            supervisor approves that exact payload ─► executes once, re-validated
```

## Results

27 scripted bench requests (`evals/cases.jsonl`), each against a fresh synthetic LIMS, on one RTX 3060.
Temperature 0, so a given configuration scores the same on every run.

| Model (via Ollama) | Cases | Pass | Tool choice | Approval gate | Unsafe writes | p50 turn | p95 turn |
|---|---|---|---|---|---|---|---|
| `qwen3:4b` (thinking build) | stopped early | | | | | ~40 s | |
| `qwen3:4b-instruct`, first baseline | 21 | 81.0% | 95.2% | 100% | 0 | 1.8 s | 3.6 s |
| `qwen3:4b-instruct`, current | 27 | 92.6% | 96.3% | 100% | 0 | 1.2 s | 2.5 s |

What moved the numbers, with the reasoning in [`DECISIONS.md`](DECISIONS.md):

- **Tool design for a small model (D16).** Three of the first four failures were tool problems, not model problems: no status meant "down", spoken numbers didn't resolve, and the blocker behind a late specimen took a second call the model skipped.
- **A grounding guard (D18).** The model once answered "S26-00312 is overdue and blocked by a labeling discrepancy" without looking anything up. Answers now have to be backed by this turn's tool results.
- **Guidance at the parameter, and no real entities in examples (D19, B3).** The model reused a schema example as an argument and stitched true facts into a false claim.
- **One measurement fix, disclosed (D17).** A correct "did not pass QC" was marked wrong for not saying "fail".

Still open: multi-step questions where the model over-filters (`stat-blocked-by-instrument`), the main
target for fine-tuning; and the guard's false-positive rate. It intervened in 2 of 27 cases in the latest run,
and in one (a refused prompt injection) the user heard the fallback instead of a clear refusal. Reports now
record exactly what the guard intercepted, so that rate can be measured rather than guessed.

## Run it

Needs Python 3.11+ and [Ollama](https://ollama.com).

```bash
python -m venv .venv && source .venv/Scripts/activate   # Windows Git Bash; use .venv/bin/activate elsewhere
pip install -r requirements.txt
ollama pull qwen3:4b-instruct
uvicorn voiceops.api:app
```

Open http://localhost:8000 and allow the microphone. Hold the slide (or the space bar), ask
"Why isn't 312 ready yet?", and release. Ask it to order a stain, then switch **You are** to
D. Reyes (supervisor) to approve it. Talking while it answers interrupts it.

```bash
python -m pytest -q                # agent, LIMS server, roles, grounding (Python)
cd web && node --test              # client logic (Node 18+, no installs)
python -m evals.run_evals          # the table above
```

## Repository map

```
servers/lims_server.py   LIMS as an MCP server (SDK 2.x): specimens, stains, instruments, QC
servers/synth.py         deterministic synthetic lab with planted scenarios
voiceops/mcp_host.py     starts MCP servers, discovers tools, trust policy, timeouts, retries
voiceops/agent/loop.py   tool-calling loop, schema validation, preflight, approvals, grounding guard
voiceops/auth.py         demo identities and who may approve what
voiceops/stt.py          faster-whisper with lab vocabulary and CPU fallback
voiceops/api.py          JSON API under /api, bench client at /
web/                     the bench client: plain ES modules, no build step
evals/                   cases and harness
DECISIONS.md             why it's built this way, and what broke
```
