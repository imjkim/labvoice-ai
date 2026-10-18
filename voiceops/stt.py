"""Speech-to-text with faster-whisper (local, free). Loaded lazily on first use."""
from __future__ import annotations

import time
from functools import lru_cache
from typing import Any

from voiceops.config import settings


@lru_cache(maxsize=1)
def _model():
    from faster_whisper import WhisperModel

    return WhisperModel(settings.whisper_model, device=settings.whisper_device,
                        compute_type=settings.whisper_compute_type)


def transcribe_file(path: str) -> dict[str, Any]:
    start = time.perf_counter()
    segments, info = _model().transcribe(path, beam_size=1, vad_filter=True, language="en")
    text = " ".join(s.text.strip() for s in segments).strip()  # segments is a generator: this runs decoding
    return {"text": text, "language": info.language,
            "latency_ms": round((time.perf_counter() - start) * 1000, 1)}
