# Meeting Support Bot

During a meeting, a coworker shares a screen showing a number. You press a
button. Within about a second you get back what that number is and who owns it,
spoken quietly into your earphones. Nothing appears on screen.

No wake word, no bot voice in the meeting, no waiting.

## Core design principle

**Do the work before the button, not after.**

The naive version screenshots, transcribes, retrieves, and reasons *after* you
press. That chain is four to eight seconds and it feels broken in a live
meeting. Almost every step can happen earlier.

Everything in this project follows from that one idea: background loops keep
screen and audio context warm, so the button press only has to do the last mile.

## Status

**Working local demo of the silent-button path.** Screen loop, frame diff,
spoken answer pinned to a chosen output device, and an optional private overlay
with a live latency readout (off by default — voice only). The screen loop answers whatever the screen actually raises —
slide, chart, spreadsheet, error message, code — and falls back to summarising
it when nothing is being asked. The static sales table is now an extra line
appended when an on-screen figure matches a deal, not the answer itself.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m demo.check_setup     # pre-flight
.\.venv\Scripts\python.exe -m demo.main            # F9 / F10 to ask, F12 to quit
```

No API key yet? `--offline` runs the whole rig against a canned slide.
Full instructions, including the YouTube-video test setup, in
[docs/running-the-demo.md](docs/running-the-demo.md).

**The meeting audio loop is built.** It listens on a loopback device, keeps a
rolling 15-minute transcript, and pre-answers questions it overhears so the
button stays instant. One caveat that will cost you a meeting if you skip it:
Windows ships Stereo Mix *disabled but still enumerated*, so it looks connected
and captures nothing. `check_setup` plays a test tone to prove otherwise — see
[docs/running-the-demo.md](docs/running-the-demo.md#making-it-hear-the-meeting).

Not built: push-to-talk voice button, live CRM. See
[docs/demo-scope.md](docs/demo-scope.md).

## Shape of it

Three loops running independently:

| Part | Runs | Job |
|---|---|---|
| Meeting audio | every 7s, background | Rolling transcript from loopback **and** mic, and pre-answer any question it overhears |
| Screen | on press only | F10 grabs the region and sends the real pixels (~160ms) |
| Trigger | on button press | Transcribe the last few seconds, then answer |

Two buttons, deliberately different jobs — latencies measured, not projected:

| Key | Uses | Latency |
|---|---|---|
| **F9** | Answers the **last question asked**, transcript only, blind to the screen. No question? Catches you up on where the discussion stands | **~3.3s** after fresh speech, **0ms** if already pre-answered |
| **F10** | Said **and** shown. Sends the real screenshot, so it reads detail no summary kept | **~2.5s** |

F9's fast path does no model call at all: the audio loop answered the question in
the background the moment it heard it, so the press is a variable read. F10 is
the one you hold for detail — *"which account is in Discovery and how much"*
needs the pixels, not a one-line summary. With nothing heard yet, F10 is simply
"read my screen".

Running cost lands around **$1 to $2 per hour-long meeting**.

## Docs

| Doc | Contents |
|---|---|
| [architecture.md](docs/architecture.md) | The three loops, both trigger paths, latency budget |
| [components.md](docs/components.md) | Model and hardware choices, costs, what was rejected and why |
| [screen-capture.md](docs/screen-capture.md) | Capture timing, cropping, detail level, deck pre-indexing |
| [demo-scope.md](docs/demo-scope.md) | What to build for the demo, what to fake or skip |
| [running-the-demo.md](docs/running-the-demo.md) | Setup, hotkeys, YouTube test rig, known limits |
| [open-questions.md](docs/open-questions.md) | Unresolved items that block or shape the build |

## Before wiring this to anything external

Client-account meetings mean audio and screenshots leave the network. Confirm
what is allowed first — an internal monthly review is a different conversation
from a client call. Details in [open-questions.md](docs/open-questions.md).
