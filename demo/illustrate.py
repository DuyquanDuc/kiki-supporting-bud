"""Draw a picture when the question asks for one.

Some questions cannot be answered in a sentence because the answer is a shape:
"can you draw a simple load balancer architecture", "sketch how the retry
works". Speech is useless for those, and so is a paragraph. F11 can now emit an
image prompt and this turns it into a PNG.

TWO THINGS MAKE THIS SAFE TO HAVE ON A LATENCY-OBSESSED PRESS.

It is asynchronous. Generation was measured at ~40s, which is forever in a
meeting, so the spoken cue and the written answer go out at their normal speed
and the picture arrives when it arrives. Nothing waits for it.

It is optional. No key, no network, a refusal, a timeout — every failure lands
in the history window as one line and the answer is unaffected. An illustration
is a bonus on top of an answer, never the answer itself.

Images land in demo/data/images, which is gitignored: they are drawn from what
your meeting was about.
"""

from __future__ import annotations

import base64
import re
import time
from pathlib import Path

from . import config

# ```image ... ``` below the --- marker. The model already knows the marker
# convention for "something to look at"; this is that, for something to look at
# that has to be drawn rather than read.
_BLOCK = re.compile(r"```image\s*\n(.*?)```", re.S | re.I)


def extract(answer: str) -> tuple[str, str]:
    """Pull the image prompt out of an answer. Returns (prompt, answer_without_it).

    The block is stripped from the printed answer because it is a stage
    direction, not something the user should have to read.
    """
    match = _BLOCK.search(answer or "")
    if not match:
        return "", answer
    prompt = " ".join(match.group(1).split())
    cleaned = _BLOCK.sub("", answer).rstrip()
    # A --- left with nothing under it is just a rule across the window.
    lines = [l for l in cleaned.splitlines()]
    while lines and lines[-1].strip() in ("", config.READ_MARKER):
        lines.pop()
    return prompt, "\n".join(lines)


def draw(client, prompt: str, on_event=None) -> Path | None:
    """Generate a PNG and return its path, or None. Never raises."""
    note = on_event or (lambda _m: None)
    if not config.IMAGE_ENABLED or client is None or not prompt:
        return None
    started = time.perf_counter()
    try:
        response = client.images.generate(
            model=config.IMAGE_MODEL,
            prompt=prompt,
            size=config.IMAGE_SIZE,
            timeout=config.IMAGE_TIMEOUT,
        )
        data = base64.b64decode(response.data[0].b64_json)
    except Exception as exc:
        note(f"illustration failed: {str(exc)[:70]}")
        return None
    try:
        config.IMAGE_DIR.mkdir(parents=True, exist_ok=True)
        path = config.IMAGE_DIR / f"{time.strftime('%Y-%m-%d_%H%M%S')}.png"
        path.write_bytes(data)
    except Exception as exc:
        note(f"illustration not saved: {str(exc)[:70]}")
        return None
    usage = getattr(response, "usage", None)
    if usage is not None:
        from .meter import METER
        METER.drew(usage)
    note(f"illustration ready in {time.perf_counter() - started:.0f}s: {path.name}")
    return path
