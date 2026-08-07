"""Speak through Windows' own voices instead of the API, when one fits.

The paid TTS was the single slowest stage of a press — 2075ms to the first
audio byte, more than transcription and the answer call put together. Windows
synthesises the whole clip locally in ~125ms, so this cuts time-to-sound by
roughly half.

It is not a quality compromise on the measure that matters here. Round-tripped
through transcription, the local voices scored 1.00 for both English and
Japanese, against 0.99 and 0.94 for the paid model. They sound more robotic —
but this is a cue heard once and never repeated, so intelligibility is the
whole job.

Two things are deliberately preserved:

- **Synthesis goes to a memory stream, not the speakers.** SAPI would happily
  play it itself, but then it would land on the system default device and
  bypass TTS_DEVICE — spoken answers would reach the meeting through your
  microphone. The PCM comes back to us and plays through the same pinned
  sounddevice path as before, keeping barge-in with it.
- **Only when a voice matches the language.** Vietnamese has no Windows voice
  on this machine, so Vietnamese answers fall through to the API rather than
  being read aloud by an English voice.
"""

from __future__ import annotations

import re
import threading

# SAFT22kHz16BitMono. 22050Hz is what the desktop voices produce natively;
# asking for anything else makes SAPI resample for no benefit.
_FORMAT = 22
SAMPLE_RATE = 22050

# Kana, CJK ideographs, and the Vietnamese-only letters. Vietnamese shares Latin
# with English, so it is identified by the diacritics English never uses.
#
# The bulk of Vietnamese tone marks are PRECOMPOSED into Latin Extended
# Additional (U+1EA0-U+1EF9) — "phần" and "trễ" are single code points, not a
# base letter plus a combining mark. Matching only the bases missed them, and a
# Vietnamese answer routed to an English voice is unintelligible, which is worse
# than the API round trip it was meant to avoid.
_JA = re.compile(r"[぀-ヿ一-鿿]")
_VI = re.compile(
    r"[Ạ-ỹ]"          # precomposed vowels with tone marks
    r"|[ăâđêôơưĂÂĐÊÔƠƯ]"        # bases without a tone mark
    r"|[̀-̣]"         # decomposed combining marks, just in case
)

_lock = threading.Lock()
_voices: dict[str, object] | None = None


def detect_language(text: str) -> str:
    """'ja', 'vi' or 'en'. Cheap and good enough to pick a voice."""
    if _JA.search(text):
        return "ja"
    if _VI.search(text):
        return "vi"
    return "en"


def _load() -> dict[str, object]:
    """Map language prefix -> SAPI voice token. Empty if SAPI is unavailable."""
    global _voices
    if _voices is not None:
        return _voices
    found: dict[str, object] = {}
    try:
        import win32com.client as com

        voice = com.Dispatch("SAPI.SpVoice")
        for token in voice.GetVoices():
            # Language is a hex LCID list; the description is more reliable
            # across Windows versions than parsing that.
            description = token.GetDescription().lower()
            if "japanese" in description:
                found.setdefault("ja", token)
            elif "english" in description:
                found.setdefault("en", token)
            elif "vietnamese" in description:
                found.setdefault("vi", token)
    except Exception:
        found = {}
    _voices = found
    return found


def available() -> dict[str, object]:
    return _load()


def languages() -> list[str]:
    return sorted(_load())


def speaks(text: str) -> bool:
    """Can this text be spoken locally?"""
    return detect_language(text) in _load()


def rate_for(speed: float) -> int:
    """Map TTS_SPEED (1.0 = normal) onto SAPI's -10..10 scale.

    SAPI rate is roughly exponential — each step is about 1.15x — so this
    matches the API's multiplier rather than guessing a constant.
    """
    import math

    if speed <= 0:
        return 0
    return max(-10, min(10, round(math.log(speed) / math.log(1.15))))


def synthesize(text: str, speed: float = 1.0) -> bytes | None:
    """PCM16 mono at SAMPLE_RATE, or None if no voice fits or SAPI failed.

    Returns bytes for the caller to play on the pinned device — this
    deliberately never touches the speakers itself.
    """
    voices = _load()
    voice_token = voices.get(detect_language(text))
    if voice_token is None or not text.strip():
        return None
    try:
        import win32com.client as com

        # COM objects are apartment-bound, so everything here is created on the
        # calling thread rather than cached across threads.
        with _lock:
            speaker = com.Dispatch("SAPI.SpVoice")
            stream = com.Dispatch("SAPI.SpMemoryStream")
            audio_format = com.Dispatch("SAPI.SpAudioFormat")
            audio_format.Type = _FORMAT
            stream.Format = audio_format
            speaker.Voice = voice_token
            speaker.Rate = rate_for(speed)
            speaker.AudioOutputStream = stream
            speaker.Speak(text)
            return bytes(bytearray(stream.GetData()))
    except Exception:
        return None
