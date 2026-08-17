# Running the demo

Windows, Python 3.11 or 3.12. Tested on 3.11.1 and 3.12.0.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env      # then paste the key into .env
```

### Every new machine needs this again

Two things live outside the repo and do not travel with a clone:

1. **`.env`** — gitignored, so the API key never reaches GitHub. Copy the
   example and paste the key in.
2. **The loopback recording device** — a Windows sound setting, not a file.
   Enable Stereo Mix (below) or the bot hears only you, never the meeting.

A giveaway that Stereo Mix is still disabled: `check_setup` finds exactly **one**
match, on WDM-KS. Enabled, it appears four times — MME, DirectSound, WASAPI and
WDM-KS — because Windows only exposes the other host APIs once the device is on:

```
[09:43:29] audio [Them] failed: no input device would open:
  [13] Windows WDM-KS: Error opening InputStream: Invalid device [PaErrorCode -9996]
```

The microphone needs no setup — it falls back across host APIs on its own, which
is why `[You]` still starts after WASAPI fails to create a capture pin.

Check everything before you need it to work:

```powershell
.\.venv\Scripts\python.exe -m demo.check_setup
```

It verifies imports, whether your key can actually see the configured model ids,
which output device speech will land on, whether the input device can actually
hear anything, and whether a capture region is saved.

## Giving it documents to work from

Drop files into `demo/data/docs` before a meeting — a job spec, your CV, an
architecture note, the agenda, last quarter's numbers. The bot searches them
when answering.

**Only `.txt` and `.md`.** Anything else is named in the log and skipped, so a
dropped-in PDF tells you rather than silently never appearing in an answer.

The difference on real questions:

| Question | Without documents | With |
|---|---|---|
| Does a rollback restore the database? | *"Not necessarily — check the rollback plan on screen."* | *"No. Rollback only swaps traffic back; schema changes stay forward-only."* |
| Who do I escalate to? | *"I don't have the owner's name."* | *"Huong — escalate the Atlas migration to her, not the platform team."* |

**No retrieval, no index, no added latency.** The full text is handed to the
model with every answer. An earlier version embedded and searched chunks per
press; for the handful of small documents one person brings to a meeting that
bought nothing and cost an embedding round trip on every press, plus a relevance
threshold that could silently drop the one passage that mattered.

Documents are re-read on **every press**, so editing a file — or dropping a new
one in — takes effect on the next button press with no restart.

**Keep it small.** Everything goes with every answer, which is right for a few
pages and wrong for a library. Past ~20,000 characters (`DOCS_MAX_CHARS`) the
app warns that it is costing real tokens on every reply.

**Your documents stay on your machine.** The folder is gitignored.

## Telling it about you

`demo/data/profile.md` is free text handed to the bot before every answer. Fill
in what you want, delete the rest — it is re-read on each press, so edits apply
mid-meeting with no restart. An untouched file costs nothing: HTML comments and
empty `- Label:` lines are stripped, and headings with nothing under them are
dropped.

The highest-value section is **Terms and names**. Project names, acronyms, and
who owns what are exactly what the bot cannot infer and most often gets wrong —
it stops "Kirin" being transcribed as an ordinary word, and turns "unclear from
the transcript" into "Trang owns that".

Second is **What meetings I use this in**. The bot answers a mock interview very
differently from a sprint review.

Notes are context, not commands: if a note conflicts with what was actually said
in the meeting, what was said wins.

## Working in more than one language

Answers come back in the language the question was asked in — Vietnamese,
Japanese or English — decided per question, so a mixed meeting works without
switching anything.

Technical terms stay in whatever form they were spoken. Vietnamese and Japanese
engineers say "deploy", "commit", "stack", "heap" in English mid-sentence, and
translating those would make you stop and decode a word you already knew:

> *Stack giữ biến local và lời gọi hàm. Heap chứa object, và heap mới bị garbage collect.*

Because you only listen and never repeat these, politeness forms are dropped as
wasted breath: no Vietnamese pronouns or `dạ`/`ạ` (they carry status the bot has
no business guessing), and Japanese stays compact rather than reaching for 敬語.

## Speaking without the API (local TTS)

**This is the single biggest speed setting in the app, and it is a Windows
install detail the repo cannot carry.** The same code is fast on one laptop and
~1750ms slower on another with nothing on screen to say why.

Spoken answers go through Windows' own voices when one matches the answer's
language. Measured on this project: the whole clip synthesises in 56-264ms
against ~1800ms to the paid API's *first byte*. It is not a quality compromise
on the measure that matters — round-tripped back through transcription the
local voices scored 1.00 for English and Japanese against 0.99 and 0.94 for the
paid model. They sound more robotic, but this is a cue you hear once and never
repeat, so intelligibility is the whole job.

```powershell
.\.venv\Scripts\python.exe -m pip install pywin32   # required, or it all goes to the API
.\.venv\Scripts\python.exe -m demo.check_setup
```

```
[  ok  ] local voices for: en, ja
[  ok  ]    en: local, 264ms for the whole clip
[  ok  ]    ja: local, 216ms for the whole clip
[ warn ]    vi: no voice -> falls back to the API
```

Each language falls back on its own. A missing Vietnamese voice costs you
~1750ms on Vietnamese answers only; English and Japanese stay fast. Nothing
breaks — `LOCAL_TTS=1` is safe to leave on across every machine, because a
missing voice, a missing pywin32 or a synthesis error all fall through to the
API automatically.

### The catch: Windows has two voice registries

**Installing a voice through Settings does not necessarily make it visible to
this app.** Windows keeps SAPI5 voices and "OneCore" voices in separate places,
`Settings > Speech` installs into OneCore, and the app reads SAPI5. So a voice
you just installed can be entirely invisible while Windows itself happily uses
it.

Check both:

```powershell
Get-ChildItem "HKLM:\SOFTWARE\Microsoft\Speech\Voices\Tokens" | % PSChildName
Get-ChildItem "HKLM:\SOFTWARE\Microsoft\Speech_OneCore\Voices\Tokens" | % PSChildName
```

On the development machine those return:

```
SAPI5    TTS_MS_EN-US_DAVID_11.0, TTS_MS_EN-US_ZIRA_11.0, TTS_MS_JA-JP_HARUKA_11.0
OneCore  ...DavidM, MarkM, ZiraM, AyumiM, HarukaM, IchiroM, SayakaM, viVN_An
```

A Vietnamese voice (`MSTTS_V110_viVN_An`) and three extra Japanese voices are
installed and unreachable — which is exactly why `check_setup` reports
`vi: no voice` on a machine that has one.

The fix is to copy the voice's token key from the OneCore hive to the SAPI5
hive with `regedit` (run as Administrator, export a backup first). It is a
registry edit on your own machine, so weigh it accordingly; the app works
without it and simply pays the API round trip on that language.

To install voices in the first place: `Settings > Time & Language > Speech >
Manage voices > Add voices`. Installing a full **language pack** rather than
just the speech voice is more likely to register on both hives.

### Speech-to-**text** is not local

Transcription always goes to the network — Groq first, OpenAI as the fallback.
There is no local STT in this project and no setting to enable one.

That is a deliberate trade, not an oversight. Groq's `whisper-large-v3-turbo`
returns in ~650-900ms for a 11-16s clip (see `demo.bench`), and matching that
on CPU means running whisper locally at several times realtime, which a laptop
without a discrete GPU will not do at large-v3 quality. Smaller local models are
fast enough but lose exactly what this app needs most: Japanese accuracy and
proper nouns.

If you want to try anyway, the shape of the change is small — `_transcribe()`
in `demo/audio_loop.py` is the only place audio meets a model, and it already
has a two-provider fallback chain to extend. `faster-whisper` (CTranslate2) is
the usual choice, and with a CUDA GPU `large-v3-turbo` is genuinely competitive.
Benchmark it against the current route with `python -m demo.bench` before
switching: the transcribe leg is ~40% of a press, so a regression there is felt
on every button.

## Making it hear the meeting

The bot listens on **two** sources and merges them into one speaker-tagged
transcript:

| Tag | Source | Hears |
|---|---|---|
| `Them` | loopback (Stereo Mix) | everyone else in the call |
| `You` | your microphone | you |

They are checked separately by `check_setup` and fail independently — a dead
loopback still leaves you with a working mic, and vice versa. Set `MIC_ENABLED=0`
to drop the mic half.

On **speakers** rather than headphones, your mic also picks up the remote
voices, so remote speech can appear twice — once as `Them`, once as `You`.
Headphones remove it.

This is the step most likely to bite you, so do it before you need it.

The bot listens on a **loopback** input — a device that captures the system
output mix, i.e. the voices coming out of your speakers. On Realtek hardware
that device is **Stereo Mix**, and Windows ships it disabled. Worse, PortAudio
still *enumerates* it while disabled, so name-matching alone reports success and
the bot then silently transcribes nothing forever.

The same loopback appears once per host API, and they fail independently, so
`check_setup` probes each in turn with a test tone and reports which one is
actually usable:

```
[  ok  ] 4 device(s) match 'Stereo Mix', best API first:
         [12] Windows WASAPI       ステレオ ミキサー (Realtek(R) Audio)
         [13] Windows WDM-KS       Stereo Mix (Realtek HD Audio Stereo input)
[  ok  ] [12] Windows WASAPI: hears audio (silent 0.000082 -> tone 0.049)
```

Three distinct failure modes, and they need different fixes:

| What you see | Means |
|---|---|
| `no input device matches` | Name doesn't match. Windows localises it — a Japanese system has ステレオ ミキサー, not "Stereo Mix". Aliases are handled, but check `AUDIO_DEVICE` |
| `opens but hears NOTHING` | Enumerated and disabled. Enable it (below) |
| `will not open` | Enabled but claimed or locked. Close other audio apps, or unplug/replug the headset to force re-enumeration |

To enable it: `Win+R` → `mmsys.cpl` → **Recording** tab → right-click empty
space → **Show Disabled Devices** → right-click **Stereo Mix** → **Enable**.
Re-run `check_setup`. If it stays dead, install
[VB-Audio Cable](https://vb-audio.com/Cable/) and set `AUDIO_DEVICE=CABLE Output`.

### Headphones, Bluetooth, and switching mid-meeting

WASAPI loopback taps ONE output endpoint. Connecting Bluetooth earphones or
plugging in a jack moves playback to a different endpoint, and a tap on the old
one keeps returning digital silence for ever, with no error — the bot simply
stops hearing the meeting while you hear it perfectly.

The loopback now re-checks the default output every few seconds and follows it:

```
[10:41:02] audio [Them] output moved to Headphones (soundcore P40i Stereo) — following it
```

Two related lines worth recognising:

```
audio [Them] heard nothing but digital silence for 25s — reopening the loopback (reopen #1)
press [Them]: dropped 20.0s, no speech in it (peak 0.00000 ...)
```

`peak 0.00000` is EXACT zeros — a stalled tap, not a quiet room, because a
working loopback on a silent room still carries a noise floor. One reopen is
routine. Repeated reopens with no audio arriving means playback is going
somewhere the tap cannot see.

`OUTPUT_RECHECK_SECONDS=0` stops it following; `LOOPBACK_STALL_SECONDS=0`
disables the reopen.

### The live meter

When the device opens and streams but still transcribes nothing, the tone probe
is not enough — you need to watch the level while *your* audio plays:

```powershell
.\.venv\Scripts\python.exe -m demo.check_setup --meter
```

```
  0.000336 [###############......|..................] dropped as silence
  0.041220 [###########################|............] TRANSCRIBED
```

The `|` is the silence floor. If the bar sits left of it while you can plainly
hear the sound, the device is open but not tapping your output. Nearly always
one of:

1. **Stereo Mix's own level slider is down.** `mmsys.cpl` → Recording →
   Stereo Mix → **Properties** → **Levels** → push to 100. This is the top
   cause of "enabled, streaming, and silent" — the stream is real, the gain
   is zero.
2. **The audio is going somewhere else.** Settings → Sound → **Volume mixer**,
   check the app is on the same output device Stereo Mix taps.
3. **The endpoint changed** since it last worked — headphones plugged in,
   default output switched.

Do not "fix" this by lowering `AUDIO_SILENCE_RMS`. A signal that faint
transcribes into garbage; the gain is the thing to fix.

**Host API matters.** The bot prefers WASAPI and treats WDM-KS as a last resort:
WDM-KS is exclusive-mode, so it locks the endpoint, and a process killed while
holding it can leave the device unopenable until the driver resets. If Stereo Mix
suddenly stops opening after a crash, that is usually why.

Caveat worth knowing: Stereo Mix taps the **speaker** mix. If you route the call
to earbuds only, it may capture nothing even when enabled. Test the combination
you actually plan to use.

## Run it

```powershell
.\.venv\Scripts\python.exe -m demo.main
```

First run asks you to drag a box around the shared-content area. That box is
saved to `demo/data/region.json` and reused. `--pick-region` redraws it.

| Key | Does |
|---|---|
| **F9** | The last question, answered as a cue — transcript only, blind to the screen |
| **F11** | The same question, answered **in depth** — one spoken line, detail to read |
| **F10** | What was said **and** what's shown — sends the real screenshot |
| **F8** | **Where things stand** — the meeting so far, in points |
| **F4** | Type a **standing request** — changes what the buttons do |
| | *(F11 also draws, on its own judgement — see below)* |
| **F7** | Hide/show the history window |
| **F12** | Quit (or Ctrl+C in the terminal) |

F11 and F8 both speak one line and print the rest, because 150 words at 1.75x is
half a minute of talking over a live meeting. Read the detail in the history
window; the spoken line always stands alone if you do not.

### When the answer is a picture

Some questions have a shape for an answer — *"can you draw a simple load
balancer architecture"*, *"sketch how the retry works"*. **F11 decides on its
own** when a drawing is what was asked for, generates one with `gpt-image-2`,
and shows it **inside the history window** — the only surface excluded from
screen capture, so it cannot leak into a share.

It is always asynchronous. Drawing takes 20-40s, so the spoken cue and the
written answer arrive at their usual speed and the picture follows a while
later. A press never waits for it, and a failure (no key, a timeout, a refusal)
is one line in the log rather than a lost answer.

It draws only when asked. Measured on four cases: "draw a load balancer
architecture" and its Japanese equivalent both produced an image; "what does
the GIL mean for CPU-bound work" and a question about prices and deadlines both
correctly produced none.

`IMAGE_ENABLED=0` turns it off. Images land in `demo/data/images`, gitignored.

### Asking for something other than an answer

Not every meeting wants a question answered. **F4** puts the caret in the `ask:`
box at the bottom of the history window; type an instruction, press **Enter**,
and it applies to every press until you clear it with **Escape**.

| Typed | What the buttons do then |
|---|---|
| *(empty)* | Normal — answer the last question |
| `Translate what they said into English. Do not answer it.` | Translates instead of answering |
| `Just give me the number, no explanation.` | 340 units at 98,000 less 12% came back as `29,321,600 yen` |
| `Reply in Vietnamese regardless of the question language.` | Overrides the per-question language rule |

It overrides the default behaviour but **not** the delivery rules — still one
spoken line, still `---` before anything meant to be read. It applies to F9, F10
and F11.

Pressing F4 is the one moment this window takes keyboard focus. It normally
carries `WS_EX_NOACTIVATE` so clicking it cannot swallow a keystroke during a
meeting — but a window that cannot be activated cannot receive typing either, so
the flag is dropped while the box has the caret and restored on Enter or Escape.
The `ask:` label stays highlighted while a request is active, because a
forgotten "translate everything" is a confusing five minutes.

## Live transcription (optional)

`TRANSCRIBE_MODE=stream` holds a websocket open and receives the transcript as
it is spoken, instead of accumulating audio and transcribing a clip per press.

```powershell
$env:TRANSCRIBE_MODE = "stream"; python -m demo.main
```

```
transcribing with LIVE stream gpt-live-transcribe over websocket (~$0.017/min — TRANSCRIBE_MODE=batch to go back)
stream [Them] connected (gpt-live-transcribe)
```

Measured against the batch path on identical fixtures:

| | batch | stream |
|---|---|---|
| silence | invents text | **returns nothing** |
| accuracy en / ja | 0.77 / 0.77 | **0.91 / 0.87** |
| transcript ready | 1-3s after the press | **~700ms after speech ends** |

The first row is the reason it exists. Every silence gate and echo filter in
`audio_loop.py` is there to stop the batch transcriber inventing text, and none
of them is needed in this mode — mic noise, room tone and digital silence all
returned nothing.

**It costs about 20x more.** $0.017/min against $0.003/min, and it pays for
wall-clock time where batch only pays for audio that passed a gate: roughly
$1/hour per source against a few cents. Sending pauses after
`STREAM_IDLE_PAUSE` seconds of quiet, so an empty room is nearly free.

**Rolling back is one variable.** `TRANSCRIBE_MODE=batch` (the default) and
nothing in `stream_stt.py` runs at all. The tag `before-streaming-stt` marks the
commit before any of it existed.

## Measuring the speed

```
python -m demo.bench                  # the standard suite, ~3 minutes
python -m demo.bench --runs 5 --play  # more samples, and time the playback
python -m demo.bench --lang ja --skip-vision --runs 2   # quick check
python -m demo.bench --json out.json  # save, to diff against a later run
```

Fixed fixtures in English and Japanese, so runs weeks apart are comparable, and
the first call to every provider is discarded — cold TLS to Groq measured
1959ms against 617ms warm on the identical question, which would otherwise look
like a slow model.

`docs/bench-baseline.json` is a saved run to compare against. It records the
machine and the models alongside the timings, because a latency number without
them cannot be compared to anything.

**A change under ~150ms is not a result.** The run-to-run range is wider than
that; treat anything smaller as noise unless it survives `--runs 10`.

The capture region is chosen at startup — restart with `--pick-region` to
change it.

### F9 — answer the last question

Transcript only. It deliberately cannot see your screen, so it answers what was
asked rather than describing a slide nobody mentioned.

A question gets answered and nothing else — no preamble, no recap. If nobody has
asked anything, it catches you up instead: what is being discussed and where it
stands, leading with whatever the room is waiting on you for.

Pressing transcribes everything captured since your last press, in a single
pass. Nothing is transcribed in the background, so a question you *just* heard
is always included.

**Why one pass and not chunks.** Measured against known ground truth, the same
20s of speech scored **0.992** transcribed whole, **0.977** split in two, and
**0.844** split in four. Every boundary lands mid-word and both halves come back
wrong — that is what turned "cái ví dụ HTML" into "cái ví TML". Latency barely
grows with length (16s → 1.0s, 64s → 2.5s, 96s → 3.9s), so the long pass is both
more accurate and affordable, at one call per press instead of hundreds per
meeting.

| Case | Latency |
|---|---|
| Someone has spoken since your last press | **2.5–3.8s** — transcribe + answer |
| Nothing heard yet | instant "nothing transcribed yet" |

Press right after the question ends, not while it is still being asked — words
that have not been spoken cannot be transcribed.

### Buying time still helps

*"Sorry, could you say that again?"* is worth asking — it gives you thinking
time and gives the bot a cleaner second recording of the question. It no longer
produces an instant answer, though: with pre-answering removed there is nothing
parked to serve, so the press still costs its usual few seconds.

### Answers you have to read, not hear

Code cannot be listened to. When an answer carries something you must look at —
code, a command, an exact identifier — it comes back in two parts split by a
`---` line. Only the part above is spoken; the whole thing is printed:

```
[08:32:56] button -> 2800ms (live transcript)
           | Use a HashMap and merge counts as you go.
           | ---
           | Map<String, Integer> counts = new HashMap<>();
           | counts.merge(word, 1, Integer::sum);
```

So the speech stays short while the console holds the real thing. Ordinary
answers get no split — a number, a name or a yes/no is just spoken and printed
as one line.

The console tells you which path ran: `button -> 2800ms (live transcript)`,
`(full)`, or `(screen)`.

A USB footswitch that emulates a keypress maps straight onto either — change
`HOTKEY_AUDIO` / `HOTKEY_FULL` in `demo/config.py` to whatever key yours sends.

## Testing with a YouTube video

1. Plug in your earphones **before** starting, so the device shows up.
2. Run `check_setup`, find your earphones in the device list, and put a
   distinctive fragment of the name in `.env` as `TTS_DEVICE=` — e.g.
   `TTS_DEVICE=WF-1000`. This is what guarantees spoken answers reach only you.
3. Start a video with numbers on screen — an earnings breakdown, a chart-heavy
   explainer, anything with figures.
4. Run the demo, drag the box around the video.
5. Watch the console. Every few seconds you'll see either nothing (frame diff
   suppressed it) or `screen updated in NNNms`.
6. Press F9.

To make the sales-table join fire, pause on a frame showing one of the amounts
in `demo/data/sales.csv` — `2.4M`, `412,000`, `5.4M` and so on. Anything else
falls back to describing the screen, which still works, just without the join.

## Before the key arrives

```powershell
.\.venv\Scripts\python.exe -m demo.main --offline
```

Canned slide, no API calls, no region needed. The hotkey, overlay, latency
readout and answer templating all run for real — enough to confirm the rig works
on your machine. Once you have a key, `--offline` still speaks, so you can test
earphone output without setting up a region.

## What to expect on latency

The button path itself measures **under a millisecond** — it is a dictionary
lookup against state the screen loop already computed. The overlay card appears
effectively instantly.

Spoken output is a different number. It needs a round trip to the TTS endpoint
before the first audio chunk arrives, realistically **0.3–1s**. Audio is streamed
as raw PCM so playback starts on the first chunk rather than after a whole file
downloads, but the round trip is unavoidable.

So: the card is instant, the voice has a beat of lag. Pressing F9 again cuts off
whatever is still speaking.

## Known limits

- **Single monitor.** The region picker reports coordinates relative to the
  primary display, so a box dragged onto a second monitor captures the wrong area.
- **F8 doesn't reselect in place.** It prints a reminder; restart with
  `--pick-region`. Rebuilding a fullscreen picker while the overlay owns the
  tkinter main loop is more surgery than the demo needs.
- **No push-to-talk voice button.** Not needed as much now — the audio loop
  already hears the question without you repeating it into a mic.
- **Loopback capture is hardware-dependent.** Stereo Mix is disabled by default
  on Windows and taps the speaker mix, so an earbuds-only routing may hear
  nothing. `check_setup`'s tone probe is the way to confirm.
- **Question detection is a regex.** English, Vietnamese and Japanese markers.
  A phrasing it misses costs you ~2s (live fallback) rather than an error.

## When it misbehaves

| Symptom | Cause |
|---|---|
| Buttons do nothing, or F9 opens a screenshot | Laptop F-row is in multimedia mode, so F9 sends `print_screen`, F10 sends `end`. **Hold Fn**, or enable Function Lock (often Fn+Esc). `check_setup --keys` confirms it |
| Pressed a button, heard nothing | `check_setup --say` — a `TTS_DEVICE` pointing at unplugged headphones plays to nowhere |
| `model 'x' NOT available` in check_setup | Model id in `.env` doesn't exist for your key — the check prints near matches |
| Answer is always "Warming up" | Screen loop hasn't completed a call yet, or it errored — check the console |
| Nothing in console for minutes | Frame diff sees a static screen. That's correct. Lower `DIFF_THRESHOLD` to be twitchier |
| Speech comes out of laptop speakers | `TTS_DEVICE` is blank or doesn't match — run check_setup for exact names |
| Numbers get misread | Set `VISION_DETAIL = "high"` in config, and crop the region tighter |
| `it hears NOTHING` in check_setup | Loopback device enumerated but disabled — enable Stereo Mix in `mmsys.cpl`, or use VB-Audio Cable |
| Console never prints `heard:` | Everything is under the silence floor. Lower `AUDIO_SILENCE_RMS` in config |
| It transcribes its own answers | The self-gate failed — check that `TTS_DEVICE` and `AUDIO_DEVICE` aren't the same loop |
| `mic is silent` in check_setup | Mic muted or blocked. `mmsys.cpl` → Recording → Properties → Levels, and Settings → Privacy → Microphone |
| Remote speech appears twice | You're on speakers, so the mic hears them too. Use headphones |
| Spoken answers are too slow | Raise `TTS_SPEED` in `.env` (0.25–4.0, default 1.5) |
| Answers arrive in the wrong language | Expected: it answers in the language of the room, following the transcript |
