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
# Time-to-transcript, and the single biggest lever on whether the button is
# useful. At 20s you hear a question, press, and the words are still sitting in
# a half-full buffer — so the bot answers something older. Short chunks cost
# more transcription calls (they are cheap) and buy a button that knows about
# the thing you just heard.
AUDIO_CHUNK_SECONDS = 7
# On a button press, transcribe the last this-many seconds as ONE clip.
#
# Not just the half-full chunk: chunk boundaries fall wherever the cadence puts
# them, so a four-second question routinely lands as two fragments, and short
# fragments do not transcribe — they hallucinate ("Хит-парад" on a clean
# recording of an English sentence). A single clip spanning the whole question
# is both fresher and far more accurate, and it costs one call.
FLUSH_LOOKBACK_SECONDS = 14.0
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
ANSWER_FOCUS_SECONDS = 45
# A pre-computed answer older than this is stale — the meeting has moved on, so
# the button answers live instead of replaying a dead question.
PENDING_ANSWER_TTL = 45

# --- Trigger ---------------------------------------------------------------
# Two answer buttons, deliberately different jobs.
#
#   F9  — what was SAID. Transcript only, no screen. Instant when the audio loop
#         already pre-answered the question it overheard.
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
HOTKEY_QUIT = os.getenv("HOTKEY_QUIT", "<f12>").strip()

# --- Overlay ---------------------------------------------------------------
# Off by default: the point of this rig is a voice in your ear, and a card that
# appears on every press is one mis-shared screen away from being seen. Turn it
# on when you want the detail to stay readable after the sentence has played.
# Faults are spoken either way, so nothing is lost by leaving this off.
OVERLAY_ENABLED = os.getenv("OVERLAY_ENABLED", "0").strip() == "1"
OVERLAY_SECONDS = 9
OVERLAY_WIDTH = 460
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
