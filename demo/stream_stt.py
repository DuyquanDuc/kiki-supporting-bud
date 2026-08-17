"""Live transcription over a websocket, as an alternative to the batch path.

OPTIONAL AND OFF BY DEFAULT. `TRANSCRIBE_MODE=stream` turns it on; anything
else leaves audio_loop's existing behaviour untouched. Nothing here runs unless
it is asked for, so rolling back is one environment variable rather than a
revert.

WHY IT EXISTS. Measured against the batch path on the same fixtures:

    silence          batch invents text (the vocabulary prompt handed back, or
                     "ChatGPT, a large language model trained by OpenAI...");
                     streaming returned NOTHING for mic noise, room tone and
                     digital silence alike
    accuracy         0.91 en / 0.87 ja, against 0.77 / 0.77
    transcript ready ~700ms after speech ends, against 1-3s after the press —
                     partials arrive while the sentence is still being spoken

The first line is the reason. Every silence gate, burstiness threshold and
echo filter in audio_loop exists to stop the batch transcriber inventing text,
and none of them is needed here.

WHAT IT COSTS. $0.017/min against $0.003/min, and streaming pays for wall-clock
time where batch only pays for audio that passed a gate — roughly $1/hour for
one source against a few cents. Sending is therefore paused when the room goes
quiet, which is safe in a way the batch gate was not: a moment misjudged here
costs a moment, not a whole 20-second clip.

SHAPE OF THE PROTOCOL, learned by probing rather than from a stable spec:
  - wss://api.openai.com/v1/realtime?intent=transcription
  - NO OpenAI-Beta header. The server rejects it with beta_api_shape_disabled.
  - gpt-live-transcribe refuses turn_detection: "Turn detection is not
    supported for this transcription model". So turns are committed by hand,
    and the commit is what produces a final transcript.
"""

from __future__ import annotations

import asyncio
import base64
import json
import queue
import threading
import time

import numpy as np

from . import config

URL = "wss://api.openai.com/v1/realtime?intent=transcription"
API_RATE = 24_000               # the endpoint wants 24kHz PCM16 mono


def _resample(audio: np.ndarray, src: int) -> np.ndarray:
    if src == API_RATE:
        return audio
    count = int(round(len(audio) * API_RATE / src))
    if count <= 0:
        return np.zeros(0, dtype=np.float32)
    return np.interp(
        np.linspace(0, len(audio), count, endpoint=False),
        np.arange(len(audio)), audio,
    ).astype(np.float32)


def _pcm16(audio: np.ndarray) -> bytes:
    return (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2").tobytes()


class StreamTranscriber:
    """One websocket per audio source, fed from the capture thread.

    Owns a background thread running its own asyncio loop. `feed()` is called
    from the capture thread and only ever touches a queue, so a slow or broken
    network can never stall audio capture — the queue is bounded and drops the
    oldest audio rather than growing without limit.
    """

    def __init__(self, api_key: str, label: str, on_text, on_event=None):
        self._key = api_key
        self._label = label
        self._on_text = on_text
        self._on_event = on_event or (lambda _m: None)
        # Bounded: if the socket stalls, lose the oldest audio rather than
        # memory. ~30s at 24kHz in 100ms blocks.
        self._q: queue.Queue = queue.Queue(maxsize=300)
        self._halt = threading.Event()
        self._thread: threading.Thread | None = None
        self.connected = False
        self.reconnects = 0
        self.sent_seconds = 0.0

    # --- called from the capture thread ------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def feed(self, audio: np.ndarray, samplerate: int) -> None:
        """Hand over a block. Never blocks, never raises."""
        if self._halt.is_set():
            return
        try:
            self._q.put_nowait(_resample(audio.astype(np.float32), samplerate))
        except queue.Full:
            try:
                self._q.get_nowait()          # drop the oldest, keep the newest
                self._q.put_nowait(_resample(audio.astype(np.float32), samplerate))
            except Exception:
                pass

    def stop(self) -> None:
        self._halt.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=3)

    # --- the socket, on its own thread -------------------------------------

    def _run(self) -> None:
        try:
            asyncio.run(self._loop())
        except Exception as exc:
            self._on_event(f"stream [{self._label}] stopped: {str(exc)[:80]}")

    async def _loop(self) -> None:
        backoff = 1.0
        while not self._halt.is_set():
            try:
                await self._session()
                backoff = 1.0
            except Exception as exc:
                if self._halt.is_set():
                    return
                self.connected = False
                self.reconnects += 1
                self._on_event(
                    f"stream [{self._label}] disconnected ({str(exc)[:60]}); "
                    f"reconnecting in {backoff:.0f}s (reconnect #{self.reconnects})"
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 15.0)

    async def _session(self) -> None:
        import websockets

        async with websockets.connect(
            URL,
            additional_headers={"Authorization": f"Bearer {self._key}"},
            max_size=None,
            ping_interval=20,
        ) as ws:
            await ws.send(json.dumps({
                "type": "session.update",
                "session": {
                    "type": "transcription",
                    "audio": {"input": {
                        "format": {"type": "audio/pcm", "rate": API_RATE},
                        "transcription": {"model": config.STREAM_MODEL},
                        # Rejected by this model; turns are committed by hand.
                        "turn_detection": None,
                    }},
                },
            }))
            self.connected = True
            self._on_event(f"stream [{self._label}] connected ({config.STREAM_MODEL})")

            await asyncio.gather(self._send(ws), self._receive(ws))

    async def _send(self, ws) -> None:
        """Drain the queue into the socket, committing on natural pauses."""
        pending = 0.0            # seconds of speech sent since the last commit
        quiet_for = 0.0          # seconds of consecutive quiet
        last_audio = time.monotonic()
        while not self._halt.is_set():
            try:
                # NOT get(timeout=...): a blocking queue read inside a
                # coroutine stalls the whole event loop, and the receive task
                # never runs — the socket accepts audio and no transcript ever
                # comes back.
                block = self._q.get_nowait()
            except queue.Empty:
                # Audio has stopped arriving altogether — a stalled capture, a
                # paused source, or simply the end. Commit anything pending
                # rather than stranding it: waiting for a quiet BLOCK only
                # works while blocks are still coming.
                if pending > 0.4 and time.monotonic() - last_audio >= config.STREAM_COMMIT_SILENCE:
                    await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
                    pending = 0.0
                await asyncio.sleep(0.02)      # yield to the receive task
                continue
            last_audio = time.monotonic()

            seconds = len(block) / API_RATE
            level = float(np.sqrt(np.mean(np.square(block)))) if len(block) else 0.0
            speaking = level >= config.STREAM_SEND_FLOOR

            if speaking:
                quiet_for = 0.0
            else:
                quiet_for += seconds

            # Cost control: stop paying for an empty room. Safe here in a way
            # the batch gate was not — misjudging a moment costs a moment,
            # because the stream is continuous rather than clip-at-a-time.
            if not speaking and quiet_for > config.STREAM_IDLE_PAUSE and pending <= 0:
                continue

            await ws.send(json.dumps({
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(_pcm16(block)).decode(),
            }))
            pending += seconds
            self.sent_seconds += seconds

            # A final transcript only arrives when the turn is committed, so
            # commit on a pause in speech — which puts the boundary in a gap
            # rather than mid-word, the failure that made short batch chunks
            # score 0.844 against 0.992.
            long_enough = pending >= config.STREAM_MAX_TURN_SECONDS
            paused = quiet_for >= config.STREAM_COMMIT_SILENCE and pending > 0.4
            if paused or long_enough:
                await ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
                pending = 0.0

    async def _receive(self, ws) -> None:
        while not self._halt.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=30)
            except asyncio.TimeoutError:
                continue
            message = json.loads(raw)
            kind = message.get("type", "")
            if kind.endswith("transcription.completed"):
                text = " ".join(str(message.get("transcript", "")).split())
                if text:
                    self._on_text(text)
            elif kind == "error":
                detail = str(message.get("error", ""))[:100]
                # Commit-with-nothing-buffered is routine when a pause lands on
                # a block boundary, and is not worth reporting.
                if "buffer_empty" not in detail:
                    self._on_event(f"stream [{self._label}] {detail}")
