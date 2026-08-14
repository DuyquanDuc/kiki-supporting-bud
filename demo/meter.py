"""What this session actually consumed, and roughly what it cost.

Two different kinds of number live here, and the difference matters.

**Usage is measured.** Audio seconds sent, tokens in and out as reported by the
API itself, characters spoken. Those are facts.

**Cost is an estimate.** It multiplies usage by rates in config, and those rates
are a guess that goes stale — model prices change and this project pins a model
whose pricing is not something the code can look up. Treat the dollar figure as
an order of magnitude and set your own rates in .env once you have checked them
against your billing page.

Printed on quit, so a session ends by telling you what it spent.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

from . import config


@dataclass
class Meter:
    started: float = field(default_factory=time.monotonic)
    lock: threading.Lock = field(default_factory=threading.Lock)

    transcribe_calls: int = 0
    transcribe_seconds: float = 0.0
    # Groq's free tier costs nothing, so it is counted separately rather than
    # billed at the OpenAI rate and inflating the session total.
    groq_calls: int = 0
    groq_seconds: float = 0.0

    answer_calls: int = 0
    answer_in: int = 0
    answer_out: int = 0

    vision_calls: int = 0
    vision_in: int = 0
    vision_out: int = 0

    tts_calls: int = 0
    tts_chars: int = 0

    # Illustrations are billed per image, not per token, so they cannot ride
    # along in the answer counters.
    image_calls: int = 0

    # --- recording ---------------------------------------------------------

    def transcribed(self, seconds: float, provider: str = "openai") -> None:
        with self.lock:
            if provider == "groq":
                self.groq_calls += 1
                self.groq_seconds += max(0.0, seconds)
            else:
                self.transcribe_calls += 1
                self.transcribe_seconds += max(0.0, seconds)

    def answered(self, usage, vision: bool = False) -> None:
        """Record token usage from a chat response. Tolerates a missing usage."""
        prompt = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion = int(getattr(usage, "completion_tokens", 0) or 0)
        with self.lock:
            if vision:
                self.vision_calls += 1
                self.vision_in += prompt
                self.vision_out += completion
            else:
                self.answer_calls += 1
                self.answer_in += prompt
                self.answer_out += completion

    def drew(self, usage=None) -> None:
        with self.lock:
            self.image_calls += 1

    def spoke(self, text: str) -> None:
        with self.lock:
            self.tts_calls += 1
            self.tts_chars += len(text or "")

    # --- reporting ---------------------------------------------------------

    def costs(self) -> dict[str, float]:
        with self.lock:
            return {
                "transcribe": (self.transcribe_seconds / 60.0) * config.RATE_TRANSCRIBE_PER_MIN,
                "answer": (self.answer_in / 1e6) * config.RATE_ANSWER_IN_PER_M
                          + (self.answer_out / 1e6) * config.RATE_ANSWER_OUT_PER_M,
                "vision": (self.vision_in / 1e6) * config.RATE_ANSWER_IN_PER_M
                          + (self.vision_out / 1e6) * config.RATE_ANSWER_OUT_PER_M,
                "speech": (self.tts_chars / 1e6) * config.RATE_TTS_PER_M_CHARS,
                "images": self.image_calls * config.RATE_IMAGE_EACH,
            }

    def report(self) -> list[str]:
        """Lines for the console, usage first because usage is the measured part."""
        with self.lock:
            minutes = (time.monotonic() - self.started) / 60.0
            t_calls, t_secs = self.transcribe_calls, self.transcribe_seconds
            g_calls, g_secs = self.groq_calls, self.groq_seconds
            a_calls, a_in, a_out = self.answer_calls, self.answer_in, self.answer_out
            v_calls, v_in, v_out = self.vision_calls, self.vision_in, self.vision_out
            s_calls, s_chars = self.tts_calls, self.tts_chars
            i_calls = self.image_calls
        cost = self.costs()
        total = sum(cost.values())

        lines = [f"session: {minutes:.0f} min"]
        if t_calls:
            # Average clip length is the number to watch when tuning the sweep:
            # it is what actually goes to the model in one pass, and short clips
            # are where transcription accuracy falls apart.
            average = t_secs / t_calls if t_calls else 0
            lines.append(
                f"  transcribe  {t_calls:>4} calls  {t_secs / 60:6.1f} min audio"
                f"  avg clip {average:4.0f}s   ${cost['transcribe']:.3f}"
            )
        if g_calls:
            lines.append(
                f"  transcribe  {g_calls:>4} calls  {g_secs / 60:6.1f} min audio"
                f"  avg clip {g_secs / g_calls:4.0f}s   free (groq)"
            )
        if a_calls:
            lines.append(
                f"  answers     {a_calls:>4} calls  {a_in:>7,} in / {a_out:,} out"
                f"   ${cost['answer']:.3f}"
            )
        if v_calls:
            lines.append(
                f"  screen      {v_calls:>4} calls  {v_in:>7,} in / {v_out:,} out"
                f"   ${cost['vision']:.3f}"
            )
        if s_calls:
            lines.append(
                f"  speech      {s_calls:>4} calls  {s_chars:>7,} chars"
                f"          ${cost['speech']:.3f}"
            )
        if i_calls:
            lines.append(
                f"  images      {i_calls:>4} drawn"
                f"                              ${cost['images']:.3f}"
            )
        if not (t_calls or g_calls or a_calls or v_calls or s_calls or i_calls):
            lines.append("  nothing was sent this session")
            return lines

        lines.append(f"  ESTIMATED TOTAL  ${total:.2f}")
        if minutes >= 1:
            lines.append(f"  ~${total / minutes * 60:.2f} per hour at this rate")
        lines.append(
            "  cost is usage x the rates in config; usage is measured, rates are"
        )
        lines.append("  a guess — check them against your billing page")
        return lines


METER = Meter()
