"""Meeting Support Bot — local demo.

    python -m demo.main             # needs a key in .env
    python -m demo.main --offline   # no key: canned slide, exercises the whole rig

Two answer buttons, deliberately different jobs:

    F9   what was SAID. Transcript only, blind to the screen.
    F10  what was said AND what is shown. Sends the real screenshot, so it reads
         detail the screen loop's one-line summary never recorded. Costs a
         vision round trip. With no transcript yet it is just "read the screen".

F8 re-picks the capture region, F12 quits. Answers are spoken to your pinned
output device and mirrored to a private overlay.

"""

from __future__ import annotations

import argparse
import queue
import re
import sys
import threading
import time
import tkinter as tk

from . import audio_loop as audio_loop_mod, config, knowledge, region as region_mod, speech
from .audio_loop import AudioLoop
from .history import History
from .meter import METER
from .overlay import Overlay
from .screen_loop import ScreenLoop, ScreenState

_events: "queue.Queue[tuple]" = queue.Queue()

# `heard [Them]: ...` lines get speaker colouring in the history window; every
# other log line is plain status.
_HEARD_RE = re.compile(r"^heard \[(\w+)\](?: \(\d+s\))?: (.*)$", re.DOTALL)


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)
    # Mirrored into the history window by the tk pump. Queued rather than drawn
    # here because log() is called from the capture and answer threads, and
    # tkinter must only be touched from the thread running its main loop.
    _events.put(("log", message, "", "", False))


def split_answer(body: str) -> tuple[str, str]:
    """(to speak, to print). A line of --- separates them.

    Code cannot be listened to. When the answer carries something you have to
    look at, the sentence above the marker is spoken and the block below is only
    printed — so the speech stays short while the console holds the real thing.
    """
    lines = body.splitlines()
    for index, line in enumerate(lines):
        if line.strip() == config.READ_MARKER:
            spoken = "\n".join(lines[:index]).strip()
            return (spoken or body), body
    return body, body


def log_answer(body: str) -> None:
    """Print the answer under the timing line.

    Speech is gone the moment it plays, which is fine for a number and useless
    for anything you need to look at — code, an exact name, a command. The
    console keeps it, interleaved with the `heard [...]` lines, so the window is
    a running record of the meeting and what was answered.
    """
    for line in body.splitlines():
        if line.strip().startswith("```"):
            continue  # markdown fences are clutter in a console
        print(f"           | {line}", flush=True)


# --- answer building -------------------------------------------------------


def build_answer(state: ScreenState, rows: list[dict]) -> tuple[str, str, str, str]:
    """Returns (title, body, spoken, source). No network, no model call.

    The screen loop has already answered the question the screen raises, whatever
    it happens to be. The sales table is no longer the answer — it is an extra
    line grafted on when an on-screen figure happens to match a deal.
    """
    title = state.headline or state.summary or "On screen"
    body = state.answer or state.summary or "Nothing readable captured yet."
    spoken = body
    source = "screen"

    hit = knowledge.match(state.numbers, rows)
    if hit:
        row, number = hit
        amount = knowledge.format_money(row["_amount"])
        detail = (
            f"Sales table: {row['deal']} — {amount}, "
            f"owner {row['owner']}, {row['account']}, {row['stage']}."
        )
        body = f"{body}\n\n{detail}"
        spoken = f"{spoken} {detail}"
        source = "screen + sales table"

    return title, body, spoken, source


# --- trigger path ----------------------------------------------------------


class Trigger:
    def __init__(
        self,
        loop: ScreenLoop,
        rows: list[dict],
        speaker: speech.Speaker | None,
        audio: AudioLoop | None = None,
    ):
        self._loop = loop
        self._rows = rows
        self._speaker = speaker
        self._audio = audio
        self._busy = threading.Lock()

    def fire_audio(self) -> None:
        """F9 — what was said. Transcript only, never the screen."""
        self._dispatch(self._run_audio)

    def fire_full(self) -> None:
        """F10 — what was said and what is shown, with the real screenshot."""
        self._dispatch(self._run_full)

    def _dispatch(self, target) -> None:
        if not self._busy.acquire(blocking=False):
            return  # already handling a press
        threading.Thread(target=self._guard, args=(target,), daemon=True).start()

    def _guard(self, target) -> None:
        try:
            target()
        finally:
            self._busy.release()

    # --- F9: audio only ----------------------------------------------------

    def _run_audio(self) -> None:
        started = time.perf_counter()
        if self._audio is None:
            self._emit("Not listening", "The audio loop is off — F10 reads the screen.", "", False)
            return

        # Transcribe the last few seconds now. The question you just heard exists
        # only as raw samples until this runs — without it the button confidently
        # answers the previous topic.
        self._audio.flush_and_wait()

        if not self._audio.has_transcript():
            # Distinguish "nobody has spoken" from "the capture is broken". They
            # look the same from here and need completely different fixes.
            fault = self._audio.status()
            if fault:
                log(f"button -> audio loop is not working: {fault}")
                self._emit(
                    "Not hearing anything",
                    f"{fault}\n\nRun check_setup. F10 still reads the screen.",
                    "audio loop fault", False,
                    spoken="Not hearing anything. Press F10 to read the screen.",
                )
            else:
                self._emit(
                    "Nothing transcribed yet",
                    f"Hearing audio (level {self._audio.level:.4f}) but nothing "
                    f"transcribed yet — chunks close at a pause in speech.",
                    "listening", False,
                    spoken="Nothing transcribed yet.",
                )
            return

        live = self._audio.answer_now()
        if live:
            self._deliver("From the room", live, started, "live from transcript", "live transcript")
        else:
            self._emit("Answer failed", "Could not answer from the transcript.", "", False)

    # --- F10: transcript + screenshot ---------------------------------------

    def _run_full(self) -> None:
        started = time.perf_counter()
        frame = self._loop.latest_frame()

        # No audio loop and no frame means offline mode: fall back to the local
        # screen-state answer, which needs neither.
        if self._audio is None or frame is None:
            self._run_screen(started)
            return

        # Same reason as F9: include what was said in the last few seconds.
        self._audio.flush_and_wait()
        answer = self._audio.answer_with_screenshot(frame)
        if not answer:
            self._run_screen(started)
            return
        self._deliver("Screen + room", answer, started, "screenshot + transcript", "full")

    def _run_screen(self, started: float) -> None:
        """Local screen answer. No model call — the screen loop already did it."""
        state = self._loop.latest()
        if state is None:
            # With the background loop off there is no cached description, so
            # this only happens when the screenshot itself could not be taken.
            self._emit("No screen", "Could not capture the screen.", "", False,
                       spoken="Could not capture the screen.")
            return
        if state.error and not state.summary:
            self._emit("Screen loop error", state.error, "", False,
                       spoken="The screen loop errored.")
            return

        title, body, spoken, source = build_answer(state, self._rows)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        meta = f"{elapsed_ms}ms · {source} · screen {state.age_seconds:.0f}s old"
        _events.put(("answer", title, body, meta, "sales table" in source))
        log(f"button -> {elapsed_ms}ms ({source})")
        log_answer(body)
        self._speak(spoken)

    # --- shared -------------------------------------------------------------

    def _deliver(self, title: str, body: str, started: float, note: str,
                 label: str, question: str = "") -> None:
        # Delivered answers enter the history, so the next question has context.
        if self._audio is not None:
            self._audio.record_delivered(body, question)
        spoken, printed = split_answer(body)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
        _events.put(("answer", title, printed, f"{elapsed_ms}ms · {note}", True))
        log(f"button -> {elapsed_ms}ms ({label})")
        log_answer(printed)
        self._speak(spoken)

    def _emit(self, title: str, body: str, meta: str, accent: bool, spoken: str = "") -> None:
        """Show a card if the overlay is on, and always say something.

        With the overlay off — the default — the voice is the only channel, so a
        path that emits without speaking is a press that does nothing at all.
        """
        _events.put(("answer", title, body, meta, accent))
        self._speak(spoken or body)

    def _speak(self, text: str) -> None:
        if self._speaker is not None:
            self._speaker.say(text)


# --- wiring ----------------------------------------------------------------


def build_client(offline: bool):
    """A key is optional only in offline mode.

    Offline disables the *screen loop*, not speech — so once the key lands you
    can test earphone output immediately without picking a capture region.
    """
    if config.missing_key():
        if offline:
            return None
        print(
            "No OPENAI_API_KEY found.\n"
            "  Copy .env.example to .env and add the key, or run with --offline\n"
            "  to exercise the rig without one.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    from openai import OpenAI

    return OpenAI(api_key=config.OPENAI_API_KEY)


def main() -> None:
    parser = argparse.ArgumentParser(description="Meeting Support Bot demo")
    parser.add_argument(
        "--offline", action="store_true",
        help="run with a canned slide and no API calls (no key needed)",
    )
    parser.add_argument(
        "--pick-region", action="store_true", help="re-select the capture region first"
    )
    parser.add_argument("--no-tts", action="store_true", help="text overlay only")
    args = parser.parse_args()

    client = build_client(args.offline)

    if config.HIDE_FROM_CAPTURE:
        from . import privacy

        hidden, why = privacy.hide_console()
        log(f"screen-share: {why}")
        if not hidden:
            log("screen-share: WARNING — answers print here and WILL be shared")

    region = None
    if not args.offline:
        region = region_mod.select() if args.pick_region else region_mod.load_or_select()
        if region is None:
            print("No capture region selected. Nothing to watch.", file=sys.stderr)
            raise SystemExit(1)
        log(f"watching region {region}")

    if config.DOCS_ENABLED:
        from . import docs as documents
        log(documents.summary())
        documents.load(on_event=log)   # surfaces skipped files at startup

    rows = knowledge.load_rows(config.SALES_CSV)
    log(f"loaded {len(rows)} rows from {config.SALES_CSV.name}")

    speaker = None
    if config.TTS_ENABLED and not args.no_tts and client is not None:
        device = speech.resolve_device(config.TTS_DEVICE)
        if config.TTS_DEVICE and device is None:
            log(f"WARNING: no output device matching {config.TTS_DEVICE!r} — using default")
        speaker = speech.Speaker(client, device=device, on_error=lambda m: log(m))
        log(f"speech on, device={device if device is not None else 'system default'}")
    elif client is None:
        log("speech off — needs an API key")
    else:
        log("speech off by request")

    loop = ScreenLoop(region or {}, client, offline=args.offline, on_event=log)
    loop.start()

    audio = None
    if config.AUDIO_ENABLED and not args.offline and client is not None:
        sources = []
        loopback = audio_loop_mod.input_candidates(config.AUDIO_DEVICE)
        if loopback:
            sources.append((audio_loop_mod.THEM, loopback))
        else:
            log(f"WARNING: no loopback device matching {config.AUDIO_DEVICE!r}")
            log("         the bot will not hear the other people in the call")

        if config.MIC_ENABLED:
            wanted = config.MIC_DEVICE or audio_loop_mod.default_input_name()
            mic = audio_loop_mod.input_candidates(wanted)
            if mic:
                sources.append((audio_loop_mod.YOU, mic))
            else:
                log(f"WARNING: no microphone matching {wanted!r} — not hearing you")

        if not sources:
            log("WARNING: no audio sources at all — F9 will not work")
            log("         run `python -m demo.check_setup` to see what is available")
        else:
            audio = AudioLoop(
                client,
                sources,
                # Gate on our own playback. Loopback would hear the answer
                # directly and the mic would hear it out of the speakers —
                # either way the bot transcribes itself and answers itself.
                is_muted=(speaker.is_speaking if speaker is not None else None),
                on_event=log,
            )
            audio.start()
            # Sources die on their own threads, so without this the app looks
            # healthy while F9 quietly has nothing to work with.
            time.sleep(1.5)
            for line in audio.health():
                log(f"  audio {line}")
            if not audio.is_alive():
                log("WARNING: every audio source died on startup — F9 will not work")
                log("         run `python -m demo.check_setup` to diagnose")
    elif args.offline:
        log("audio loop off — offline mode")
    elif not config.AUDIO_ENABLED:
        log("audio loop off by config")

    trigger = Trigger(loop, rows, speaker, audio)

    root = tk.Tk()
    root.withdraw()
    # "history" is one persistent window; the other modes are per-press cards.
    # They are alternatives, so only one is ever built.
    use_history = config.OVERLAY_MODE == "history"
    overlay = Overlay(root, mode="off" if use_history else config.OVERLAY_MODE)
    history = History(root, enabled=use_history)

    def pump() -> None:
        try:
            while True:
                kind, title, body, meta, accent = _events.get_nowait()
                if kind == "answer":
                    overlay.show(title, body, meta, accent=accent)
                    history.answer(body, meta)
                elif kind == "log":
                    heard = _HEARD_RE.match(title)
                    if heard:
                        history.heard(heard.group(1), heard.group(2))
                    else:
                        history.note(title)
        except queue.Empty:
            pass
        root.after(40, pump)

    def quit_all() -> None:
        loop.stop()
        if audio:
            audio.stop()
        if speaker:
            speaker.stop()
        root.quit()

    def repick() -> None:
        log("re-pick region: restart with --pick-region")
        _events.put(("answer", "Re-pick region", "Run with --pick-region to reselect.", "", False))

    from pynput import keyboard as kb

    # A plain Listener with explicit matching, NOT GlobalHotKeys. The latter
    # silently fails to fire on some machines while the keys are demonstrably
    # arriving — measured 0/3 against 3/3 for a Listener on identical presses —
    # and a button that does nothing with no error is the worst failure this
    # app has. Single keys need no combo parsing anyway.
    actions = {}
    for spec, action in (
        (config.HOTKEY_AUDIO, trigger.fire_audio),
        (config.HOTKEY_FULL, trigger.fire_full),
        (config.HOTKEY_REGION, repick),
        (config.HOTKEY_HISTORY, lambda: root.after(0, history.toggle)),
        (config.HOTKEY_QUIT, lambda: root.after(0, quit_all)),
    ):
        key = getattr(kb.Key, spec.strip("<>").lower(), None)
        if key is None:
            log(f"WARNING: unknown hotkey {spec!r} — that button will not work")
            continue
        actions[key] = action

    def on_press(key) -> None:
        action = actions.get(key)
        if action is not None:
            action()

    hotkeys = kb.Listener(on_press=on_press)
    hotkeys.start()

    key = lambda h: h.strip("<>").upper()
    log(
        f"ready — {key(config.HOTKEY_AUDIO)} what was said, "
        f"{key(config.HOTKEY_FULL)} said + screen, "
        f"{key(config.HOTKEY_QUIT)} quit (or Ctrl+C here)"
    )
    if not actions:
        log("WARNING: no hotkeys registered — check the HOTKEY_ names in .env")
    if args.offline:
        status = "offline mode"
    elif audio is not None:
        status = "listening · F10 grabs the screen"
    else:
        status = "not listening · F10 grabs the screen"
    overlay.show(
        "Ready",
        f"{key(config.HOTKEY_AUDIO)} — what was just said\n"
        f"{key(config.HOTKEY_FULL)} — that, plus what's on screen",
        status,
    )

    root.after(40, pump)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        pass
    finally:
        loop.stop()
        if audio:
            audio.stop()
        if speaker:
            speaker.stop()
        hotkeys.stop()
        log("stopped")
        # Last thing printed, so a session ends by saying what it spent. Printed
        # rather than logged: the history window is already gone by now.
        for line in METER.report():
            print(f"           {line}", flush=True)


if __name__ == "__main__":
    main()
