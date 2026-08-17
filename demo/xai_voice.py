"""Speak with an xAI voice — a cloned one, or one of their built-ins.

OPTIONAL. Nothing here runs unless XAI_VOICE_ID is set; blank leaves speech
exactly as it was.

CLONING IS NOT DONE HERE, and cannot be. POST /v1/custom-voices answers
"Custom voices are not enabled for this team" on a normal key — it wants an
Enterprise team — and even there it would not take a recording: verification is
a live passphrase read back plus a speaker-embedding match, specifically so
nobody can be cloned from audio that already exists. Make the voice in the xAI
console (free, cap 30), then set its id.

WHAT IT COSTS. Measured on short lines: 950-2000ms for the whole clip, against
220ms for the Windows voice. The request is unary rather than streamed, so that
is also the time to the FIRST sound. Choosing a voice id is choosing to pay it.
"""

from __future__ import annotations

import io
import time

import numpy as np

from . import config


def available() -> bool:
    return bool(config.XAI_API_KEY and config.XAI_VOICE_ID)


def synthesize(text: str, on_error=None) -> tuple[bytes, int] | None:
    """(PCM16 bytes, samplerate), or None so the caller falls back.

    Never raises: a voice that fails must cost the user their preferred voice,
    never the answer itself.
    """
    note = on_error or (lambda _m: None)
    if not available() or not text:
        return None
    try:
        import requests
        import soundfile as sf

        started = time.perf_counter()
        response = requests.post(
            config.XAI_TTS_URL,
            headers={"Authorization": f"Bearer {config.XAI_API_KEY}"},
            json={"text": text, "voice_id": config.XAI_VOICE_ID, "language": "en"},
            timeout=config.XAI_TIMEOUT,
        )
        if response.status_code != 200:
            note(f"xai voice failed ({response.status_code}): {response.text[:80]}")
            return None
        # Always MP3 regardless of any `format` asked for, so it is decoded
        # here rather than handed to the sound card raw.
        audio, rate = sf.read(io.BytesIO(response.content), dtype="float32",
                              always_2d=False)
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2").tobytes()
        note(f"xai voice {config.XAI_VOICE_ID}: {len(audio) / rate:.1f}s in "
             f"{(time.perf_counter() - started) * 1000:.0f}ms")
        return pcm, int(rate)
    except Exception as exc:
        note(f"xai voice failed: {str(exc)[:80]}")
        return None
