"""Background screen loop: capture, diff, describe, keep in memory.

This is the loop that makes the button feel instant. By the time you press,
the screen has already been captured, sent to a vision model, and turned into
structured data. The trigger path only has to read a variable.

The frame diff is what keeps it cheap: a static slide costs nothing, and a
meeting only changes screen 20-40 times an hour.
"""

from __future__ import annotations

import base64
import io
import json
import threading
import time
from dataclasses import dataclass, field

import numpy as np
from PIL import Image

from . import config

_PROMPT = """Here is a screenshot of what the user is looking at. Help them with
whatever is being asked of them right now. It could be anything — a slide, a
spreadsheet, a chart, a code diff, an error message, a document, a dashboard, a
video. Do not assume it is a sales table.

Work out the question this screen most likely raises for the person watching it,
then answer that question. Not a description of the screen — the answer they
would need if someone turned to them and said "thoughts?"

If the screen poses no question — no error, no decision, nothing being asked —
then just summarise what is on it: what it shows and the point it is making.

Return JSON with exactly these keys:
  "headline": the single most prominent line of text, or "" if none
  "summary":  one plain sentence naming what is on screen
  "answer":   the actual help, 1-3 short sentences. The point the screen is
              making, what the number or error or chart means, what to say or
              do about it. Plain spoken English, no bullet markup.
  "numbers":  array of notable figures, most prominent first, each
              {"value": "as shown, e.g. 2.4M or $412,000", "label": "what it refers to"}

Rules: ground everything in what is visibly on screen and never invent a figure,
name, or fact. If the screen is unreadable or empty, say so in "answer" rather
than guessing. Return an empty array when there are no meaningful numbers. Keep
"summary" under 20 words and "answer" under 45 — it gets spoken aloud while
someone is still talking, so length is the failure mode."""

_THUMB = (160, 90)


def grab_region(region: dict) -> Image.Image | None:
    """One screenshot of `region`, right now.

    Creates its own capture handle per call: mss objects are not safe to share
    across threads, and the button runs on its own. The grab costs tens of
    milliseconds against a vision call measured in seconds, so taking it at press
    time is free — and gives the screen as it was when you pressed, rather than
    whatever a background poll last happened to catch.
    """
    if not region:
        return None
    try:
        import mss
    except Exception:
        return None
    capture = getattr(mss, "MSS", None) or mss.mss
    try:
        with capture() as sct:
            shot = sct.grab(region)
        frame = np.asarray(shot)[:, :, :3][:, :, ::-1]  # BGRA -> RGB
        return Image.fromarray(frame)
    except Exception:
        return None


def encode_jpeg(image: Image.Image) -> str:
    """Base64 JPEG, the form both the screen loop and the button send."""
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="JPEG", quality=config.JPEG_QUALITY)
    return base64.b64encode(buffer.getvalue()).decode()


@dataclass
class ScreenState:
    headline: str = ""
    summary: str = ""
    answer: str = ""
    numbers: list[dict] = field(default_factory=list)
    captured_at: float = 0.0
    analyzed_ms: int = 0
    error: str = ""

    @property
    def age_seconds(self) -> float:
        return time.monotonic() - self.captured_at if self.captured_at else 0.0


_OFFLINE_STATE = ScreenState(
    headline="Q3 Pipeline Review",
    summary="A slide showing the APAC renewal figure for the quarter.",
    answer=(
        "The 2.4M is the APAC renewal booked for Q3 — it is the single largest "
        "line on the slide, so any question about the quarter lands on it."
    ),
    numbers=[{"value": "2.4M", "label": "Q3 APAC renewal"}],
    captured_at=time.monotonic(),
    analyzed_ms=0,
)


class ScreenLoop(threading.Thread):
    def __init__(self, region: dict, client, offline: bool = False, on_event=None):
        super().__init__(daemon=True, name="screen-loop")
        self._region = region
        self._client = client
        self._offline = offline
        self._on_event = on_event or (lambda _m: None)
        # NOT `_stop`: threading.Thread uses that name internally and join()
        # calls it, so shadowing it breaks shutdown.
        self._halt = threading.Event()
        self._lock = threading.Lock()
        self._state: ScreenState | None = _OFFLINE_STATE if offline else None
        self._last_thumb: np.ndarray | None = None
        self.analyses = 0
        self.captures = 0

    # --- read side (called from the trigger path) --------------------------
    def latest(self) -> ScreenState | None:
        with self._lock:
            return self._state

    def latest_frame(self) -> Image.Image | None:
        """A screenshot taken now, not a cached one.

        Deliberately not the background loop's last frame: grabbing costs
        milliseconds, and this way the picture is the screen at the moment of the
        press even when the loop is switched off entirely.
        """
        if self._offline:
            return None
        return grab_region(self._region)

    def stop(self) -> None:
        self._halt.set()

    # --- loop --------------------------------------------------------------
    def run(self) -> None:
        if self._offline:
            self._on_event("screen loop offline — using a canned slide")
            return
        if not config.SCREEN_LOOP_ENABLED:
            self._on_event(
                "screen loop off — F10 grabs a fresh screenshot at press time, "
                "so nothing is analysed in the background"
            )
            return
        try:
            import mss
        except Exception as exc:  # pragma: no cover
            self._set_error(f"screen capture unavailable: {exc}")
            return

        # mss 10 renamed the constructor to MSS and deprecated the old name.
        capture = getattr(mss, "MSS", None) or mss.mss
        with capture() as sct:
            while not self._halt.is_set():
                started = time.monotonic()
                try:
                    self._tick(sct)
                except Exception as exc:
                    self._set_error(str(exc))
                elapsed = time.monotonic() - started
                self._halt.wait(max(0.2, config.SCREEN_POLL_SECONDS - elapsed))

    def _tick(self, sct) -> None:
        shot = sct.grab(self._region)
        self.captures += 1
        frame = np.asarray(shot)[:, :, :3][:, :, ::-1]  # BGRA -> RGB
        image = Image.fromarray(frame)
        thumb = np.asarray(image.resize(_THUMB).convert("L"), dtype=np.int16)

        if self._last_thumb is not None:
            delta = float(np.abs(thumb - self._last_thumb).mean())
            if delta < config.DIFF_THRESHOLD:
                return  # nothing changed, no call, no cost
        self._last_thumb = thumb
        self._analyze(image)

    def _analyze(self, image: Image.Image) -> None:
        encoded = encode_jpeg(image)

        started = time.monotonic()
        try:
            response = self._client.chat.completions.create(
                model=config.VISION_MODEL,
                response_format={"type": "json_object"},
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": _PROMPT},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/jpeg;base64,{encoded}",
                                    "detail": config.VISION_DETAIL,
                                },
                            },
                        ],
                    }
                ],
            )
            payload = json.loads(response.choices[0].message.content or "{}")
        except Exception as exc:
            self._set_error(str(exc))
            return

        elapsed_ms = int((time.monotonic() - started) * 1000)
        numbers = [n for n in payload.get("numbers") or [] if isinstance(n, dict)]
        state = ScreenState(
            headline=str(payload.get("headline") or ""),
            summary=str(payload.get("summary") or ""),
            answer=str(payload.get("answer") or ""),
            numbers=numbers,
            captured_at=time.monotonic(),
            analyzed_ms=elapsed_ms,
        )
        with self._lock:
            self._state = state
        self.analyses += 1
        self._on_event(
            f"screen updated in {elapsed_ms}ms — {state.headline or state.summary!r}"
        )

    def _set_error(self, message: str) -> None:
        with self._lock:
            if self._state is None:
                self._state = ScreenState(error=message, captured_at=time.monotonic())
            else:
                self._state.error = message
        self._on_event(f"screen loop error: {message}")
