# Architecture

One always-on background loop — audio — plus two buttons. The screen is captured
on demand rather than polled, which is a reversal of the original design and is
explained below.

## 1. Screen — on demand, not a loop (`SCREEN_LOOP_ENABLED=0`)

F10 grabs the region at the moment you press and sends those pixels. A grab
costs ~160ms against a vision call measured in seconds, so taking it at press
time is effectively free.

The background loop it replaced polled every 2–3 seconds, frame-diffed, and kept
a structured description warm so the button needed no vision call. Two things
killed it:

- **F10 sends the image anyway.** Once the button transmits real pixels, a
  cached one-line summary is strictly worse — older, and missing every detail
  the summary did not think to record.
- **On a video call it burns money for nothing.** The frame diff fires on every
  head movement, so it re-describes a webcam on a loop: *"a webcam view shows
  part of a person beside white cabinets and window blinds"*, ~15 high-detail
  calls a minute, none of it ever read.

It still exists behind `SCREEN_LOOP_ENABLED=1` for the no-model-call fallback
answer, which is the only thing that needs a pre-computed description.

Capture and cropping details are in [screen-capture.md](screen-capture.md).

## 2. Meeting audio (background capture, no background calls)

- Capture **two** sources at once: system audio via loopback (`Them`) and your
  microphone (`You`), tagged by speaker
- Transcribe **nothing** in the background — audio just accumulates in memory
- A press transcribes everything since the last press, in one pass

### Why the cadence is slow, not off and not fast

This started out chunking continuously, and every version of that was worse.
Measured against known ground truth, the same 20s of Vietnamese speech scored:

| | Accuracy |
|---|---|
| One pass | **0.992** |
| Split in two | 0.977 |
| Split in four | 0.844 |

Every boundary lands mid-word, and half a word transcribes as a *different*
word — in both chunks. That is what turned "cái ví dụ HTML" into "cái ví TML"
and "cái thang" into "cái thằng", and no amount of pause detection fixed it.

But transcribing *only* on press was the other extreme, and it was slow: a whole
meeting piles up behind one button, and real presses measured 7-10s.

A ~40s sweep sits between them. Boundary cost is paid per boundary, so making
them rare costs almost nothing, while the press only transcribes what arrived
since the last sweep. The transcript also fills in during the meeting rather
than only when asked.

Press and sweep drain the same buffer under one lock, so an utterance is never
split between them. A 45s cap bounds the buffer.

### The gates

**Silence.** A buffer under an RMS floor is dropped without a call. Transcription
models invent text out of silence.

**Self.** While the bot is speaking, capture on *every* source is discarded.
Loopback would hear the answer directly and the mic would hear it out of the
speakers, so without this the bot transcribes itself and answers itself.

**Why both sources.** Loopback alone hears the meeting talk *at* you and never
hears you answer, clarify or commit — half the conversation. They also fail
independently: a machine where Stereo Mix is disabled still has a working mic,
so one dead source degrades the transcript instead of killing it.

On speakers rather than headphones, the mic also picks up the remote voices, so
remote speech can land twice — once as `Them`, once as `You`. Headphones remove
it.

## 3. Trigger path (on button)

Two buttons. The split is by *what the answer is grounded in*, because that is
what you actually know at press time: whether the question was spoken or is
sitting on the screen.

### F9 — answer the last question

Transcribe everything since the last press, then answer from the transcript.
Deliberately blind to the screen — feeding it screen context made it answer
questions nobody asked.

**A question gets answered and nothing else** — no scene-setting, no recap. With
no question it catches you up instead: what is being discussed and where it
stands, leading with anything the room is waiting on you for.

Two things stop it answering the wrong question:

- **`BACKGROUND` / `JUST SAID`.** One undifferentiated blob makes every line look
  equally important, and the safest output from that is a recap.
- **`ALREADY ANSWERED`.** Delivered answers are carried forward and named as
  settled. That is what lets the focus window stay wide enough for a follow-up
  to make sense — "and C and D?" is meaningless without the "A and B" before
  it — without the reply folding the old questions back in.

An earlier version pre-answered in the background: any chunk that looked like a
question got an answer composed ready for an instant press. It was removed. The
press transcribes fresh audio, and fresh speech invalidates a parked answer by
definition, so in a live meeting the fast path almost never fired — while a
model call was spent on every question detected, most never served.

### F10 — what was said and what is shown

Sends the **actual screenshot** with the transcript. This is the only path where
raw pixels leave at press time; the screen loop's own calls happen in the
background on its own schedule.

Worth the round trip because the screen loop's summary is a sentence written for
a different purpose — anything it did not think worth recording is simply gone.
*"Which account is in Discovery and how much"* cannot be answered from
`headline / summary / numbers`; it needs the image. *Measured: ~2.5s.*

With no transcript yet, F10 degrades to "read my screen", which is why two
buttons cover the whole space and a third was unnecessary.

### The floor

If the audio loop is off or offline, F10 falls back to the original silent path:
precomputed screen state plus the sales-table join, no model call at press time.
*Measured: under 1ms.*

### Voice button — arbitrary questions

1. Button down: start mic capture, fire retrieval in parallel
2. Button up: transcribe the clip (5 to 10 seconds of audio)
3. Assemble question + screen state + rolling transcript + retrieved docs
4. Stream the answer to the overlay

**Target: 1 to 1.5 seconds to first text.**

## Latency budget

The whole design exists to keep the post-button chain short. What each loop
buys you:

| Work | Naive timing | Here |
|---|---|---|
| Screenshot + vision analysis | 2–4s after press | Already in memory (screen loop) |
| Meeting context | Not available | Already in memory (audio loop) |
| Retrieval | 0.5–1s after press | Fired in parallel at button *down* |
| Question transcription | 1–2s | Only path that can't be precomputed |
| Answer generation | 1–3s | Streamed, so first text arrives early |

The silent path removes transcription and generation entirely, which is why it
lands under half a second and why it's the demo.
