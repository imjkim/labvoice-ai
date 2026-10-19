"""Speech-to-text with faster-whisper (local, free). Loaded lazily on first use.

Two practical details matter more than model size for this domain:
- Vocabulary biasing. Whisper has never heard "IHC" or "Ki-67" in a lab context. An initial prompt
  written in the lab's own words makes it far more likely to spell them the way the tools expect.
- Device fallback. CUDA setups on Windows often miss a cuBLAS/cuDNN DLL, which can fail at load
  time or only at first inference. Rather than failing the demo, fall back to CPU (int8) and
  report which device is actually in use.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

from voiceops.config import settings

log = logging.getLogger("voiceops.stt")

# Written like something a histotech would say, with generic examples only: no real accession
# numbers or instruments from the demo LIMS (same reason as the tool schemas, DECISIONS B3).
DOMAIN_PROMPT = (
    "Histology lab. Accession S26-04821, block A2. Stains: H&E, PAS, GMS, AFB, trichrome, Ki-67, "
    "ER, PR, HER2, p53, CK7, CK20. IHC stainer, H&E stainer, tissue processor, microtome, "
    "coverslipper, QC hold, STAT, grossing, embedding, sectioning, sign-out."
)

_lock = threading.Lock()
_state: dict[str, Any] = {"model": None, "device": None}


def _load(device: str, compute_type: str) -> None:
    from faster_whisper import WhisperModel

    _state["model"] = WhisperModel(settings.whisper_model, device=device, compute_type=compute_type)
    _state["device"] = device


def _ensure_loaded() -> None:
    with _lock:
        if _state["model"] is not None:
            return
        try:
            _load(settings.whisper_device, settings.whisper_compute_type)
        except Exception as exc:  # typically missing CUDA libraries
            if settings.whisper_device == "cpu":
                raise
            log.warning("Whisper on %s failed to load (%s); using CPU int8", settings.whisper_device, exc)
            _load("cpu", "int8")


def device() -> str | None:
    """The device Whisper is running on, or None if it hasn't been loaded yet."""
    return _state["device"]


# Whisper's best-known failure on non-speech: it "hears" stock phrases from its training data.
PHANTOM_PHRASES = {"you", "thank you", "thanks", "thank you for watching", "thanks for watching",
                   "bye", "okay", "so", "um", "uh", "hmm"}


def _norm(text: str) -> str:
    return " ".join("".join(c.lower() if c.isalnum() else " " for c in text).split())


def clean_transcript(segments: Any) -> str:
    """Keep what was actually said. With hands-free listening, coughs and clatter reach Whisper,
    which then tends to invent text. Drop:
    - segments Whisper itself scores as probably-not-speech and low-confidence;
    - a whole transcript that is a stock phantom phrase ("Thank you.");
    - a transcript that just echoes the vocabulary prompt back (prompting makes this possible)."""
    kept = [s.text.strip() for s in segments
            if not (getattr(s, "no_speech_prob", 0.0) > 0.6 and getattr(s, "avg_logprob", 0.0) < -1.0)]
    text = " ".join(t for t in kept if t).strip()
    normalized = _norm(text)
    if not normalized or normalized in PHANTOM_PHRASES:
        return ""
    if len(normalized) >= 12 and normalized in _norm(DOMAIN_PROMPT):
        return ""
    return text


def _run(path: str) -> tuple[str, str]:
    segments, info = _state["model"].transcribe(path, beam_size=1, vad_filter=True, language="en",
                                                initial_prompt=DOMAIN_PROMPT)
    # `segments` is a generator: iterating it is what actually runs decoding.
    return clean_transcript(segments), info.language


def transcribe_file(path: str) -> dict[str, Any]:
    start = time.perf_counter()
    _ensure_loaded()
    try:
        text, language = _run(path)
    except RuntimeError as exc:
        # CUDA can load fine and still fail on first use (e.g. "cublas64_12.dll is not found").
        if _state["device"] == "cpu":
            raise
        log.warning("Whisper failed on %s at inference (%s); switching to CPU int8", _state["device"], exc)
        with _lock:
            _load("cpu", "int8")
        text, language = _run(path)
    return {"text": text, "language": language, "device": _state["device"],
            "latency_ms": round((time.perf_counter() - start) * 1000, 1)}
