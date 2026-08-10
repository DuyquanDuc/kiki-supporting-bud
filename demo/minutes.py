"""Meeting minutes, written when you quit.

The transcript the answer buttons read is deliberately short — a rolling
fifteen minutes, so prompts stay small. Minutes need the opposite, so the audio
loop keeps an unpruned archive alongside it and this writes from that.

Two files land per meeting, and the split matters. The transcript is free and
exact: it is what was actually said, and it is written even when the model call
fails, so a session never ends with nothing. The minutes are a model's reading
of that transcript — useful, and wrong often enough that the raw text has to
survive next to it.

Both go to demo/data/minutes, which is gitignored: this is what your colleagues
said, and some of it will be about clients.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from . import config

_MINUTES_PROMPT = """You are writing the minutes of a meeting that has just ended, from its
transcript. The transcript is tagged by speaker: "Them:" is other participants,
"You:" is the person these minutes are for.

Write markdown, in the language the meeting was mostly held in. Use only the
sections that have real content — an empty heading is worse than a missing one:

## Summary
Two or three sentences. What the meeting was for and where it got to.

## Decisions
What was actually settled, and by or for whom. Not proposals — decisions.

## Actions
"- [ ] owner — what, by when" for anything someone took on. The person these
minutes are for is "you". Only include a date if one was said.

## Open questions
What was raised and left unresolved, and who is expected to resolve it.

## Numbers and dates
Figures, deadlines and identifiers exactly as stated, with what each refers to.

RULES. Never invent a decision, an owner, a number or a deadline — this is a
record someone may act on. Speech-to-text is imperfect: where a name or figure
is garbled, write it as heard and mark it "(unclear)" rather than guessing at a
correction. If the transcript is too thin for a section, leave that section out.
If it is too thin for minutes at all, say so in one line.

"Them" and "You" are internal labels, never names. Refer to the person these
minutes are for the way that language naturally would — "you" in English,
あなた or 自分 in Japanese, "bạn" or a dropped subject in Vietnamese — and never
by writing the label itself. Everyone else keeps the names used in the
transcript."""


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "meeting"


def transcript_text(archive: list[tuple[float, str, str]]) -> str:
    """The raw transcript with wall-clock timestamps, for the saved file."""
    lines = []
    for stamp, label, text in archive:
        lines.append(f"[{time.strftime('%H:%M:%S', time.localtime(stamp))}] {label}: {text}")
    return "\n".join(lines)


def write(client, archive: list[tuple[float, str, str]], on_event=None) -> dict:
    """Save the transcript, then the minutes. Returns the paths written.

    The transcript is saved FIRST and unconditionally. Generating minutes is a
    network call at the moment the user is quitting, and losing an hour of
    meeting record because that call failed would be the worst bug in this file.
    """
    note = on_event or (lambda _m: None)
    if not archive:
        return {}

    started = time.localtime(archive[0][0])
    stamp = time.strftime("%Y-%m-%d_%H%M", started)
    directory = config.MINUTES_DIR
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        note(f"minutes: cannot create {directory}: {exc}")
        return {}

    written: dict = {}
    raw = transcript_text(archive)
    transcript_path = directory / f"{stamp}-transcript.txt"
    try:
        transcript_path.write_text(raw, encoding="utf-8")
        written["transcript"] = transcript_path
    except Exception as exc:
        note(f"minutes: could not save the transcript: {exc}")

    if client is None or not raw.strip():
        return written

    # A long meeting can outgrow a sensible prompt. The END is what minutes are
    # usually about — decisions land late — so keep the tail.
    body = raw[-config.MINUTES_MAX_CHARS:]
    if len(raw) > config.MINUTES_MAX_CHARS:
        note(f"minutes: transcript is {len(raw):,} chars, summarising the last "
             f"{config.MINUTES_MAX_CHARS:,}")

    try:
        response = client.chat.completions.create(
            model=config.ANSWER_MODEL,
            messages=[
                {"role": "system", "content": _MINUTES_PROMPT},
                {"role": "user", "content": f"Transcript:\n{body}"},
            ],
            timeout=90.0,
        )
        text = (response.choices[0].message.content or "").strip()
    except Exception as exc:
        note(f"minutes: the model call failed ({str(exc)[:60]}) — transcript kept")
        return written

    if not text:
        return written

    when = time.strftime("%Y-%m-%d %H:%M", started)
    ended = time.strftime("%H:%M", time.localtime(archive[-1][0]))
    header = f"# Meeting — {when} to {ended}\n\n"
    minutes_path = directory / f"{stamp}-minutes.md"
    try:
        minutes_path.write_text(header + text + "\n", encoding="utf-8")
        written["minutes"] = minutes_path
    except Exception as exc:
        note(f"minutes: could not save: {exc}")
    return written
