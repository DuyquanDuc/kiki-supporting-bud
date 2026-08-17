"""Spoken output, pinned to one device, interruptible.

Two things make this usable in a live meeting:

1. **Streaming PCM.** The API returns raw 24kHz mono int16, which goes straight
   into the sound card. Playback starts on the first chunk rather than after a
   whole file downloads and decodes.
2. **Barge-in.** Pressing the button again cuts off whatever is still talking.
   A stale answer playing over a new question is worse than no answer.
"""

from __future__ import annotations

import re
import threading
import time
from collections import deque

from . import config, local_tts
from .meter import METER

# Stereo Mix lags the sound card slightly, so the audio loop has to stay deaf
# for a moment after playback ends or it captures the tail of our own answer.
_SPEECH_TAIL_SECONDS = 1.0


def list_output_devices() -> list[tuple[int, str, int]]:
    """(index, name, channels) for everything that can play audio."""
    try:
        import sounddevice as sd
    except Exception:
        return []
    devices = []
    for index, device in enumerate(sd.query_devices()):
        if device.get("max_output_channels", 0) > 0:
            devices.append((index, device["name"], device["max_output_channels"]))
    return devices


def resolve_device(substring: str) -> int | None:
    """Match a device by name fragment, e.g. 'Earbuds'. None means system default."""
    if not substring:
        return None
    needle = substring.lower()
    for index, name, _channels in list_output_devices():
        if needle in name.lower():
            return index
    return None


def _plain(text: str) -> str:
    """Drop markup a voice would read out as punctuation.

    The detailed answer writes `Comparable` and **must** because the same string
    is also printed — but spoken, a backtick becomes an audible stumble. Strip
    them from the spoken copy only; the printed one keeps its formatting.
    """
    text = re.sub(r"```[a-zA-Z]*", " ", text)
    text = re.sub(r"[`*_#]+", "", text)
    return text


def shorten(text: str, limit: int = config.TTS_MAX_CHARS) -> str:
    """Speech gets talked over. Cut at a sentence boundary if there is one."""
    text = " ".join(_plain(text).split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for stop in (". ", "; ", ", "):
        index = cut.rfind(stop)
        if index > limit * 0.5:
            return cut[: index + 1].strip()
    space = cut.rfind(" ")
    if space > 0:
        cut = cut[:space]
    return cut.rstrip(" ,;:") + "..."


class Speaker:
    """Serialises speech requests; a new one cancels the one in flight."""

    def __init__(self, client, device: int | None = None, on_error=None):
        self._client = client
        self._device = device
        self._on_error = on_error
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._spoke_at = 0.0
        # What we have said lately, so the audio loop can recognise our own
        # voice coming back through loopback and drop it — instead of going
        # deaf while we talk and losing whatever was asked over the top.
        self._said: "deque[tuple[float, str]]" = deque(maxlen=12)

    @property
    def device(self) -> int | None:
        return self._device

    def is_speaking(self) -> bool:
        """True while audio is playing, plus a tail. The audio loop gates on this."""
        thread = self._thread
        if thread is not None and thread.is_alive():
            return True
        return time.monotonic() - self._spoke_at < _SPEECH_TAIL_SECONDS

    def recent(self, within: float = 60.0) -> list[str]:
        """What we said in the last `within` seconds, newest first."""
        now = time.monotonic()
        return [t for stamp, t in reversed(self._said) if now - stamp <= within]

    def stop(self) -> None:
        self._cancel.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=1.5)

    def say(self, text: str) -> None:
        if not text or self._client is None:
            return
        self.stop()
        self._cancel = threading.Event()
        cancel = self._cancel
        self._spoke_at = time.monotonic()
        clipped = shorten(text)
        self._said.append((time.monotonic(), clipped))
        # Only bill what actually goes to the API. Locally synthesised speech
        # is free, and counting it would inflate the session cost report.
        if not (config.LOCAL_TTS and local_tts.speaks(clipped)):
            METER.spoke(clipped)
        self._thread = threading.Thread(
            target=self._run, args=(shorten(text), cancel), daemon=True
        )
        self._thread.start()

    def _run(self, text: str, cancel: threading.Event) -> None:
        try:
            import sounddevice as sd
        except Exception as exc:  # pragma: no cover - environment dependent
            self._report(f"audio device unavailable: {exc}")
            return

        # Try Windows' own voice first: ~125ms for the whole clip against
        # ~2075ms to the API's FIRST byte. Falls through to the API when no
        # local voice matches the language.
        local = None
        if config.LOCAL_TTS:
            local = local_tts.synthesize(text, config.TTS_SPEED)

        stream = None
        try:
            stream = sd.RawOutputStream(
                samplerate=local_tts.SAMPLE_RATE if local else config.TTS_SAMPLE_RATE,
                channels=1,
                dtype="int16",
                device=self._device,
                blocksize=0,
            )
            stream.start()
            if local:
                # Written in blocks rather than one call so a second press can
                # still cut it off part-way, exactly as with the streamed API.
                for start in range(0, len(local), 4096):
                    if cancel.is_set():
                        break
                    stream.write(local[start:start + 4096])
                return
            with self._lock, self._client.audio.speech.with_streaming_response.create(
                model=config.TTS_MODEL,
                voice=config.TTS_VOICE,
                input=text,
                response_format=config.TTS_FORMAT,
                speed=config.TTS_SPEED,
            ) as response:
                for chunk in response.iter_bytes(chunk_size=4096):
                    if cancel.is_set():
                        break
                    if chunk:
                        stream.write(chunk)
        except Exception as exc:
            self._report(f"tts failed: {exc}")
        finally:
            self._spoke_at = time.monotonic()
            if stream is not None:
                try:
                    if cancel.is_set():
                        stream.abort()
                    stream.stop()
                    stream.close()
                except Exception:
                    pass

    def _report(self, message: str) -> None:
        if self._on_error:
            self._on_error(message)
        else:
            print(f"[speech] {message}")
