"""Pre-flight check. Run this before the demo, and again when something breaks.

    python -m demo.check_setup

Checks imports, the API key, whether the configured model ids actually exist,
which output device the spoken answer will land on, and whether a capture
region has been saved.
"""

from __future__ import annotations

import sys
import time

from . import audio_loop, config, knowledge, region as region_mod, speech

OK, WARN, BAD = "  ok  ", " warn ", " FAIL "


def line(status: str, message: str) -> None:
    print(f"[{status}] {message}")


def check_imports() -> bool:
    ok = True
    for module, why in [
        ("mss", "screen capture"),
        ("numpy", "frame diff"),
        ("PIL", "image encoding"),
        ("pynput", "global hotkey"),
        ("openai", "API client"),
        ("sounddevice", "audio in and out"),
        ("soundfile", "chunk encoding for transcription"),
        ("tkinter", "overlay"),
    ]:
        try:
            __import__(module)
            line(OK, f"{module} ({why})")
        except Exception as exc:
            line(BAD, f"{module} ({why}): {exc}")
            ok = False
    return ok


def check_models() -> None:
    if config.missing_key():
        line(WARN, "no OPENAI_API_KEY — run the demo with --offline until you have one")
        return
    try:
        from openai import OpenAI

        client = OpenAI(api_key=config.OPENAI_API_KEY)
        available = {m.id for m in client.models.list()}
    except Exception as exc:
        line(BAD, f"could not reach the API: {exc}")
        return

    line(OK, f"API reachable, {len(available)} models visible")
    for label, model in [
        ("vision", config.VISION_MODEL),
        ("tts", config.TTS_MODEL),
        ("transcribe", config.TRANSCRIBE_MODEL),
        ("answer", config.ANSWER_MODEL),
    ]:
        if model in available:
            line(OK, f"{label} model {model!r} exists")
        else:
            line(BAD, f"{label} model {model!r} NOT available to this key")
            hint = sorted(m for m in available if m.split("-")[0] in model.split("-")[0])
            if hint:
                line(WARN, f"   closest ids: {', '.join(hint[:6])}")
            line(WARN, f"   set it in .env, e.g. {label.upper()}_MODEL=<id>")


def check_audio() -> None:
    devices = speech.list_output_devices()
    if not devices:
        line(BAD, "no output devices found — speech will not work")
        return
    line(OK, f"{len(devices)} output devices")
    target = speech.resolve_device(config.TTS_DEVICE)
    if not config.TTS_DEVICE:
        line(WARN, "TTS_DEVICE is blank — speech goes to the system default")
        line(WARN, "   fine for a solo test; pin it to your earphones for a real meeting")
    elif target is None:
        line(BAD, f"no device matches {config.TTS_DEVICE!r} — falling back to default")
    else:
        name = next(n for i, n, _c in devices if i == target)
        line(OK, f"speech pinned to [{target}] {name}")
    print("       available:")
    for index, name, channels in devices:
        print(f"         [{index:>2}] {name}  ({channels}ch)")


def _device_hint() -> None:
    line(WARN, "   Windows enumerates loopback devices it will not let you open,")
    line(WARN, "   so a name in the list proves nothing. To fix:")
    line(WARN, "     Win+R -> mmsys.cpl -> Recording tab")
    line(WARN, "     right-click empty space, tick 'Show Disabled Devices'")
    line(WARN, "     right-click Stereo Mix -> Enable -> Set as Default")
    line(WARN, "   If it is already enabled and still fails, something is holding")
    line(WARN, "   it: close other audio apps, or unplug/replug your headset to")
    line(WARN, "   make the driver re-enumerate. Last resort, install VB-Audio")
    line(WARN, "   Cable and set AUDIO_DEVICE=CABLE Output in .env.")


def check_mic() -> None:
    """The 'You' source. Passive: a tone probe would only test the speakers."""
    if not config.MIC_ENABLED:
        line(WARN, "MIC_ENABLED=0 — the bot will not hear anything you say")
        return
    wanted = config.MIC_DEVICE or audio_loop.default_input_name()
    candidates = audio_loop.input_candidates(wanted)
    if not candidates:
        line(BAD, f"no microphone matches {wanted!r} — the bot cannot hear you")
        return

    import numpy as np

    levels: list[float] = []
    try:
        stream, index, name, api, _errors = audio_loop.open_input(
            candidates, dtype="float32", blocksize=2048,
            callback=lambda d, *a: levels.append(
                float(np.sqrt(np.mean(np.square(d.mean(axis=1)))))
            ),
        )
    except Exception as exc:
        line(BAD, f"microphone will not open: {str(exc).splitlines()[0][:70]}")
        return
    print("       measuring your mic for 2s — say something...")
    time.sleep(2.0)
    try:
        stream.stop()
        stream.close()
    except Exception:
        pass

    peak = max(levels) if levels else 0.0
    if peak >= config.AUDIO_SILENCE_RMS:
        line(OK, f"mic hears you: [{index}] {name} via {api} (peak {peak:.5f})")
    else:
        line(BAD, f"mic is silent (peak {peak:.5f} < floor {config.AUDIO_SILENCE_RMS})")
        line(WARN, "   [{}] {} via {}".format(index, name, api))
        line(WARN, "   it opens but hears nothing. Usually muted:")
        line(WARN, "     mmsys.cpl -> Recording -> your mic -> Properties -> Levels")
        line(WARN, "     unmute and raise the slider; also check Settings ->")
        line(WARN, "     Privacy -> Microphone lets desktop apps use it.")
        line(WARN, "   Speak during the 2s window — silence here reads the same.")


def check_audio_in() -> None:
    """The listening path. Without an input device the bot is screen-only."""
    if not config.AUDIO_ENABLED:
        line(WARN, "AUDIO_ENABLED=0 — the bot will not listen to the meeting")
        return
    devices = audio_loop.list_input_devices()
    if not devices:
        line(BAD, "no input devices found — the bot cannot hear the meeting")
        return
    candidates = audio_loop.input_candidates(config.AUDIO_DEVICE)
    if not candidates:
        line(BAD, f"no input device matches {config.AUDIO_DEVICE!r} — not listening")
        line(WARN, "   enable it in Sound > Recording, or set AUDIO_DEVICE in .env")
        _device_hint()
    else:
        line(OK, f"{len(candidates)} device(s) match {config.AUDIO_DEVICE!r}, best API first:")
        for index, name, api in candidates:
            print(f"         [{index:>2}] {api:<20} {name}")
        print("       probing each with a test tone (you will hear short beeps)...")

        working = None
        floor = config.AUDIO_SILENCE_RMS
        for index, name, api in candidates:
            try:
                quiet, tone = audio_loop.probe_loopback(index)
            except Exception as exc:
                line(WARN, f"[{index}] {api}: will not open — {str(exc).splitlines()[0][:60]}")
                continue
            # The question is only "is real audio reaching this device". Do NOT
            # require tone >> quiet: if something is already playing, the
            # baseline is loud too, and a ratio test then fails on a device that
            # is plainly working.
            if max(quiet, tone) >= floor:
                if quiet >= floor:
                    line(OK, f"[{index}] {api}: hears audio (already playing, "
                             f"{quiet:.5f}; tone {tone:.5f})")
                else:
                    line(OK, f"[{index}] {api}: hears audio "
                             f"(silent {quiet:.6f} -> tone {tone:.5f})")
                working = (index, name, api)
                break
            line(WARN, f"[{index}] {api}: opens but hears NOTHING "
                       f"(quiet {quiet:.6f}, tone {tone:.6f}, floor {floor})")

        if working is None:
            line(BAD, "no matching device can actually capture — the bot will hear nothing")
            _device_hint()
        else:
            index, name, api = working
            line(OK, f"listening will use [{index}] {name} via {api}")
            line(WARN, "   a loopback taps the SPEAKER mix: if you route the call to")
            line(WARN, "   earbuds only, it may hear nothing even when enabled.")
    print("       all inputs:")
    for index, name, channels in devices:
        print(f"         [{index:>2}] {name}  ({channels}ch)")


def check_data() -> None:
    rows = knowledge.load_rows(config.SALES_CSV)
    if rows:
        line(OK, f"{len(rows)} rows in {config.SALES_CSV.name}")
    else:
        line(BAD, f"no rows loaded from {config.SALES_CSV}")

    saved = region_mod.load()
    if saved:
        line(OK, f"capture region saved: {saved}")
    else:
        line(WARN, "no capture region yet — you'll be asked to drag one on first run")


def meter(seconds: float = 30.0) -> None:
    """Live input level. Play something and watch whether the number moves.

    The tone probe answers "can it hear a beep right now". This answers "is it
    hearing the thing I am actually playing", which is the question you have when
    the device opens, streams, and still transcribes nothing.
    """
    import numpy as np
    import sounddevice as sd

    candidates = audio_loop.input_candidates(config.AUDIO_DEVICE)
    if not candidates:
        line(BAD, f"no input device matches {config.AUDIO_DEVICE!r}")
        return

    blocks: list[float] = []

    def callback(indata, _n, _t, _s) -> None:
        blocks.append(float(np.sqrt(np.mean(np.square(indata.mean(axis=1))))))

    try:
        stream, index, name, api, errors = audio_loop.open_input(
            candidates, dtype="float32", blocksize=2048, callback=callback
        )
    except Exception as exc:
        line(BAD, f"nothing would open: {exc}")
        return
    for skipped in errors:
        line(WARN, f"skipped {skipped}")

    floor = config.AUDIO_SILENCE_RMS
    print(f"\n  [{index}] {name} via {api}")
    print(f"  floor is {floor} — the bar must cross the | to be transcribed")
    print("  PLAY YOUR AUDIO NOW. Ctrl+C to stop.\n")
    try:
        for _ in range(int(seconds * 2)):
            time.sleep(0.5)
            level = max(blocks) if blocks else 0.0
            blocks.clear()
            # Log scale: the interesting range spans four orders of magnitude.
            filled = 0 if level <= 0 else min(40, max(0, int((np.log10(level) + 5) * 10)))
            mark = int((np.log10(floor) + 5) * 10)
            bar = "".join(
                "|" if i == mark else ("#" if i < filled else ".") for i in range(40)
            )
            verdict = "TRANSCRIBED" if level >= floor else "dropped as silence"
            print(f"  {level:.6f} [{bar}] {verdict}      ", end="\r", flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        print()
        try:
            stream.stop()
            stream.close()
        except Exception:
            pass


def keys(seconds: float = 30.0) -> None:
    """Show which keys actually reach the app, and whether the hotkeys fire.

    "F9 does nothing" has three completely different causes and they are
    indistinguishable from the outside: the key never arrives (laptop Fn-lock
    sends media keys instead of F9), it arrives but the hotkey does not match, or
    it fires fine and the answer went somewhere you did not notice. This tells
    you which.
    """
    from pynput import keyboard as kb

    fired: list[str] = []
    seen: list[str] = []

    def note(name: str):
        def handler() -> None:
            fired.append(name)
            print(f"\n  >>> {name} FIRED — the hotkey works", flush=True)
        return handler

    # Same mechanism the app uses, so this proves the real thing rather than a
    # lookalike.
    actions = {}
    for spec, label in (
        (config.HOTKEY_AUDIO, "F9 answer"),
        (config.HOTKEY_FULL, "F10 screen"),
        (config.HOTKEY_REGION, "F8 region"),
        (config.HOTKEY_QUIT, "F12 quit"),
    ):
        key = getattr(kb.Key, spec.strip("<>").lower(), None)
        if key is None:
            line(BAD, f"{spec!r} is not a key name pynput knows")
            continue
        actions[key] = note(f"{spec} ({label})")

    def on_press(key) -> None:
        name = getattr(key, "name", None) or str(key)
        seen.append(name)
        print(f"  key seen: {name}", flush=True)
        action = actions.get(key)
        if action is not None:
            action()

    listener = kb.Listener(on_press=on_press)
    listener.start()

    print(f"\n  Configured: {config.HOTKEY_AUDIO} {config.HOTKEY_FULL} "
          f"{config.HOTKEY_REGION} {config.HOTKEY_QUIT}")
    print(f"  PRESS F9, F10, F8 and F12 now — {int(seconds)}s. Ctrl+C to stop.\n")
    try:
        time.sleep(seconds)
    except KeyboardInterrupt:
        pass
    listener.stop()

    print()
    if fired:
        line(OK, f"{len(fired)} hotkey(s) fired: {', '.join(fired)}")
        line(WARN, "   so the keys work — if the app seems dead, the answer is")
        line(WARN, "   going somewhere you are not seeing. The overlay is off by")
        line(WARN, "   default, so watch the CONSOLE for 'button -> ...' lines.")
    elif seen:
        arrived = list(dict.fromkeys(seen))
        line(BAD, "keys arrived, but none matched a hotkey")
        line(WARN, f"   what arrived: {', '.join(arrived)[:140]}")
        line(WARN, "   Your F-row is in multimedia mode: pressing F9 sends its")
        line(WARN, "   secondary function instead of F9. Two ways out.")
        print()
        line(WARN, "   1. Turn on Function Lock so F-keys send F-keys. Usually")
        line(WARN, "      Fn+Esc, sometimes a padlock icon on Esc or Fn, or")
        line(WARN, "      BIOS > 'Action Keys Mode' / 'Hotkey Mode' = Disabled.")
        print()

        # Anything that arrived intact and is not a modifier, a character, or
        # already bound is a candidate the user has physically confirmed works.
        modifiers = {
            "ctrl_l", "ctrl_r", "shift", "shift_r", "alt_l", "alt_r", "alt_gr",
            "cmd", "cmd_r", "caps_lock", "tab", "esc", "enter", "backspace",
        }
        bound = {getattr(k, "name", str(k)) for k in actions}
        usable = [
            name for name in arrived
            if name not in modifiers and name not in bound
            and not name.startswith("'") and not name.startswith("<")
        ]
        line(WARN, "   2. Or bind to a key that does arrive. Put in .env:")
        if usable:
            for spec, name in zip(("HOTKEY_AUDIO", "HOTKEY_FULL", "HOTKEY_QUIT"), usable):
                line(WARN, f"        {spec}=<{name}>")
            if len(usable) < 3:
                line(WARN, "      (press more keys to find enough candidates)")
        else:
            line(WARN, "        HOTKEY_AUDIO=<pause>")
            line(WARN, "        HOTKEY_FULL=<scroll_lock>")
            line(WARN, "        HOTKEY_QUIT=<insert>")
            line(WARN, "      then re-run this to confirm those arrive.")
        print()
        line(WARN, "   Re-run and press your candidates first to check them.")
    else:
        line(BAD, "no key presses reached the app at all")
        line(WARN, "   pynput cannot see input going to a window running as")
        line(WARN, "   administrator unless it is elevated too. Close elevated")
        line(WARN, "   apps, or run this terminal as administrator.")


def say() -> None:
    """Speak a test line through the configured device, so you know you'd hear it.

    "I pressed the button and nothing was said" has two causes that look the
    same: the press never happened, or it did and the audio went somewhere you
    are not listening. This settles the second.
    """
    if config.missing_key():
        line(BAD, "no API key — cannot test speech")
        return
    from openai import OpenAI

    from . import speech

    target = speech.resolve_device(config.TTS_DEVICE)
    if config.TTS_DEVICE and target is None:
        line(BAD, f"TTS_DEVICE={config.TTS_DEVICE!r} matches no output device")
        line(WARN, "   the name must appear in the list from `check_setup`.")
        line(WARN, "   Non-ASCII names work only if .env is saved as UTF-8 —")
        line(WARN, "   if in doubt use an ASCII fragment of the device name.")
        return
    where = "system default" if target is None else f"[{target}]"
    if target is not None:
        name = next((n for i, n, _c in speech.list_output_devices() if i == target), "?")
        where = f"[{target}] {name}"
    print(f"\n  speaking through {where} at speed {config.TTS_SPEED}...")
    print("  LISTEN NOW.\n")

    errors: list[str] = []
    speaker = speech.Speaker(
        OpenAI(api_key=config.OPENAI_API_KEY), device=target,
        on_error=lambda m: errors.append(m),
    )
    speaker.say(
        "This is the meeting support bot. If you can hear this, "
        "spoken answers will reach you."
    )
    deadline = time.monotonic() + 30
    while speaker.is_speaking() and time.monotonic() < deadline:
        time.sleep(0.1)
    speaker.stop()

    if errors:
        for message in errors:
            line(BAD, message)
    else:
        line(OK, "speech played without error")
        line(WARN, "   if you heard nothing, it went to the wrong device —")
        line(WARN, "   set TTS_DEVICE in .env to a fragment of the right name.")


def main() -> None:
    if "--say" in sys.argv:
        say()
        return
    if "--keys" in sys.argv:
        keys()
        return
    if "--meter" in sys.argv:
        meter()
        return
    print(f"python {sys.version.split()[0]}\n")
    print("-- imports")
    check_imports()
    print("\n-- api")
    check_models()
    print("\n-- audio out")
    check_audio()
    print("\n-- audio in: them (loopback)")
    check_audio_in()
    print("\n-- audio in: you (microphone)")
    check_mic()
    print("\n-- data")
    check_data()
    print("\nWhen this is clean:  python -m demo.main")


if __name__ == "__main__":
    main()
