"""Settings for the local demo. Everything tunable lives here."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(__file__).resolve().parent / "data"
REGION_FILE = DATA_DIR / "region.json"
SALES_CSV = DATA_DIR / "sales.csv"
# Free-text notes about the user, injected into every answer prompt. Re-read on
# each press so edits apply without a restart.
PROFILE_FILE = DATA_DIR / "profile.md"
# Drop reference documents here before a meeting — job spec, CV, architecture
# note, agenda. Gitignored: these are yours and some of them will be sensitive.
DOCS_DIR = DATA_DIR / "docs"

load_dotenv(ROOT / ".env")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()

# Model ids come from the project spec (docs/components.md). They are not
# verified against the API here — if one is rejected, `python -m demo.check_setup`
# lists what the key can actually see.
VISION_MODEL = os.getenv("VISION_MODEL", "gpt-5.6-luna").strip()
TTS_MODEL = os.getenv("TTS_MODEL", "gpt-4o-mini-tts").strip()
TTS_VOICE = os.getenv("TTS_VOICE", "alloy").strip()
TRANSCRIBE_MODEL = os.getenv("TRANSCRIBE_MODEL", "gpt-4o-mini-transcribe").strip()
# Text-only reasoning over transcript + screen state. Same model as vision by
# default; there is no reason it has to be.
ANSWER_MODEL = os.getenv("ANSWER_MODEL", VISION_MODEL).strip()

# --- Screen loop -----------------------------------------------------------
# OFF by default. The background loop existed to keep a description of the screen
# warm so the button needed no vision call — but F10 now sends the real
# screenshot anyway, which is both fresher and reads detail no summary kept. All
# the loop added was a vision call every few seconds; on a video call the frame
# diff fires on every head movement and it burns high-detail calls re-describing
# a webcam ("a webcam view shows part of a person beside white cabinets").
# Turn it on only if you want the no-model-call fallback path back.
SCREEN_LOOP_ENABLED = os.getenv("SCREEN_LOOP_ENABLED", "0").strip() == "1"
# Slides change 20-40 times an hour, so polling is cheap and the diff gate
# means a static screen costs nothing at all.
SCREEN_POLL_SECONDS = 2.5
# Mean per-pixel difference (0-255) on a 160x90 grayscale thumbnail that counts
# as "the screen actually changed". Raise it if a video's noise keeps retriggering.
DIFF_THRESHOLD = 6.0
JPEG_QUALITY = 70
# "high" is needed for tables and small numbers; "low" is enough for headlines
# and costs far fewer tokens.
VISION_DETAIL = "high"

# --- Meeting audio loop ----------------------------------------------------
AUDIO_ENABLED = os.getenv("AUDIO_ENABLED", "1").strip() == "1"
# Substring match against an INPUT device name. "Stereo Mix" is the system
# output mix on Realtek hardware — what everyone else in the call is saying.
# sounddevice 0.5.x exposes no WASAPI loopback flag, so this is the route on
# Windows. Enable it in Sound > Recording if it does not show up.
AUDIO_DEVICE = os.getenv("AUDIO_DEVICE", "Stereo Mix").strip()
# Your microphone, captured alongside the loopback and merged into the same
# transcript tagged "You". Without it the bot hears the meeting talk *at* you and
# never hears you answer, which is half the conversation it is meant to help with.
# Blank means the system default recording device.
MIC_ENABLED = os.getenv("MIC_ENABLED", "1").strip() == "1"
MIC_DEVICE = os.getenv("MIC_DEVICE", "").strip()
# NOTHING is transcribed in the background. Audio accumulates and a button press
# transcribes the lot in one pass.
#
# Measured: the same 20s of speech scored 0.992 against ground truth as a single
# pass, 0.977 split in two, 0.844 split in four. Every chunk boundary lands
# mid-word and costs accuracy — "cái ví dụ HTML" became "cái ví TML". Latency
# scales sub-linearly (16s->1.0s, 64s->2.5s, 96s->3.9s), so one long pass is
# affordable, and it costs one call per press instead of hundreds per meeting.
#
# This caps how much audio a single press can transcribe — the oldest audio is
# dropped once the buffer is full.
#
# Kept short, and it is the main lever on press latency. At 120s a real session
# hit the cap repeatedly and presses took 7-10s: two minutes of speech to
# transcribe, then ~2000 words of it in the answer prompt. Worse, the whole two
# minutes arrives as ONE transcript entry stamped "now", so all of it counts as
# JUST SAID and the model has to hunt for the question inside a monologue.
#
# The question is almost always in the last few seconds. Anything older is
# already in the transcript from earlier presses, so shortening this loses
# nothing except re-transcribing speech nobody asked about.
AUDIO_BUFFER_MAX_SECONDS = 45.0
# Drain and transcribe in the background every this many seconds. 0 disables it.
#
# This is the middle ground between the two failed extremes. Chunking every 7s
# was fast at the button but wrong — 0.844 against ground truth, boundaries
# mid-word, "cái ví dụ HTML" as "cái ví TML". Transcribing only on press was
# accurate but slow, because a whole meeting's audio piles up and the press pays
# for all of it: measured 7-10s.
#
# At 40s the boundary cost is small (one pass 0.992, split in two 0.977 — the
# loss is per boundary, and this makes them rare) while the press only has to
# transcribe whatever has accumulated since the last sweep, usually well under
# 40s. It also restores a transcript that fills in as the meeting goes, instead
# of only when you ask.
BACKGROUND_TRANSCRIBE_SECONDS = 20.0
# Reasoning effort for the answer call. Measured 1621ms at "low" against 1965ms
# at default on the same question — small, but it is spent on every press.
# "minimal" is rejected by this model. Sent only if the model accepts it.
ANSWER_EFFORT = os.getenv("ANSWER_EFFORT", "low").strip()
# Below this much buffered audio there is nothing worth a call.
FLUSH_MIN_SECONDS = 0.7
# Mean RMS (float32, 0-1) below which a chunk counts as silence and is dropped
# without a call. Raise it if a noisy room keeps triggering transcription.
# Measured floor on a genuinely silent Realtek input is ~0.00007, and speech
# picked up indirectly sits near 0.0035 — so this has to be well under that or
# it eats real talking.
AUDIO_SILENCE_RMS = 0.0015
TRANSCRIPT_WINDOW_MINUTES = 15
# How much transcript the answer call gets to see, total.
ANSWER_CONTEXT_SECONDS = 180
# ...of which this much is the part that actually gets answered. Everything
# older is passed as background only. Without this split the model reads three
# minutes of chatter and summarises it instead of answering the last thing said.
#
# Wide enough to keep a follow-up intelligible: "and C and D?" means nothing
# without the "totals for A and B?" that preceded it. What stops the answer
# merging them is ANSWER_HISTORY below — the questions already answered are
# named as answered, rather than being hidden by a narrow window.
ANSWER_FOCUS_SECONDS = 40
# How many recent question/answer pairs to carry. They give the model continuity
# and, more importantly, tell it which questions are already dealt with so it
# answers only what is new.
ANSWER_HISTORY = 3

# --- Cost estimate ---------------------------------------------------------
# Printed on quit. USAGE is measured from what the API reports; these RATES are
# a guess and go stale — this project pins a model whose pricing the code cannot
# look up. Check them against your billing page and override in .env, otherwise
# read the total as an order of magnitude rather than a bill.
RATE_TRANSCRIBE_PER_MIN = float(os.getenv("RATE_TRANSCRIBE_PER_MIN", "0.003"))
RATE_ANSWER_IN_PER_M = float(os.getenv("RATE_ANSWER_IN_PER_M", "2.50"))
RATE_ANSWER_OUT_PER_M = float(os.getenv("RATE_ANSWER_OUT_PER_M", "10.00"))
RATE_TTS_PER_M_CHARS = float(os.getenv("RATE_TTS_PER_M_CHARS", "15.00"))

# --- Reference documents ---------------------------------------------------
# .txt and .md files in DOCS_DIR, sent whole with every answer. Re-read on each
# press, so a file dropped in mid-meeting is live on the next press.
DOCS_ENABLED = os.getenv("DOCS_ENABLED", "1").strip() == "1"
# Not a limit — a warning. Past this the folder is big enough that sending it
# whole is the wrong design and retrieval would be worth its complexity again.
# ~20k characters is roughly 5k tokens on every single answer.
DOCS_MAX_CHARS = int(os.getenv("DOCS_MAX_CHARS", "20000"))

# --- Trigger ---------------------------------------------------------------
# Two answer buttons, deliberately different jobs.
#
#   F9  — what was SAID. Transcript only, no screen.
#   F10 — what was said AND what is SHOWN. Sends the actual screenshot, so it can
#         read detail the screen loop's summary never captured. Costs a vision
#         round trip. With no transcript yet it is simply "read the screen".
#
# Most USB footswitches emulate a keypress, so whatever key yours sends goes here.
#
# Settable in .env, because the F-keys are contested: screenshot tools bind F10,
# browsers take F12, and OEM laptops send media keys unless Fn is held. If a
# button does nothing — or does something else entirely — another app owns that
# key, and the fix is to pick one nothing else wants. `check_setup --keys` shows
# what actually arrives.
#
# Single keys only, by pynput's name: f1-f24, insert, delete, home, end,
# page_up, page_down, pause, scroll_lock, print_screen, menu. The angle brackets
# are optional. Rarely-contested picks: pause, scroll_lock, insert, f13-f24.
HOTKEY_AUDIO = os.getenv("HOTKEY_AUDIO", "<f9>").strip()
HOTKEY_FULL = os.getenv("HOTKEY_FULL", "<f10>").strip()
HOTKEY_REGION = os.getenv("HOTKEY_REGION", "<f8>").strip()
# Show/hide the history window without quitting.
HOTKEY_HISTORY = os.getenv("HOTKEY_HISTORY", "<f7>").strip()
HOTKEY_QUIT = os.getenv("HOTKEY_QUIT", "<f12>").strip()

# --- Overlay ---------------------------------------------------------------
# A line of --- in an answer separates the sentence to hear from the block to
# read, because code cannot be listened to.
READ_MARKER = "---"

# Where answers appear on screen, if anywhere:
#   history — ONE window, opened at startup, holding the running log of what was
#             heard and what was answered. Scrollable. No per-press popups.
#   read    — a card, only when the answer carries something to look at.
#   always  — a card on every answer.
#   off     — nothing; voice only.
#
# "history" by default. Per-press cards were noise, and a code answer that
# disappears after nine seconds is no use — but the terminal is not a safe place
# to read from either, since VS Code and Windows Terminal own their windows and
# cannot be kept out of a screen share. This one can (see demo/privacy.py).
OVERLAY_MODE = os.getenv("OVERLAY_MODE", "history").strip().lower()
# Superseded by OVERLAY_MODE, still honoured so an existing .env keeps working.
if os.getenv("OVERLAY_ENABLED", "").strip() == "0" and not os.getenv("OVERLAY_MODE"):
    OVERLAY_MODE = "off"
# Keep this tool's own windows out of screen shares — the overlay, and the
# console the answers print into. On by default: the entire point is a private
# assist, and that collapses the moment you share your screen with the answers
# sitting on it. Protects windows only; it does nothing about audio leaking
# through your microphone. See demo/privacy.py.
HIDE_FROM_CAPTURE = os.getenv("HIDE_FROM_CAPTURE", "1").strip() == "1"
OVERLAY_SECONDS = 9
# Code needs longer on screen than a sentence does. Grows with the line count
# so a long block is not yanked away mid-read.
OVERLAY_READ_BASE_SECONDS = 20
OVERLAY_READ_PER_LINE = 2.5
OVERLAY_READ_MAX_SECONDS = 120
# The history window. Big enough to hold a method body without wrapping.
HISTORY_WIDTH = 760
HISTORY_HEIGHT = 460
# A long meeting would otherwise grow the buffer without limit.
HISTORY_MAX_LINES = 2000
OVERLAY_WIDTH = 460
# Wider when there is code in it — wrapped code is unreadable.
OVERLAY_CODE_WIDTH = 780
OVERLAY_MARGIN = 28

# --- Speech ----------------------------------------------------------------
# On by default: the point of this rig is not having to read mid-meeting.
TTS_ENABLED = os.getenv("TTS_ENABLED", "1").strip() == "1"
# Substring match against an output device name. PIN THIS before using it in a
# real meeting — spoken answers must reach your earbuds and nothing else.
TTS_DEVICE = os.getenv("TTS_DEVICE", "").strip()
# Raw PCM streams straight into the sound card with no decode step, so audio
# starts on the first chunk instead of after a whole file lands.
TTS_FORMAT = "pcm"
TTS_SAMPLE_RATE = 24_000
# Playback speed. Default voices read at a presenter's pace, which is far too
# slow when you are trying to catch an answer inside a live conversation — you
# have already missed the next sentence of the meeting. The API caps this at 4.0;
# 1.75 is brisk but still clear on a single listen, which is all you get.
TTS_SPEED = float(os.getenv("TTS_SPEED", "1.75"))
# Spoken answers get truncated to this. Long speech is the failure mode here —
# your coworker keeps talking over it. Sized to fit the ~45-word answer the
# screen loop is asked for, plus a CRM line when one matches.
TTS_MAX_CHARS = 300


def missing_key() -> bool:
    return not OPENAI_API_KEY or OPENAI_API_KEY.startswith("sk-...")
