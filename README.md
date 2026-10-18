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
