"""Standard latency benchmark: `python -m demo.bench`.

Every speed claim in this project's config comments came from a throwaway script
that no longer exists, which meant re-deriving the same numbers each time a
model or provider changed. This is that script, kept.

WHAT IT MEASURES. The three legs of a press, separately, because they have
completely different fixes:

    transcribe   audio in the buffer -> text          (Groq, OpenAI fallback)
    answer       text -> the spoken sentence is ready (F9 / F11)
    speak        that sentence -> first sound         (SAPI, API fallback)

Plus F10, which replaces the answer leg with a vision call.

HOW TO READ IT. The headline is `press -> first word`: the sum of the legs, and
what you actually wait through before hearing anything. `press -> done talking`
adds the playback itself, which is usually the largest single number on the
page and the one people forget. Both are medians; the range next to them is
what varies run to run, and it is wide enough that a change under ~150ms is not
a result.

WHY THE FIXTURES ARE FIXED. Same sentences every run, in both languages, so two
runs a month apart are comparable. The audio for the transcribe leg is
synthesised locally rather than recorded, so there is no fixture file to lose
and no microphone in the loop; the screenshot for F10 is drawn here for the
same reason.

THE FIRST CALL TO EVERY PROVIDER IS DISCARDED. Cold TLS to Groq measured 1959ms
against 617ms warm on the identical question — benchmarking it produces a
number that describes a handshake, not a model.

BACK-TO-BACK RUNS THROTTLE A FREE TIER, and the result looks exactly like a slow
model. Measuring llama-3.1-8b in a tight loop gave 12.9s and a 325-11619ms
spread; the same model at 25s between calls answered in 333-487ms. Nothing here
is slow enough to trip it at `--runs 3`, but if a Groq leg suddenly reports
seconds, space the calls out before believing it — that number is a rate limit
wearing a model's name.
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics as st
import sys
import time

from . import config

# --- fixtures ---------------------------------------------------------------

# Read aloud by SAPI to make the transcribe-leg audio. Long enough to be a
# realistic press (a sweep drains ~20s) and full of the things that break
# speech-to-text: figures, product names, a date.
SPEECH = {
    "en": "It's a Realtek codec, and the pricing question came up again. Do we hold "
          "the unit price at ninety eight thousand yen for the second batch, or does "
          "the volume discount apply from unit fifty? The customer wants an answer "
          "before Friday.",
    "ja": "設置場所の件ですが、来週の金曜日までに仕様を送っていただけますか。"
          "それと、単価は九万八千円のままで大丈夫でしょうか。",
}

# Fed to the answer legs directly, so the answer measurement does not inherit
# whatever the transcriber happened to mishear that run.
MEETING = {
    "en": "Them: It's a Realtek codec, and the pricing question came up again. Do we "
          "hold the unit price at 98,000 yen for the second batch, or does the volume "
          "discount apply from unit 50?",
    "ja": "Them: 設置場所の件ですが、来週の金曜日までに仕様を送っていただけますか。"
          "それと、単価は九万八千円のままで大丈夫でしょうか。",
}

# General-knowledge questions: the answer comes from the model rather than from
# what was said in the room, and the two cases time differently — the main model
# reasons before answering a question aimed at the user and does not bother for
# a definition.
GENERAL = {
    "en": "Them: Python has a global interpreter lock. What does that actually mean "
          "for a CPU-bound workload, and how would you work around it?",
    "ja": "Them: オブジェクト指向設計において、継承とコンポジションの違いを"
          "説明してください。どちらを優先すべきだと考えますか。",
}

# Asked in each language, because the answer comes back in the language of the
# question: measuring F10 with an English prompt in the Japanese section would
# report a number for a case that never happens.
_CODE_QUESTION = {
    "en": "JUST SAID (answer the most recent question in here):\n"
          "Them: Okay, take a look at this one and walk me through how you'd solve it.",
    "ja": "JUST SAID (answer the most recent question in here):\n"
          "Them: では、この問題を見て、どう解くか説明してもらえますか。",
}


def _screenshot():
    """A coding-interview screen, drawn rather than captured so it never drifts."""
    from PIL import Image, ImageDraw, ImageFont

    def font(size, bold=False):
        for path in ((r"C:\Windows\Fonts\consolab.ttf" if bold
                      else r"C:\Windows\Fonts\consola.ttf"),
                     r"C:\Windows\Fonts\arial.ttf"):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
        return ImageFont.load_default()

    image = Image.new("RGB", (1600, 900), (30, 30, 32))
    draw = ImageDraw.Draw(image)
    draw.rectangle([0, 0, 780, 900], fill=(250, 250, 250))
    draw.text((40, 40), "1145. Binary Tree Coloring Game", font=font(30, True),
              fill=(20, 20, 20))
    body = """Two players play a turn based game on a binary tree.
We are given the root of this binary tree, and the
number of nodes n in the tree. n is odd, and each
node has a distinct value from 1 to n.

Initially, the first player names a value x with
1 <= x <= n, and the second player names a value y
with 1 <= y <= n and y != x. The first player colors
the node with value x red, and the second player
colors the node with value y blue.

Then the players take turns. In each turn, that
player chooses a node of their color and colors an
uncolored neighbor of the chosen node.

If a player cannot choose such a node they pass. If
both pass, the game ends, and the winner is the
player that colored more nodes.

You are the second player. If it is possible to
choose such a y to ensure you win, return true."""
    y = 100
    for line in body.split("\n"):
        draw.text((40, y), line, font=font(19), fill=(40, 40, 40))
        y += 27
    code = ("class Solution:\n    def btreeGameWinningMove(\n"
            "        self, root: Optional[TreeNode],\n        n: int, x: int\n"
            "    ) -> bool:")
    y = 100
    for line in code.split("\n"):
        draw.text((820, y), line, font=font(21), fill=(180, 220, 180))
        y += 30
    return image


# --- timing helpers ---------------------------------------------------------

def _stats(values: list[float]) -> dict:
    return {"median": st.median(values), "min": min(values), "max": max(values),
            "n": len(values)}


def _row(label: str, s: dict | None, note: str = "") -> str:
    if s is None:
        return f"  {label:<26} {'—':>8}   {note}"
    return (f"  {label:<26} {s['median']:>7.0f}ms   "
            f"{s['min']:.0f}-{s['max']:.0f}   {note}")


class Bench:
    def __init__(self, runs: int, play: bool):
        self.runs = runs
        # NOT `self.speak` — that name is the method below, and assigning the
        # flag over it makes the whole leg uncallable.
        self.play = play
        self.results: dict = {}
        from openai import OpenAI
        self.client = OpenAI(api_key=config.OPENAI_API_KEY)

    def _loop(self, transcript: str):
        """A real AudioLoop with one line of transcript already in it."""
        from .audio_loop import AudioLoop
        loop = AudioLoop(self.client, [("Them", "loopback")], on_event=lambda _m: None)
        loop._transcript.append((time.monotonic(), "Them", transcript))
        return loop

    # --- legs ---------------------------------------------------------------

    def transcribe(self, lang: str) -> dict | None:
        """Buffered audio -> text, through the real flush path."""
        import numpy as np
        from . import local_tts

        pcm = local_tts.synthesize(SPEECH[lang], 1.0)
        if pcm is None:
            return None
        clip = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        loop = self._loop(MEETING[lang])
        source = loop.sources[0]
        source.thread = __import__("threading").current_thread()
        source.samplerate = local_tts.SAMPLE_RATE

        times, heard = [], ""
        for i in range(self.runs + 1):          # +1: discarded warm-up
            with source.buf_lock:
                source.buf.clear()
                source.buf.append(clip.copy())
                source.buf_frames = len(clip)
            before = len(loop._archive)
            start = time.perf_counter()
            loop.flush_and_wait()
            elapsed = (time.perf_counter() - start) * 1000
            if len(loop._archive) > before:
                heard = loop._archive[-1][2]
            if i:
                times.append(elapsed)
        loop.close_transcript()
        seconds = len(clip) / local_tts.SAMPLE_RATE
        out = _stats(times)
        out["clip_seconds"] = seconds
        out["heard"] = heard
        return out

    def answer(self, kind: str, lang: str, transcripts: dict) -> dict | None:
        """Press -> the spoken sentence is complete. kind: f9 | f6 | f11."""
        method = {"f9": "answer_now", "f11": "answer_detailed"}[kind]
        getattr(self._loop(transcripts[lang]), method)()   # discarded warm-up

        times, said = [], ""
        for _ in range(self.runs):
            loop = self._loop(transcripts[lang])
            spoken: list[float] = []
            start = time.perf_counter()
            text = getattr(loop, method)(on_spoken=lambda _t: spoken.append(
                time.perf_counter()))
            end = spoken[0] if spoken else time.perf_counter()
            times.append((end - start) * 1000)
            said = text.split(config.READ_MARKER)[0].strip()
        out = _stats(times)
        out["said"] = said
        return out

    def vision(self, lang: str) -> dict | None:
        """F10: the screenshot replaces the transcript-only answer."""
        from .audio_loop import _FULL_PROMPT
        from .screen_loop import encode_jpeg

        try:
            encoded = encode_jpeg(_screenshot())
        except Exception as exc:
            print(f"  (no screenshot: {exc})", file=sys.stderr)
            return None
        messages = [
            {"role": "system", "content": _FULL_PROMPT},
            {"role": "user", "content": [
                {"type": "text", "text": _CODE_QUESTION[lang]},
                {"type": "image_url", "image_url": {
                    "url": f"data:image/jpeg;base64,{encoded}",
                    "detail": config.VISION_DETAIL}}]},
        ]
        times, prompt_tokens, said = [], [], ""
        for i in range(self.runs + 1):
            start = time.perf_counter()
            cue = None
            text = ""
            usage = None
            stream = self.client.chat.completions.create(
                model=config.VISION_MODEL, reasoning_effort=config.ANSWER_EFFORT,
                messages=messages, stream=True,
                stream_options={"include_usage": True})
            for chunk in stream:
                if getattr(chunk, "usage", None):
                    usage = chunk.usage
                if not chunk.choices:
                    continue
                text += chunk.choices[0].delta.content or ""
                if cue is None and config.READ_MARKER in text:
                    cue = time.perf_counter()
            elapsed = ((cue or time.perf_counter()) - start) * 1000
            if i:
                times.append(elapsed)
                if usage:
                    prompt_tokens.append(usage.prompt_tokens)
            said = text.split(config.READ_MARKER)[0].strip()
        out = _stats(times)
        out["said"] = said
        out["prompt_tokens"] = st.median(prompt_tokens) if prompt_tokens else 0
        return out

    def speak(self, text: str) -> dict | None:
        """Sentence -> first sound, and -> finished talking."""
        from . import local_tts, speech

        clipped = speech.shorten(text)
        first, whole, audio = [], [], None
        for i in range(self.runs + 1):
            start = time.perf_counter()
            audio = local_tts.synthesize(clipped, config.TTS_SPEED)
            if audio is None:
                return None
            synth = time.perf_counter()
            if not self.play:
                if i:
                    first.append((synth - start) * 1000)
                continue
            import sounddevice as sd
            stream = sd.RawOutputStream(samplerate=local_tts.SAMPLE_RATE, channels=1,
                                        dtype="int16", device=None, blocksize=0)
            stream.start()
            stream.write(audio[:4096])
            heard_at = time.perf_counter()
            for offset in range(4096, len(audio), 4096):
                stream.write(audio[offset:offset + 4096])
            stream.stop()
            stream.close()
            if i:
                first.append((heard_at - start) * 1000)
                whole.append((time.perf_counter() - start) * 1000)
        out = _stats(first)
        out["playback_seconds"] = len(audio) / 2 / local_tts.SAMPLE_RATE
        if whole:
            out["to_last_sound"] = st.median(whole)
        return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--runs", type=int, default=3,
                        help="measured runs per leg, after a discarded warm-up")
    parser.add_argument("--lang", choices=("en", "ja", "both"), default="both")
    parser.add_argument("--skip-vision", action="store_true",
                        help="skip F10, the slowest leg to measure")
    parser.add_argument("--play", action="store_true",
                        help="actually play the speech, to time it to the last "
                             "sound (makes noise)")
    parser.add_argument("--json", metavar="PATH",
                        help="also write the raw numbers here, to diff against a "
                             "later run")
    args = parser.parse_args(argv)

    if not config.OPENAI_API_KEY:
        print("no OPENAI_API_KEY — nothing to measure", file=sys.stderr)
        return 2

    bench = Bench(args.runs, args.play)
    languages = ("en", "ja") if args.lang == "both" else (args.lang,)
    print(f"answer model {config.ANSWER_MODEL} (effort {config.ANSWER_EFFORT!r})")
    print(f"{args.runs} runs per leg, first discarded\n")

    # Stamped, because these numbers are only meaningful next to what produced
    # them: a saved run with no date, machine or model list cannot be compared
    # against anything later.
    everything: dict = {"measured": {
        "at": time.strftime("%Y-%m-%d %H:%M"),
        "machine": platform.node(),
        "os": f"{platform.system()} {platform.release()}",
        "runs_per_leg": args.runs,
        "answer_model": config.ANSWER_MODEL,
        "answer_effort": config.ANSWER_EFFORT,
        "transcribe": (config.GROQ_TRANSCRIBE_MODEL if config.GROQ_API_KEY
                       else config.TRANSCRIBE_MODEL),
        "tts_speed": config.TTS_SPEED,
        "vision_detail": config.VISION_DETAIL,
    }}

    for lang in languages:
        print(f"=== {lang} " + "=" * 62)
        out: dict = {}
        out["transcribe"] = bench.transcribe(lang)
        out["f9"] = bench.answer("f9", lang, MEETING)
        out["f9_general"] = bench.answer("f9", lang, GENERAL)
        out["f11"] = bench.answer("f11", lang, MEETING)
        out["f10"] = None if args.skip_vision else bench.vision(lang)
        spoken = (out["f9"] or {}).get("said") or SPEECH[lang]
        out["speak"] = bench.speak(spoken)

        t = out["transcribe"]
        print(_row("transcribe", t,
                   f"{t['clip_seconds']:.0f}s clip" if t else "no local voice"))
        print(_row("answer  F9 (meeting)", out["f9"], config.ANSWER_MODEL))
        print(_row("answer  F9 (general)", out["f9_general"], config.ANSWER_MODEL))
        print(_row("answer  F11 (detail)", out["f11"], "spoken cue only"))
        if out["f10"]:
            print(_row("answer  F10 (screen)", out["f10"],
                       f"{out['f10']['prompt_tokens']:.0f} prompt tokens"))
        s = out["speak"]
        print(_row("speak -> first sound", s,
                   f"{s['playback_seconds']:.1f}s of audio" if s else "no local voice"))

        if t and out["f9"] and s:
            press = t["median"] + out["f9"]["median"] + s["median"]
            print(f"\n  {'PRESS -> FIRST WORD':<26} {press:>7.0f}ms")
            if "to_last_sound" in s:
                done = t["median"] + out["f9"]["median"] + s["to_last_sound"]
                print(f"  {'PRESS -> DONE TALKING':<26} {done:>7.0f}ms")
            else:
                print(f"  {'+ playback':<26} {s['playback_seconds'] * 1000:>7.0f}ms"
                      f"   (--play to time it properly)")
            out["press_to_first_word"] = press
        if out["f9"]:
            print(f"\n  F9  said: {out['f9']['said'][:96]}")
        if out["f10"]:
            print(f"  F10 said: {out['f10']['said'][:96]}")
        print()
        everything[lang] = out

    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(everything, handle, indent=2, ensure_ascii=False)
        print(f"raw numbers -> {args.json}")
    print("a change under ~150ms is inside the run-to-run range, not a result")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
