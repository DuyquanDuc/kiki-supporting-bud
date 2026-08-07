# Meeting Support Bot

During a meeting, someone asks you something — or a coding problem is sitting on
the shared screen. You press a key. A few seconds later the answer is spoken
quietly into your earphones. No bot joins the call, nothing renders where a
screen share can see it.

## What it does

Two buttons. Both answer into your earphones; nothing renders where a screen
share can see it.

| Key | Uses | Latency |
|---|---|---|
| **F9** | The **last question asked**, from the transcript. Blind to the screen on purpose. No question? Catches you up on where the discussion stands | **2.5–3.8s** |
| **F10** | That, plus a screenshot taken as you press — so it reads the code, table or error you are looking at | **~2.5s** |

Answers are cues, not scripts: 15–25 words, front-loaded, in whichever of
Vietnamese, Japanese or English the question was asked. Code comes back in a
private window, not spoken.

## The principle, and how it was overturned

This began with one rule: **do the work before the button**. Screenshot,
transcribe and reason ahead of time, so the press only does the last mile.

Both halves of that turned out to be wrong, and the measurements are worth
keeping because they are counterintuitive:

**The screen loop** described the shared screen every few seconds so the button
needed no vision call. But F10 sends the real pixels anyway, which is fresher
and carries detail no summary kept — and on a video call the frame diff fired on
every head movement, burning ~15 high-detail calls a minute to re-describe a
webcam. Now the screen is grabbed on press, in ~160ms.

**Pre-answering** composed an answer for every overheard question, ready to serve
instantly. But any fresh speech invalidates a parked answer, so in a live meeting
it almost never fired — while spending a model call on questions nobody asked
about.

**Continuous chunking** was the last to go, and it was actively destructive.
Against known ground truth, the same 20s of speech scored **0.992** transcribed
in one pass, **0.977** split in two, **0.844** split in four. Every boundary
lands mid-word and both halves come back wrong: "cái ví dụ HTML" became "cái ví
TML". Latency barely grows with length, so there was nothing to buy.

What survived is the opposite design: **capture everything, compute nothing until
asked.** Audio accumulates in memory, a press transcribes it in a single pass and
answers. One call per press instead of hundreds per meeting, and better answers.

## Status

Working. Listens on loopback (`Them`) and your microphone (`You`), answers in
three languages, reads your own `.txt`/`.md` notes from `demo/data/docs`, and
keeps a private history window that is excluded from screen capture.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m demo.check_setup     # pre-flight
.\.venv\Scripts\python.exe -m demo.main            # F9 / F10 to ask, F12 to quit
```

One caveat that will cost you a meeting if you skip it: Windows ships Stereo Mix
*disabled but still enumerated*, so it looks connected and captures nothing.
`check_setup` plays a test tone to prove otherwise — see
[docs/running-the-demo.md](docs/running-the-demo.md#making-it-hear-the-meeting).

Running cost lands around **$1 to $2 per hour-long meeting**.

## Docs

| Doc | Contents |
|---|---|
| [architecture.md](docs/architecture.md) | Capture, both trigger paths, and why the original design was inverted |
| [components.md](docs/components.md) | Model and hardware choices, costs, what was rejected and why |
| [screen-capture.md](docs/screen-capture.md) | Capture timing, cropping, detail level, deck pre-indexing |
| [demo-scope.md](docs/demo-scope.md) | What to build for the demo, what to fake or skip |
| [running-the-demo.md](docs/running-the-demo.md) | Setup, hotkeys, YouTube test rig, known limits |
| [open-questions.md](docs/open-questions.md) | Unresolved items that block or shape the build |

## Before wiring this to anything external

Client-account meetings mean audio and screenshots leave the network. Confirm
what is allowed first — an internal monthly review is a different conversation
from a client call. Details in [open-questions.md](docs/open-questions.md).
