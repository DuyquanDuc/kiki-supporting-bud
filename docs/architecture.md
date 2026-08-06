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

## 2. Meeting audio loop (background)

- Capture **two** sources at once: system audio via loopback (`Them`) and your
  microphone (`You`), merged into one speaker-tagged transcript
- Chunk to transcription every 20 seconds
- Maintain a rolling 15-minute transcript buffer
- **Pre-answer**: when a chunk contains a question, compose the answer straight
  away from transcript + current screen state and park it for the button

This is what lets the bot answer *"what did she just ask about the timeline."*

The pre-answer step is the same trade the screen loop makes. Without it, a
spoken question would mean transcribe-then-reason *after* the press — the four-
to-eight-second chain this project exists to avoid. With it, the press is a
variable read: **measured at 0ms** on a question the loop had already heard.

Question detection before that call is a plain regex, not a model — a chunk only
earns a call if it looks like someone asked something. It covers English,
Vietnamese and Japanese markers, because a transcript of speech frequently
contains no `?` at all. A miss is not fatal; the button falls back to answering
live off the transcript, which measured **~2s**.

Two gates keep it cheap and honest. Chunks under an RMS floor are dropped
without a call — meetings are mostly not talking, and transcription models
invent text out of silence. And while the bot is speaking, capture on *every*
source is discarded: loopback would hear the answer directly and the mic would
hear it out of the speakers, so without that gate the bot transcribes its own
answer and starts answering itself.

**Why both sources.** Loopback alone hears the meeting talk *at* you and never
hears you answer, clarify, or commit — half the conversation the bot exists to
support you through. They also fail independently: a machine where Stereo Mix is
disabled still has a working mic, so one dead source degrades the transcript
instead of killing it. Each source runs its own capture thread for that reason.

One caveat that cannot be fully solved here: on speakers rather than headphones,
the mic also picks up the remote voices, so remote speech can land in the
transcript twice — once as `Them`, once as `You`. Headphones remove it.

## 3. Trigger path (on button)

Two buttons, different jobs.

Two buttons. The split is by *what the answer is grounded in*, because that is
the thing the user actually knows at press time — they know whether the question
was spoken or is sitting on the slide.

### F9 — answer the last question

Transcript only, deliberately blind to the screen. Feeding it screen context
made it answer questions nobody asked.

**A question gets answered and nothing else** — no scene-setting, no recap.
With no question it catches you up instead: what is being discussed and where it
stands, leading with anything the room is waiting on you for.

That fallback was removed once and then restored, which is worth explaining. In
the first version it fired constantly, because summarising was what the model
did whenever it could not locate the question — and it usually could not, since
the question was never passed explicitly and the transcript arrived as one flat
blob. The summary was a symptom of the broken question path, not a feature.
With the question handed over directly and the transcript split into
`BACKGROUND` / `JUST SAID`, it only fires when there genuinely is no question.

1. **Flush.** Transcribe the last 14 seconds as one clip, right now.
2. **If the flush found nothing new**, serve the parked pre-answer. *0ms.*
3. **Otherwise**, answer live off transcript + flush. *Measured: ~3.3s.*

### Why the flush exists

The pre-answer path alone produced the project's worst failure: the interviewer
asks a question, you press, and the bot confidently answers the *previous* topic.

Chunks fill on a cadence. At the instant you press, the question you just heard
is raw samples in a half-full buffer — no background work can reach it. Measured
on a real mock interview: question spoken ~08:31:38, transcribed 08:31:46,
pre-answered 08:31:47. Presses at :39 and :45 both answered the topic before it.

So the press transcribes the last 14 seconds itself. Two details matter:

- **It re-transcribes the whole rolling window, not just the unsent remainder.**
  Boundaries fall mid-sentence, and short fragments do not merely lose
  accuracy — they hallucinate. A clean recording of "what are the differences
  between heap and stack memory" came back as *"Step aside, the hood's here to
  finish this"* and *"Хит-парад"* when split. One unbroken clip transcribes
  correctly.
- **The result is held apart from the transcript.** It overlaps by design;
  appending it would double every line in two slightly different renderings.
  It becomes the `JUST SAID` section, and the transcript window it covers is
  dropped from `BACKGROUND`.

This is a real departure from "do the work before the button" — the last few
seconds of audio cannot be pre-computed, because they had not happened yet. It
buys correctness for ~3.3s, against 0ms for an answer to the wrong question.

Three things decide whether the answer is any use:

- **The detected question is passed to the model explicitly.** The matcher
  already knows which words were the question; making the model re-find them in
  three minutes of transcript is how you get a summary instead of an answer.
- **The transcript is split into `BACKGROUND` and `JUST SAID`.** One
  undifferentiated blob makes every line look equally important, and the safest
  output from that is a recap.
- **Chunks are 7 seconds, not 20.** This is the single biggest lever on whether
  the button is useful at all. At 20s you hear a question, press, and the words
  are still sitting in a half-full buffer — so the bot answers something older.
  Short chunks cost more transcription calls, which are cheap.

A parked answer is discarded if anyone has spoken since it was composed. That
sounds like it would kill the fast path, but it does the opposite: after a
question aimed at you the room goes quiet waiting, so nothing newer arrives and
the 0ms path fires exactly when it matters.

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
