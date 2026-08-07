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
| **F9** | What was **said** — transcript only, blind to the screen |
| **F10** | What was said **and** what's shown — sends the real screenshot |
| **F8** | Reminder to re-pick the region |
| **F12** | Quit |

### F9 — answer the last question

Transcript only. It deliberately cannot see your screen, so it answers what was
asked rather than describing a slide nobody mentioned.

A question gets answered and nothing else — no preamble, no recap. If nobody has
asked anything, it catches you up instead: what is being discussed and where it
stands, leading with whatever the room is waiting on you for.

Pressing transcribes the last 14 seconds first, so a question you *just* heard is
included rather than waiting for the chunk cadence to catch up.

| Case | Latency |
|---|---|
| Someone just spoke — flush finds fresh speech | **~3.3s** — transcribe + answer |
| Quiet since the question, already pre-answered | **0ms** — no call at all |
| Nothing heard yet | instant "nothing transcribed yet" |

Press right after the question ends, not while it is still being asked — words
that have not been spoken cannot be transcribed.

### Buy time on purpose: ask them to repeat it

*"Sorry, could you say that again?"* is a normal thing to say in a meeting, and
it is the best move available here. The seconds it buys are exactly what the
pre-answer needs, so by the time they finish repeating, the answer is parked.

Press when they finish and you get it at **0ms** — the console shows
`button -> 0ms (repeat)`.

This works because a repeat would otherwise ruin it. Fresh speech normally
discards the parked answer as stale, so without special handling the repeat
would trigger a full recompute and the tactic would *cost* time. The press-time
transcription is compared against the question already answered, and a close
match serves the parked answer instead.

Matching is on character trigrams rather than words, so it survives rephrasing
and works in Japanese, which has no spaces to split on. Measured on real
rewordings across all three languages: repeats score 0.67–0.89, unrelated
questions 0.00–0.10, against a 0.45 threshold — a wide gap, so it does not fire
when they move on to something new.

### F10 — said + shown

Sends the actual screenshot alongside the transcript. Costs a vision round trip,
**~2.5s**.

**The screen usually *is* the question.** A coding exercise, a failing test, a
stack trace, a diff waiting on review — those are asks, not scenery, and
describing them back is the one thing you cannot use, because you are looking at
them. F10 solves what is on screen:

```
[08:41:02] button -> 2600ms (full)
           | Use a write pointer for nonzeros, then fill the rest with zeros.
           | ---
           | public static int[] moveZeros(int[] nums) {
           |     int write = 0;
           |     for (int num : nums) if (num != 0) nums[write++] = num;
           |     while (write < nums.length) nums[write++] = 0;
           |     return nums;
           | }
           | }
```

It matches the class and method signature already on screen rather than
inventing its own.

Priority when the two disagree:

1. Someone asked out loud → answer that, using the screen to ground it
2. Nothing said, screen poses a problem → solve it
3. Screen poses nothing → say what it shows and the point it makes

**One private window, opened once, holding the running history.** Not a popup per
press. It shows what was heard and what was answered, colour-coded by speaker,
with code set apart and monospaced. Scroll back through it; **F7** hides and
shows it.

```
Them: So, next question. Can you move all zeros in the array to the right?
You: Sure, give me a second.

> Use a write pointer for nonzeros, then fill the rest with zeros.
    public static int[] moveZeros(int[] nums) {
        int write = 0;
        ...
  2600ms · full
```

**Read from this window, not the terminal.** It is excluded from screen capture —
measured at `760x460` with **0 pixels** visible to a screenshot. VS Code's
terminal and Windows Terminal own their own windows, so neither can be hidden;
anything you read there is in the screen share.

It never takes focus, so scrolling it cannot swallow a keystroke meant for the
meeting. It follows the newest line only when you are already at the bottom —
scroll back to read something and it leaves you there.

`OVERLAY_MODE` in `.env`:

| Value | Behaviour |
|---|---|
| `history` *(default)* | one window with the running log |
| `read` | a card, only when the answer has something to look at |
| `always` | a card on every answer |
| `off` | nothing; voice only |

**The console is the transcript.** Every answer is printed under its timing
line, interleaved with the `heard [...]` lines, so that window is a running
record of the meeting and what was answered. Speech is gone the moment it plays;
keep the console somewhere you can glance at.

### Keeping it out of your screen share

The design is a private assist — voice in your ear, no bot in the call, nothing
rendered for anyone else. That collapses the moment you share your screen with
the console sitting there full of answers.

On by default (`HIDE_FROM_CAPTURE=1`). Windows' `SetWindowDisplayAffinity` with
`WDA_EXCLUDEFROMCAPTURE` makes a window completely normal on your monitor and
*absent* from anything capturing the screen — not blacked out, simply not
present. Verify it rather than trusting it:

```powershell
.\.venv\Scripts\python.exe -m demo.check_setup --hide
```

It puts a marker window on screen, screenshots that region and counts its
pixels: `64800 pixels -> 0` means genuinely invisible.

**The console is the weak point.** Windows Terminal draws in its own window,
which this process does not own, so the console cannot be hidden from inside —
and the console is where answers print. Classic `conhost` can be hidden. The
check reports which you have, and the app warns at startup:

```
screen-share: running under Windows Terminal ...
screen-share: WARNING — answers print here and WILL be shared
```

Either run under conhost, or keep that window off the screen you share (another
monitor, or share a single application window rather than the whole desktop).

**It hides windows, nothing else.** Spoken answers still reach the call if
`TTS_DEVICE` points at your speakers, and it cannot help against a phone camera
pointed at your screen.

### Answers you have to read, not hear

Code cannot be listened to. When an answer carries something you must look at —
code, a command, an exact identifier — it comes back in two parts split by a
`---` line. Only the part above is spoken; the whole thing is printed:

```
[08:32:56] button -> 2100ms (live transcript)
           | Use a HashMap and merge counts as you go.
           | ---
           | Map<String, Integer> counts = new HashMap<>();
           | counts.merge(word, 1, Integer::sum);
```

So the speech stays short while the console holds the real thing. Ordinary
answers get no split — a number, a name or a yes/no is just spoken and printed
as one line.

The console tells you which fired: `button -> 0ms (overheard)`,
`(live transcript)`, `(full)`, or `(screen)`.

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
