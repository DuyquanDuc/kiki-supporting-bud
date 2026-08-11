"""Meeting audio: capture continuously, transcribe on a slow sweep and on press.

Loop 2 of the three in docs/architecture.md. Listens on **two** sources at once
and merges them into one rolling 15-minute transcript, tagged by who spoke:

    Them  the system output mix (loopback) — your coworkers' voices
    You   your microphone — what you said

Both halves matter. Loopback alone cannot hear you answer, clarify, or commit to
anything, so the bot loses the half of the conversation it is meant to support
you through. And they fail independently: a machine where Stereo Mix is disabled
still has a working mic, so one dead source degrades the transcript instead of
killing it.

Audio accumulates in memory and is transcribed in ONE pass, either by a slow
background sweep or by a button press — whichever comes first. Both drain the
same buffer under one lock, so an utterance never gets split between them.

The cadence is the whole design, and both extremes were tried and failed. At 7s
it was fast at the button and wrong: 0.844 against known ground truth, because
every boundary lands mid-word and both halves come back garbled — "cái ví dụ
HTML" as "cái ví TML". Transcribing only on press scored 0.992 but a whole
meeting piled up behind one button, and presses measured 7-10s.

A ~40s sweep sits between them. Boundary cost is paid per boundary, so making
them rare costs almost nothing (0.992 whole against 0.977 split in two), while
the press only has to transcribe what has arrived since the last sweep. It also
means the transcript fills in during the meeting rather than only when asked.

Two gates keep it honest:

1. **Silence gate.** A buffer below an RMS floor is dropped without a call.
   Transcription models are notorious for inventing text out of silence.
2. **Self gate.** While the bot is speaking, capture on *every* source is
   discarded. Loopback would hear its own answer directly, and the mic would
   hear it out of the speakers — either way it transcribes itself and starts
   answering itself.
"""

from __future__ import annotations

import io
import queue
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import config, docs as documents
from .meter import METER

THEM = "Them"
YOU = "You"

_CUE_RULES = """DELIVERY. One sentence, 15-25 words — two only if the question has two
halves. Spoken into the user's ear while the meeting keeps moving, so every
extra word costs them the next thing said. Front-load: the substance in the
first five words. Yes/no questions start with the yes or no. No hedging
("I think", "generally"), no filler openers, no lists, no summaries, no markup.

A cue for ONE listener, never repeated aloud by anyone. Strip the packaging,
keep the substance — a sharp colleague leaning over, not a textbook:

  BAD:  "Stack memory stores method calls and local variables; heap memory
        stores objects and is managed by garbage collection."
  GOOD: "Stack holds your local variables and method calls, heap is where
        objects live — that's the part that gets garbage collected."
  BAD:  "The November deadline is at risk because the authentication migration
        has slipped by two weeks."
  GOOD: "Probably not — auth slipped a couple of weeks, so November's tight."
  GOOD (vi): "Stack giữ biến local với lời gọi hàm. Heap chứa object, và heap
        mới bị garbage collect."
  GOOD (ja): 「スタックはローカル変数とメソッド呼び出し。オブジェクトはヒープで、
        GC 対象もヒープ側」

LANGUAGE. Answer in the language the question was asked in; a mixed question
gets its majority language. Keep technical terms exactly as spoken — engineers
say "deploy", "stack", "milestone" in English mid-sentence in every language.
No politeness forms: Japanese stays compact (です・ます or bare noun phrases, no
敬語), Vietnamese drops pronouns entirely, no dạ/ạ.

SOMETHING TO LOOK AT. Only when the answer IS code, a command, or an exact
identifier: the spoken sentence first (obeying the length rule), then a line
containing only ---, then the thing to read. Below --- is printed, never
spoken, exempt from the length rule — the smallest complete thing, not a
tutorial. Ordinary answers never get a --- block."""

# Everything about WHICH question to answer and what may be trusted. Shared, so
# the short and detailed buttons can never disagree about what was asked — only
# about how much to say.
_CONTEXT_RULES = f"""You get a transcript tagged by speaker: "{THEM}:" is other people, "{YOU}:"
is the user — treat those as their own words, so never tell them what they just
said or contradict them without reason.

ANSWER THE FINAL QUESTION ONLY. JUST SAID may hold several questions asked
seconds apart; the earlier ones are context, not work — never merge them into
one reply. ALREADY ANSWERED lists what you just answered: settled, kept for
continuity ("and C and D?" needs the A-and-B before it). If the same question
comes again, they did not hear you — answer it again, shorter.

The transcript is imperfect speech-to-text. A repeated line is one utterance
heard twice. Near-identical forms of one term (ETA, EPA, ETR, ETL) are one
word heard badly — pick the reading that fits the context and answer that. If
the latest lines correct an earlier question ("no, ETL, not ETA"), the
corrected question is the question.

If no question was asked, catch them up instead: what is being discussed and
where it stands, leading with anything the room is waiting on the user for.
Current state, not a history.

Ground everything in the transcript and the reference material. Never invent a
figure, a name, or a commitment on the user's behalf. If what is needed is
missing, say what is missing — and if it would be on the screen, say "check the
screen" so they know to press the other button."""

# F9. Deliberately blind to the screen: mixing in screen context made it answer
# questions nobody asked. Every behaviour in here was added against a measured
# failure — trim with care, but keep it lean: this rides on every press.
_ANSWER_PROMPT = f"""Someone in a meeting just asked a question out loud and the user needs the
answer in their ear immediately. You cannot see their screen.

{_CONTEXT_RULES}

{_CUE_RULES}"""

# F11. Same question, same grounding, but the user has asked for depth.
_DETAIL_PROMPT = f"""Someone in a meeting asked a question and the user wants the full answer, not
the one-line cue. You cannot see their screen.

{_CONTEXT_RULES}

SHAPE OF THE REPLY. Two parts, split by a line containing only ---.

Above the marker: ONE sentence, 15-25 words, that is spoken aloud. It has to
stand alone, because the user may act on it without reading further. Front-load
the answer exactly as the short button would.

Below the marker: the detail, which is READ, not spoken. This is where depth
belongs — 80-150 words. Use short paragraphs or "- " bullets. Include the
things a cue has to drop: the why, the trade-off, the exception, the concrete
number or example, the thing that bites people. Code, commands and exact
identifiers go here too.

Depth is not padding. No restating the question, no "in summary", no
definitions of terms the user obviously knows — they are an engineer in a
meeting about their own project. If the honest answer is short, let it be
short; a thin answer stretched to fill the space wastes the seconds this
button costs.

Answer in the language the question was asked in, both parts. Keep technical
terms in the form they were spoken."""

# F8. Not a question at all — the whole meeting so far.
_SUMMARY_PROMPT = f"""The user has been in this meeting a while and wants to see where things
stand. You get the transcript, tagged by speaker: "{THEM}:" is other people,
"{YOU}:" is the user themselves.

This is NOT a question to answer. Summarise the conversation.

SHAPE. One sentence above a line containing only ---, then bullets below it.

The spoken sentence is the single most important thing to know right now — the
decision taken, the blocker, or what the room is waiting on the user for. If
nothing stands out, say what the meeting is about in one line.

Below the marker, "- " bullets, at most seven, newest concerns first. Each is
one line. Cover only what was actually said:

- decisions and who owns them
- numbers, dates and deadlines exactly as stated
- open questions and disagreements still unresolved
- anything the user personally agreed to or was asked for

Drop pleasantries, tangents and thinking-aloud. A bullet nobody would act on
should not be there. If the transcript is too thin to summarise, say so in one
line and stop — do not pad.

Never invent a decision, a name, a number or a commitment. Where the transcript
is garbled, say what was unclear rather than guessing. Attribute to the user
only what the "{YOU}:" lines actually say.

"{THEM}" and "{YOU}" are internal labels, not names — never write them. Refer to
the user as "you", or the natural equivalent in the language you are writing in,
and to everyone else by the names they were called in the transcript.

Write in the language the meeting is mostly in. Keep technical terms and
project names exactly as spoken."""

# F10. Same job with the actual pixels.
_FULL_PROMPT = f"""The user is in a meeting and needs help right now. You get a screenshot of
their screen and a transcript of what was said, tagged "{THEM}:" (other
people) and "{YOU}:" (the user's own words). Either may be the important one.

THE SCREEN USUALLY CONTAINS THE QUESTION. A coding problem, a failing test, a
stack trace, a form, a diff waiting on review — those are asks, not scenery,
and describing them back is the one thing the user cannot use: they are
looking at them. Priority:

1. Someone asked out loud — answer that, grounded in the screen.
2. Nothing said, but the screen poses a problem — SOLVE it. A coding problem
   gets working code below the --- marker, matching the language, class and
   method signatures already on screen. An error gets the fix.
3. The screen poses nothing — say what it shows and the point it is making.

Never restate a problem instead of solving it. Read the screenshot exactly —
figures, labels, names, error text. Never invent anything that is not in the
image or the transcript.

{_CUE_RULES}"""

# --- device discovery ------------------------------------------------------


def load_profile() -> str:
    """User notes from profile.md, or "" if there is nothing usable.

    Read on every answer rather than cached at startup: it is one small file
    read against a network round trip, and it means editing the file mid-meeting
    takes effect on the next press.

    Comments and blank headings are stripped so the untouched template — which is
    all headings and HTML comments — contributes nothing instead of teaching the
    model to answer with placeholders.
    """
    try:
        raw = config.PROFILE_FILE.read_text(encoding="utf-8")
    except Exception:
        return ""
    sections: list[tuple[str, list[str]]] = [("", [])]
    skipping = False
    for line in raw.splitlines():
        stripped = line.strip()
        if skipping:
            skipping = "-->" not in stripped
            continue
        if stripped.startswith("<!--"):
            skipping = "-->" not in stripped
            continue
        if stripped.startswith("#"):
            sections.append((line.rstrip(), []))
            continue
        if not stripped:
            continue
        # "- Job title / what I actually do:" with nothing after the colon is an
        # unfilled prompt, not a fact about anyone.
        if stripped.startswith("-") and stripped.endswith(":"):
            continue
        sections[-1][1].append(line.rstrip())

    out: list[str] = []
    for heading, body in sections:
        if not body:
            continue  # a heading with nothing under it is noise in the prompt
        if heading:
            out.append(heading)
        out.extend(body)
    return "\n".join(out).strip()


def list_input_devices() -> list[tuple[int, str, int]]:
    """(index, name, channels) for everything that can record."""
    try:
        import sounddevice as sd
    except Exception:
        return []
    devices = []
    for index, device in enumerate(sd.query_devices()):
        if device.get("max_input_channels", 0) > 0:
            devices.append((index, device["name"], device["max_input_channels"]))
    return devices


def loopback_target() -> str:
    """Name of the speaker WASAPI loopback would capture, or "" if unavailable.

    Checked rather than assumed: soundcard is an optional dependency and the
    call fails on a machine with no active render endpoint.
    """
    try:
        import soundcard

        speaker = soundcard.default_speaker()
        soundcard.get_microphone(id=str(speaker.name), include_loopback=True)
        return str(speaker.name)
    except Exception:
        return ""


def default_input_name() -> str:
    """Name of the system default recording device, for when none is configured."""
    try:
        import sounddevice as sd

        return str(sd.query_devices(kind="input")["name"])
    except Exception:
        return ""


# Windows names the loopback device in the display language, so a machine set to
# Japanese has no device called "Stereo Mix" at all — it is ステレオ ミキサー, and
# matching the English string alone finds nothing. Any alias matches any other.
_DEVICE_ALIASES = [
    {"stereo mix", "ステレオ ミキサー", "ステレオミキサー", "stereomix"},
    {"microphone", "マイク", "mic in"},
]

# Host APIs are not interchangeable. WDM-KS is exclusive-mode: it locks the
# endpoint, and a process killed while holding it leaves the device unopenable
# until the driver resets. WASAPI shared mode is the safe default. Higher wins.
_HOST_API_RANK = {
    "Windows WASAPI": 3,
    "MME": 2,
    "Windows DirectSound": 1,
    "Windows WDM-KS": 0,
}


def _aliases_for(needle: str) -> set[str]:
    matches = {needle}
    for group in _DEVICE_ALIASES:
        if any(alias in needle or needle in alias for alias in group):
            matches |= group
    return matches


def input_candidates(substring: str) -> list[tuple[int, str, str]]:
    """Every input matching `substring`, best host API first.

    Returns (index, name, host_api). Plural on purpose: the same physical device
    shows up once per host API, they fail independently, and the only reliable
    way to pick is to try opening them in order.
    """
    try:
        import sounddevice as sd

        hostapis = sd.query_hostapis()
        devices = list(sd.query_devices())
    except Exception:
        return []
    if not substring:
        return []

    needles = _aliases_for(substring.lower())
    found = []
    for index, device in enumerate(devices):
        if device.get("max_input_channels", 0) <= 0:
            continue
        name = device["name"]
        lowered = name.lower()
        if not any(n in lowered for n in needles):
            continue
        api = hostapis[device["hostapi"]]["name"]
        found.append((_HOST_API_RANK.get(api, -1), index, name, api))
    found.sort(key=lambda row: -row[0])
    return [(index, name, api) for _rank, index, name, api in found]


def resolve_input_device(substring: str) -> int | None:
    """Best single match, or None. See `input_candidates` for the full list."""
    candidates = input_candidates(substring)
    return candidates[0][0] if candidates else None


def open_input(candidates: list[tuple[int, str, str]], samplerate=None, **kwargs):
    """Open the first candidate that actually works.

    Enumeration is not availability on Windows. A device can be listed and still
    refuse to open — disabled, claimed exclusively by another process, or left
    locked by one that died holding it. Trying them in order is the only way to
    find out which one is real right now.

    Returns (stream, index, name, api, errors), stream already started. Raises if
    none work.
    """
    import sounddevice as sd

    errors = []
    for index, name, api in candidates:
        try:
            info = sd.query_devices(index, "input")
            rate = int(samplerate or info.get("default_samplerate") or 48_000)
            channels = min(2, int(info.get("max_input_channels") or 1)) or 1
            stream = sd.InputStream(
                device=index, samplerate=rate, channels=channels, **kwargs
            )
            # Constructing is NOT enough. WDM-KS builds a stream happily and
            # only fails on start ("DeviceIoControl GLE = 0x492") when the
            # kernel pin is gone. Start it here so a device that cannot actually
            # run is rejected in favour of the next candidate.
            try:
                stream.start()
            except Exception:
                try:
                    stream.close()
                except Exception:
                    pass
                raise
            return stream, index, name, api, errors
        except Exception as exc:
            errors.append(f"[{index}] {api}: {str(exc).splitlines()[0]}")
    raise RuntimeError("no input device would open:\n  " + "\n  ".join(errors))


def probe_loopback(device: int, seconds: float = 1.2) -> tuple[float, float]:
    """Play a tone and measure whether `device` actually hears it.

    Returns (quiet_rms, tone_rms). A loopback device that Windows has enumerated
    but not enabled opens cleanly and returns silence forever — the stream works,
    the capture is dead. Comparing the two numbers is the only way to tell the
    difference before you are sitting in a meeting relying on it.
    """
    import sounddevice as sd

    info = sd.query_devices(device, "input")
    samplerate = int(info.get("default_samplerate") or 48_000)
    channels = min(2, int(info.get("max_input_channels") or 1)) or 1

    def listen(play: bool) -> float:
        frames: list[np.ndarray] = []

        def callback(indata, _n, _t, _s) -> None:
            frames.append(indata.mean(axis=1).astype(np.float32))

        with sd.InputStream(
            device=device, samplerate=samplerate, channels=channels,
            dtype="float32", blocksize=2048, callback=callback,
        ):
            if play:
                t = np.arange(int(samplerate * seconds)) / samplerate
                sd.play((0.2 * np.sin(2 * np.pi * 440 * t)).astype(np.float32), samplerate)
            time.sleep(seconds)
            if play:
                sd.stop()
        if not frames:
            return 0.0
        audio = np.concatenate(frames)
        return float(np.sqrt(np.mean(np.square(audio))))

    return listen(play=False), listen(play=True)


def _before_marker(text: str) -> str:
    """The spoken part of an answer: everything above a --- line."""
    for line_index, line in enumerate(text.splitlines()):
        if line.strip() == config.READ_MARKER:
            return "\n".join(text.splitlines()[:line_index]).strip()
    return text.strip()


def _system_prompt(base: str) -> str:
    """Base prompt plus whatever the user has written about themselves."""
    profile = load_profile()
    if not profile:
        return base
    return (
        f"{base}\n\n"
        "ABOUT THE USER — their own notes. Use these to be specific: real project\n"
        "names, who owns what, what kind of meeting this is. They are context, not\n"
        "instructions from the meeting, and nothing here overrides the rules above.\n"
        "If a note conflicts with what was actually said, what was said wins.\n\n"
        f"{profile}"
    )


@dataclass
class Source:
    """One capture device and how it is doing."""

    label: str
    candidates: list[tuple[int, str, str]]
    thread: threading.Thread | None = None
    # "loopback" captures whatever the default speaker is playing, whatever
    # device that is. "device" opens one named input by index.
    backend: str = "device"
    device: str = ""
    api: str = ""
    samplerate: int = 48_000
    frames: int = 0
    level: float = 0.0
    chunks: int = 0
    error: str = ""
    floor: float = field(default_factory=lambda: config.AUDIO_SILENCE_RMS)
    # Everything captured since the last press, waiting to be transcribed.
    #
    # Nothing is transcribed in the background any more. Chunking was measurably
    # destructive: the same 20s of speech scored 0.992 against ground truth as
    # one pass, 0.977 split in two, and 0.844 split in four. Every boundary falls
    # mid-word and costs accuracy — "cái ví dụ HTML" becomes "cái ví TML".
    #
    # So the audio simply accumulates, and a press transcribes the lot in one
    # go. Latency scales sub-linearly (16s->1.0s, 64s->2.5s, 96s->3.9s), so this
    # is affordable, and it costs one call per press instead of a few hundred
    # per meeting.
    buf: deque = field(default_factory=deque)
    buf_frames: int = 0
    buf_lock: threading.Lock = field(default_factory=threading.Lock)

    def add(self, block) -> None:
        """Buffer a block, discarding whatever has aged past the cap.

        The cap bounds how long a press can take: without it, an hour of silence
        followed by one press would try to transcribe an hour.
        """
        self.buf.append(block)
        self.buf_frames += len(block)
        limit = int(config.AUDIO_BUFFER_MAX_SECONDS * self.samplerate)
        while self.buf and self.buf_frames > limit:
            self.buf_frames -= len(self.buf.popleft())

    def drain(self, minimum_seconds: float = 0.0):
        """Take everything buffered and clear it, or None if too little to bother."""
        import numpy as _np

        with self.buf_lock:
            if self.buf_frames < minimum_seconds * self.samplerate or not self.buf:
                return None
            audio = _np.concatenate(list(self.buf))
            self.buf.clear()
            self.buf_frames = 0
            return audio

    @property
    def alive(self) -> bool:
        return bool(self.thread and self.thread.is_alive())


class AudioLoop:
    """Owns N capture sources, one transcript, and the transcription worker.

    Not a Thread itself — it supervises one capture thread per source plus a
    single transcription worker, so a dead device takes down its own source and
    nothing else.
    """

    def __init__(self, client, sources, is_muted=None, on_event=None):
        self._client = client
        self.sources = []
        for label, spec in sources:
            # "loopback" means capture the default speaker's render endpoint,
            # so there is no device index to resolve.
            if spec == "loopback":
                self.sources.append(Source(label, [], backend="loopback"))
            elif isinstance(spec, int):
                self.sources.append(
                    Source(label, [(spec, f"device {spec}", "pinned")])
                )
            else:
                self.sources.append(Source(label, spec))
        # A callable, not a value: it changes constantly and the loop must read
        # it at the moment it needs it, not at construction.
        self._is_muted = is_muted or (lambda: False)
        self._on_event = on_event or (lambda _m: None)

        self._halt = threading.Event()
        self._lock = threading.Lock()
        self._transcript: deque[tuple[float, str, str]] = deque()
        # Never pruned. The rolling window above is short so answer prompts stay
        # small; minutes need the whole meeting.
        self._archive: list[tuple[float, str, str]] = []
        # The archive also goes to disk line by line. Holding a meeting in
        # memory until quit means a crash, a dead battery or a closed laptop
        # loses all of it — and the transcript is the one artefact that cannot
        # be regenerated.
        self._transcript_path: Path | None = None
        self._transcript_file = None
        self._transcript_broken = False
        # Both capture threads flush independently, so the file needs its own
        # lock — two of them opening it at once would leak a handle and
        # interleave a line.
        self._disk_lock = threading.Lock()
        # Bounded on purpose: if transcription falls behind, drop the oldest
        # audio rather than grow a backlog that answers questions from minutes ago.
        self.chunks_transcribed = 0
        self.chunks_processed = 0
        # Questions already answered, with what was said. This is what lets the
        # focus window stay wide: earlier questions are marked as dealt with
        # rather than hidden, so a follow-up keeps the context it needs without
        # the answer folding the old questions back in.
        self._flush_lock = threading.Lock()
        # Per model: F6 and F9 are different models, and one rejecting
        # reasoning_effort says nothing about the other.
        self._no_effort: set[str] = set()
        self._groq_client = None
        self._groq_dead = False
        self._history: deque[tuple[str, str]] = deque(maxlen=config.ANSWER_HISTORY)
        self._last_query = ""

    # --- lifecycle ----------------------------------------------------------

    def transcription_route(self) -> str:
        """Which provider this machine will transcribe with, in words.

        Reported because it is invisible otherwise: the same code is twice as
        fast on a machine whose .env has a Groq key, and nothing on screen would
        say why.
        """
        if config.GROQ_API_KEY and not self._groq_dead:
            return (f"groq {config.GROQ_TRANSCRIBE_MODEL} "
                    f"(fallback: openai {config.TRANSCRIBE_MODEL})")
        return f"openai {config.TRANSCRIBE_MODEL} (no GROQ_API_KEY — about 2x slower)"

    def start(self) -> None:
        for source in self.sources:
            source.thread = threading.Thread(
                target=self._capture, args=(source,), daemon=True,
                name=f"audio-{source.label}",
            )
            source.thread.start()
        if config.BACKGROUND_TRANSCRIBE_SECONDS > 0:
            threading.Thread(target=self._sweep, daemon=True, name="audio-sweep").start()

    def stop(self) -> None:
        self._halt.set()

    def is_alive(self) -> bool:
        """True while at least one source is still capturing."""
        return any(source.alive for source in self.sources)

    # --- aggregate state ----------------------------------------------------

    @property
    def level(self) -> float:
        return max((s.level for s in self.sources), default=0.0)

    @property
    def frames_captured(self) -> int:
        return sum(s.frames for s in self.sources)

    @property
    def chunks_heard(self) -> int:
        return sum(s.chunks for s in self.sources)

    @property
    def error(self) -> str:
        errors = [f"{s.label}: {s.error}" for s in self.sources if s.error]
        return " | ".join(errors)

    def status(self) -> str:
        """Why there is no transcript, in words the button can say out loud.

        Empty string means "listening fine, nobody has said anything". Anything
        else is a real fault the user needs to know about, because "nothing heard
        yet" and "the device never opened" look identical from the outside and
        have completely different fixes.
        """
        living = [s for s in self.sources if s.alive]
        if not living:
            return self.error or "no audio source is running"
        hearing = [s for s in living if s.level >= s.floor]
        if self.frames_captured == 0:
            return "a capture device opened but is delivering no audio at all"
        if not hearing and self.chunks_transcribed == 0:
            detail = ", ".join(f"{s.label} {s.level:.5f}" for s in living)
            return (
                f"capturing, but only silence ({detail}; floor "
                f"{config.AUDIO_SILENCE_RMS}) — the devices are open but are not "
                "picking up the audio you can actually hear"
            )
        return ""

    def health(self) -> list[str]:
        """One human-readable line per source, for logs and pre-flight."""
        lines = []
        for source in self.sources:
            if source.error:
                lines.append(f"{source.label}: FAILED — {source.error}")
            elif not source.alive:
                lines.append(f"{source.label}: not running")
            else:
                # At startup this is sampled over a second or two, so a quiet
                # room reads the same as a broken device. Say which it is rather
                # than calling a working mic "silent" and alarming the user.
                if source.level >= source.floor:
                    state = f"hearing audio (level {source.level:.5f})"
                elif source.frames == 0:
                    state = "open but no audio arriving — check it is not muted"
                else:
                    state = f"open, nothing said yet (level {source.level:.5f})"
                lines.append(f"{source.label}: {source.device} via {source.api}, {state}")
        return lines

    # --- read side (called from the trigger path) --------------------------

    def recent(self, seconds: float = 120.0) -> str:
        """The transcript as speaker-tagged lines, oldest first."""
        cutoff = time.monotonic() - seconds
        with self._lock:
            rows = [(label, text) for stamp, label, text in self._transcript
                    if stamp >= cutoff]
        return "\n".join(f"{label}: {text}" for label, text in rows)

    def _split_transcript(self) -> tuple[str, str]:
        """(background, focus) — old context vs the part to actually answer.

        Handing over one undifferentiated blob is what makes a model summarise:
        with no signal about what just happened, the whole transcript looks
        equally important and the safest output is a recap.
        """
        now = time.monotonic()
        old_cut = now - config.ANSWER_CONTEXT_SECONDS
        focus_cut = now - config.ANSWER_FOCUS_SECONDS

        # No tail to reconcile any more: every line here was written by a press,
        # so the newest lines ARE what was just said.
        with self._lock:
            rows = [(stamp, label, text) for stamp, label, text in self._transcript
                    if stamp >= old_cut]
        background = "\n".join(f"{l}: {t}" for s, l, t in rows if s < focus_cut)
        focus = "\n".join(f"{l}: {t}" for s, l, t in rows if s >= focus_cut)
        # A quiet stretch can leave the focus window empty; answering nothing is
        # worse than answering slightly older speech.
        if not focus and rows:
            focus = "\n".join(f"{l}: {t}" for _s, l, t in rows[-3:])
            background = ""
        return background, focus

    def _last_stamp(self) -> float:
        with self._lock:
            return self._transcript[-1][0] if self._transcript else 0.0

    @property
    def transcript_path(self):
        """Where the live transcript is being written, or None."""
        return self._transcript_path

    def _append_to_disk(self, entry: tuple[float, str, str]) -> None:
        """Append one line and flush it. Never raises into the capture path.

        Flushed per line on purpose: buffering would defeat the point, since the
        lines at risk are the ones written just before whatever killed the app.
        """
        if not config.MINUTES_ENABLED or self._transcript_broken:
            return
        stamp, label, text = entry
        with self._disk_lock:
            try:
                if self._transcript_file is None:
                    config.MINUTES_DIR.mkdir(parents=True, exist_ok=True)
                    # The same stamp minutes.py derives from the first archived
                    # line, so the file opened here IS the one rewritten on
                    # quit — not a second copy beside it.
                    name = time.strftime("%Y-%m-%d_%H%M", time.localtime(stamp))
                    self._transcript_path = config.MINUTES_DIR / f"{name}-transcript.txt"
                    self._transcript_file = self._transcript_path.open(
                        "a", encoding="utf-8", buffering=1
                    )
                    self._on_event(f"saving transcript to {self._transcript_path}")
                clock = time.strftime("%H:%M:%S", time.localtime(stamp))
                self._transcript_file.write(f"[{clock}] {label}: {text}\n")
                self._transcript_file.flush()
            except Exception as exc:
                # Said once, then silence — a full disk must not flood the log
                # or stop the meeting being answered.
                self._transcript_broken = True
                self._on_event(f"transcript NOT being saved: {exc}")

    def close_transcript(self) -> None:
        """Release the handle so the quit-time rewrite can replace the file."""
        with self._disk_lock:
            if self._transcript_file is not None:
                try:
                    self._transcript_file.close()
                except Exception:
                    pass
                self._transcript_file = None

    def archive(self) -> list[tuple[float, str, str]]:
        """Every line transcribed this session, oldest first."""
        with self._lock:
            return list(self._archive)

    def has_transcript(self) -> bool:
        with self._lock:
            return bool(self._transcript)

    def _sweep(self) -> None:
        """Transcribe on a slow cadence so the button has less left to do.

        Deliberately infrequent. The cost of a chunk boundary is paid per
        boundary, so a sweep every 40s loses almost nothing (0.992 whole vs
        0.977 split in two) where a 7s cadence lost a great deal. What it buys
        is a press that only transcribes the remainder, and a transcript that
        fills in during the meeting rather than only when asked.
        """
        while not self._halt.is_set():
            if self._halt.wait(config.BACKGROUND_TRANSCRIBE_SECONDS):
                return
            # Never while the bot is talking: the buffer is being discarded
            # anyway, and a sweep would just spend a call on the gap.
            if self._is_muted():
                continue
            try:
                self.flush_and_wait()
            except Exception as exc:
                self._on_event(f"audio sweep failed: {exc}")

    def flush_and_wait(self, timeout: float = 12.0) -> bool:
        """Transcribe everything captured since the last press, and keep it.

        The only transcription that happens at all. Each source's buffer is
        drained and sent as one clip, which is what makes it accurate: chunking
        the same speech scored 0.844 against ground truth where one pass scored
        0.992, because every boundary lands mid-word.

        The result is appended to the transcript rather than held aside. There is
        nothing to overlap with any more — this IS the transcript.

        Returns True if it produced anything.
        """
        # A press and a background sweep must never drain the same buffer at
        # once, or half an utterance goes to each and both come back wrong.
        # The press waits for an in-flight sweep rather than skipping: it needs
        # that text to answer.
        if not self._flush_lock.acquire(timeout=timeout):
            return False
        try:
            return self._flush(timeout)
        finally:
            self._flush_lock.release()

    def _flush(self, timeout: float) -> bool:
        jobs = []
        for source in self.sources:
            if not source.alive:
                continue
            audio = source.drain(config.FLUSH_MIN_SECONDS)
            if audio is None:
                continue
            if float(np.sqrt(np.mean(np.square(audio)))) < source.floor:
                continue  # nobody spoke; transcribing silence invents text
            jobs.append((source, audio))
        if not jobs:
            return False

        # Both sources at once, not one after the other. When loopback and mic
        # are both live, sequential transcription doubles the wait for zero
        # benefit — the calls are independent and the API takes them in
        # parallel. With one source active this changes nothing.
        results: list[str | None] = [None] * len(jobs)

        def work(index: int, source: Source, audio) -> None:
            results[index] = self._transcribe(audio, source.samplerate, source.label)

        threads = [
            threading.Thread(target=work, args=(i, s, a), daemon=True)
            for i, (s, a) in enumerate(jobs)
        ]
        for thread in threads:
            thread.start()
        deadline = time.monotonic() + timeout
        for thread in threads:
            thread.join(max(0.1, deadline - time.monotonic()))

        got = False
        for (source, audio), text in zip(jobs, results):
            seconds = len(audio) / max(1, source.samplerate)
            source.chunks += 1
            if not text:
                continue
            now = time.monotonic()
            with self._lock:
                self._transcript.append((now, source.label, text))
                # Kept unpruned for the minutes written on quit. The rolling
                # window above is deliberately short so answer prompts stay
                # small, but minutes for a two-hour meeting cannot be written
                # from its last fifteen minutes. Text only — an hour is tens of
                # kilobytes.
                stamped = (time.time(), source.label, text)
                self._archive.append(stamped)
                cutoff = now - config.TRANSCRIPT_WINDOW_MINUTES * 60
                while self._transcript and self._transcript[0][0] < cutoff:
                    self._transcript.popleft()
            # Outside the lock: this touches the disk, and the answer buttons
            # read the transcript on the keyboard thread. A slow write must not
            # stall an answer.
            self._append_to_disk(stamped)
            self.chunks_transcribed += 1
            got = True
            # Not truncated. This is the transcript itself now, not a preview of
            # a 7-second chunk — a press can carry a minute of speech, and
            # clipping it at 70 characters threw most of the meeting away.
            self._on_event(f"heard [{source.label}] ({seconds:.0f}s): {text}")
        return got

    def answer_now(self, on_spoken=None) -> str:
        """Answer from the transcript. Every F9 press comes through here.

        `on_spoken` fires with the spoken part as soon as it is complete, so
        speech can start while any code block is still generating.
        """
        return self._compose("", on_spoken)

    def summarize(self, on_spoken=None) -> str:
        """F8. The whole meeting so far, in points.

        Deliberately reads the FULL transcript window rather than the
        background/focus split the answer buttons use: those exist to find the
        last question, and this one is not answering a question.
        """
        transcript = self.recent(config.TRANSCRIPT_WINDOW_MINUTES * 60)
        if not transcript.strip():
            return ""
        parts = []
        reference = self._reference(transcript[-2000:])
        if reference:
            parts.append(
                "REFERENCE MATERIAL — the user's own documents, for names and "
                "context. Do not summarise these; they are not the meeting:\n"
                + reference
            )
        parts.append(f"TRANSCRIPT OF THE MEETING SO FAR:\n{transcript}")
        parts.append("Summarise where things stand.")
        try:
            text, usage, served_fast = self._answer_call(
                [
                    {"role": "system", "content": _system_prompt(_SUMMARY_PROMPT)},
                    {"role": "user", "content": "\n\n".join(parts)},
                ],
                on_spoken=on_spoken,
            )
            METER.answered(usage)
            return text.strip()
        except Exception as exc:
            self._fail_global(f"summary failed: {exc}")
            return ""

    def answer_detailed(self, on_spoken=None) -> str:
        """F11. Same question and grounding as F9, but depth below the marker.

        The spoken half stays a cue — a 150-word answer read aloud at 1.75x is
        half a minute of talking over a live meeting. Depth goes under --- to be
        read, which is also why on_spoken fires early: the sentence is done long
        before the detail finishes generating.
        """
        return self._compose("", on_spoken, prompt=_DETAIL_PROMPT)

    def answer_fast(self, on_spoken=None) -> str:
        """F6. The same question and grounding as F9, on the fast model.

        Worth roughly a second on English and a second and a half on Japanese,
        paid for in judgement — see HOTKEY_FAST in config for what that costs.
        Falls back to the main model when Groq is not configured, so the button
        always answers something.
        """
        return self._compose("", on_spoken, fast=True)

    def fast_available(self) -> bool:
        return self._groq() is not None

    def record_delivered(self, answer: str, question: str = "") -> None:
        """Note that this answer actually reached the user.

        Recorded on delivery so the history holds only what the user actually
        heard.
        """
        # A parked answer is delivered long after it was composed, by which time
        # _last_query belongs to some later composition — so the caller passes
        # the question it actually answered.
        question = (question or self._last_query or "").strip()
        if not question or not answer.strip():
            return
        spoken = answer.split(config.READ_MARKER)[0].strip() or answer.strip()
        self._history.append((question[-300:], spoken[:300]))

    def _history_text(self) -> str:
        if not self._history:
            return ""
        lines = []
        for question, answer in self._history:
            lines.append(f"Q: {' '.join(question.split())}")
            lines.append(f"A: {' '.join(answer.split())}")
        return "\n".join(lines)

    def answer_with_screenshot(self, image, on_spoken=None) -> str:
        """The full-context button: real pixels plus the transcript.

        The screen loop's summary is a sentence written for a different purpose,
        so anything it did not think worth recording is gone. Sending the image
        itself is the only way to answer "what does row seven say".
        """
        from .screen_loop import encode_jpeg

        background, focus = self._split_transcript()
        if focus:
            said = ""
            if background:
                said += f"BACKGROUND (context only):\n{background}\n\n"
            said += f"JUST SAID (answer the most recent question in here):\n{focus}"
        else:
            said = "(nothing audible yet — answer from the screen alone)"
        reference = self._reference(focus or background)
        if reference:
            said = (
                "REFERENCE MATERIAL — the user's own documents. Prefer these over "
                "your own knowledge when they conflict:\n" + reference + "\n\n" + said
            )
        content: list[dict] = [{"type": "text", "text": said}]
        if image is not None:
            content.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/jpeg;base64,{encode_jpeg(image)}",
                    "detail": config.VISION_DETAIL,
                },
            })
        try:
            text, usage, served_fast = self._answer_call(
                [
                    {"role": "system", "content": _system_prompt(_FULL_PROMPT)},
                    {"role": "user", "content": content},
                ],
                model=config.VISION_MODEL,
                on_spoken=on_spoken,
            )
            METER.answered(usage, vision=True)
            return text.strip()
        except Exception as exc:
            self._fail_global(f"full answer failed: {exc}")
            return ""

    # --- capture ------------------------------------------------------------

    def _capture_loopback(self, source: Source) -> None:
        """Capture whatever the default speaker is playing, via WASAPI loopback.

        This is what makes Bluetooth and wired headphones work. Stereo Mix is a
        capture pin on the Realtek codec, so it only hears what Realtek renders —
        route audio to a Bluetooth headset and Realtek renders nothing, so Stereo
        Mix records silence while the meeting plays perfectly. WASAPI loopback
        attaches to the render endpoint itself, whichever device that is.

        It also sidesteps every Stereo Mix failure this project has hit: disabled
        by default, enumerated-but-dead, and WDM-KS pins left locked by a killed
        process.
        """
        try:
            import soundcard
        except Exception as exc:
            source.error = f"soundcard unavailable: {exc}"
            return

        # WASAPI is COM, and COM is per-thread. Without this the capture thread
        # fails with CO_E_NOTINITIALIZED (0x800401f0) while the identical code
        # works from the main thread, where something else already initialised
        # it. S_FALSE means "already initialised on this thread", which is fine.
        try:
            import ctypes

            ctypes.windll.ole32.CoInitializeEx(None, 0)  # COINIT_MULTITHREADED
        except Exception:
            pass

        try:
            speaker = soundcard.default_speaker()
            microphone = soundcard.get_microphone(id=str(speaker.name),
                                                  include_loopback=True)
        except Exception as exc:
            source.error = f"no loopback for the default speaker: {exc}"
            self._on_event(f"audio [{source.label}] {source.error}")
            return

        source.samplerate = 48_000
        source.device = f"loopback of {speaker.name}"
        source.api = "WASAPI loopback"
        block = 2048
        sweep = config.BACKGROUND_TRANSCRIBE_SECONDS
        cadence = (f"sweeping every {sweep:.0f}s" if sweep > 0
                   else "transcribing only on press")
        muted_frames = 0
        try:
            with microphone.recorder(samplerate=source.samplerate, channels=1,
                                     blocksize=block) as recorder:
                self._on_event(
                    f"audio [{source.label}] listening — {source.device} "
                    f"via WASAPI loopback, {source.samplerate}Hz, {cadence} "
                    f"(buffer max {config.AUDIO_BUFFER_MAX_SECONDS:.0f}s)"
                )
                while not self._halt.is_set():
                    data = recorder.record(numframes=block)
                    if data is None or len(data) == 0:
                        continue
                    mono = data.mean(axis=1) if data.ndim > 1 else data
                    mono = mono.astype(np.float32)
                    if self._is_muted():
                        muted_frames += len(mono)
                        continue
                    source.frames += len(mono)
                    block_rms = float(np.sqrt(np.mean(np.square(mono))))
                    source.level = max(source.level * 0.85, block_rms)
                    with source.buf_lock:
                        source.add(mono)
        except Exception as exc:
            source.error = f"loopback capture stopped: {exc}"
            self._on_event(f"audio [{source.label}] error: {exc}")
        finally:
            if muted_frames:
                self._on_event(
                    f"audio [{source.label}] discarded "
                    f"{muted_frames / max(1, source.samplerate):.0f}s "
                    "captured while speaking"
                )

    def _capture(self, source: Source) -> None:
        if source.backend == "loopback":
            self._capture_loopback(source)
            return
        # PortAudio's WDM-KS backend rejects blocking reads outright ("Blocking
        # API not supported yet"), so callback mode is the only way in.
        blocks: "queue.Queue[np.ndarray]" = queue.Queue()
        muted = {"frames": 0}

        def callback(indata, frames, _time_info, _status) -> None:
            # Runs on PortAudio's thread: no I/O, no locks held for long.
            # Drop audio recorded while we are talking rather than gating later,
            # so the discard lines up with when the sound actually happened.
            if self._is_muted():
                muted["frames"] += frames
                return
            blocks.put(indata.mean(axis=1).astype(np.float32))

        try:
            stream, index, name, api, errors = open_input(
                source.candidates, dtype="float32", blocksize=2048, callback=callback
            )
        except Exception as exc:
            source.error = str(exc)
            self._on_event(f"audio [{source.label}] failed: {exc}")
            return
        for skipped in errors:
            self._on_event(f"audio [{source.label}] skipped {skipped}")

        source.device = f"[{index}] {name}"
        source.api = api
        source.samplerate = int(stream.samplerate)
        try:
            sweep = config.BACKGROUND_TRANSCRIBE_SECONDS
            cadence = (f"sweeping every {sweep:.0f}s" if sweep > 0
                       else "transcribing only on press")
            self._on_event(
                f"audio [{source.label}] listening — {source.device} via {api}, "
                f"{source.samplerate}Hz, {cadence} "
                f"(buffer max {config.AUDIO_BUFFER_MAX_SECONDS:.0f}s)"
            )
            # Buffer only. Nothing is transcribed until a button press, so this
            # loop makes no network calls at all.
            while not self._halt.is_set():
                try:
                    block = blocks.get(timeout=0.5)
                except queue.Empty:
                    continue
                source.frames += len(block)
                block_rms = float(np.sqrt(np.mean(np.square(block))))
                source.level = max(source.level * 0.85, block_rms)
                with source.buf_lock:
                    source.add(block)
        except Exception as exc:
            source.error = f"capture stopped: {exc}"
            self._on_event(f"audio [{source.label}] error: {exc}")
        finally:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
            if muted["frames"]:
                self._on_event(
                    f"audio [{source.label}] discarded "
                    f"{muted['frames'] / max(1, source.samplerate):.0f}s "
                    "captured while speaking"
                )

    # --- transcription -----------------------------------------------------

    def _to_wav(self, chunk: np.ndarray, samplerate: int) -> io.BytesIO:
        import soundfile as sf

        buffer = io.BytesIO()
        sf.write(buffer, chunk, samplerate, format="WAV", subtype="PCM_16")
        buffer.seek(0)
        buffer.name = "chunk.wav"  # the SDK infers the format from the filename
        return buffer

    def _vocabulary(self) -> str:
        """Terms to bias transcription toward.

        Acronyms are where speech-to-text falls apart: "ETL" comes back as ETA,
        EPA, ETR, "eatcia" — four renderings of three letters, and every one of
        them sends the answer somewhere else. The model is guessing at letters
        with no context, so give it context.

        STATIC ONLY — the user's own notes, never the running transcript.

        Feeding recent transcript lines in here looked obviously right and was a
        disaster. The API treats `prompt` as preceding context, so the model
        happily *continues* it: recent lines came back echoed inside the new
        transcription, which then became the next prompt, which echoed again.
        Within a minute the transcript was the same sentences rotating and
        accumulating —

            Them: Hôm qua anh Tuấn gửi cho phần driver của FTOS ấy.
            Them: ... FTOS ấy. Ừ, xem ở đây thì nó hơi khó ấy ...
            Them: ... nó hơi khó ấy ... Ở dưới này cái thằng ...

        — and every answer was built on speech nobody said twice.

        profile.md is fixed text, so it biases without compounding. That is what
        fixed "ETL" being heard as ETA/EPA/ETR, and it needs no transcript.
        """
        profile = load_profile()
        return profile[:900] if profile else ""

    def _groq(self):
        """Lazily built Groq client, or None if unconfigured or disabled."""
        if not config.GROQ_API_KEY or self._groq_dead:
            return None
        if self._groq_client is None:
            try:
                from openai import OpenAI

                self._groq_client = OpenAI(
                    api_key=config.GROQ_API_KEY,
                    base_url="https://api.groq.com/openai/v1",
                    timeout=20.0,
                )
            except Exception:
                self._groq_dead = True
                return None
        return self._groq_client

    def _transcribe(self, chunk: np.ndarray, samplerate: int, label: str) -> str:
        """Groq first when configured, OpenAI as the fallback.

        Measured at roughly half the latency for the same text — 554ms against
        1119ms on 20-26s of speech, with identical English output. Transcription
        is about 40% of a press, so it is the largest saving available.

        A failure here must never cost an answer, so anything that goes wrong
        falls through to OpenAI. An auth failure disables Groq for the session
        (every later call would waste a round trip); a rate limit does not, since
        the free tier's limits reset and the next press may well succeed.
        """
        vocabulary = self._vocabulary()
        groq = self._groq()

        if groq is not None:
            try:
                request = {
                    "model": config.GROQ_TRANSCRIBE_MODEL,
                    "file": self._to_wav(chunk, samplerate),
                    "response_format": "text",
                }
                if vocabulary:
                    request["prompt"] = vocabulary[:config.GROQ_PROMPT_LIMIT]
                response = groq.audio.transcriptions.create(**request)
                METER.transcribed(len(chunk) / max(1, samplerate), provider="groq")
                text = response if isinstance(response, str) else getattr(response, "text", "")
                return " ".join(str(text).split())
            except Exception as exc:
                message = str(exc)
                if "401" in message or "invalid_api_key" in message:
                    self._groq_dead = True
                    self._on_event("groq key rejected — using OpenAI for transcription")
                else:
                    self._on_event(f"groq transcription failed, falling back: {message[:70]}")

        try:
            request = {
                "model": config.TRANSCRIBE_MODEL,
                "file": self._to_wav(chunk, samplerate),
                "response_format": "text",
            }
            if vocabulary:
                request["prompt"] = vocabulary
            response = self._client.audio.transcriptions.create(**request)
            METER.transcribed(len(chunk) / max(1, samplerate))
        except Exception as exc:
            self._fail_global(f"transcription failed ({label}): {exc}")
            return ""
        text = response if isinstance(response, str) else getattr(response, "text", "")
        return " ".join(str(text).split())

    def _reference(self, _query: str = "") -> str:
        """The user's documents, whole. Re-read each time so edits are live."""
        if not config.DOCS_ENABLED:
            return ""
        try:
            return documents.load()
        except Exception as exc:
            self._on_event(f"docs: could not read: {exc}")
            return ""

    def _compose(self, question: str = "", on_spoken=None, prompt=None,
                 fast: bool = False) -> str:
        """Transcript -> one spoken-length answer. No screen, by design.

        `question` is the line the matcher flagged. Passing it explicitly matters:
        the loop already knows which words were the question, and making the
        model re-find them in minutes of transcript is how you get a summary
        instead of an answer.
        """
        background, focus = self._split_transcript()
        # Remembered so record_delivered() knows what this answer answered,
        # without main having to reconstruct it.
        self._last_query = question or focus
        parts = []
        reference = self._reference(question or focus)
        if reference:
            parts.append(
                "REFERENCE MATERIAL — the user's own documents, indexed before "
                "the meeting. Prefer these over your own knowledge when they "
                "conflict, and cite the file name if it matters:\n" + reference
            )
        if background:
            parts.append(f"BACKGROUND (context only — do not answer these):\n{background}")
        already = self._history_text()
        if already:
            parts.append(
                "ALREADY ANSWERED — you gave these moments ago. They are settled.\n"
                "Use them for continuity, but do not answer them again and do not\n"
                "fold them into your reply:\n" + already
            )
        parts.append(f"JUST SAID (the live part of the conversation):\n{focus}")
        if question:
            parts.append(f"THE QUESTION TO ANSWER, heard moments ago:\n{question}")
            parts.append("Answer that question now. Lead with the answer.")
        else:
            parts.append(
                "Find the most recent question in JUST SAID and answer it. "
                "If there is no question, catch the user up on what is being "
                "discussed and where it stands."
            )
        try:
            text, usage, served_fast = self._answer_call(
                [
                    {"role": "system", "content": _system_prompt(prompt or _ANSWER_PROMPT)},
                    {"role": "user", "content": "\n\n".join(parts)},
                ],
                on_spoken=on_spoken,
                fast=fast,
            )
            METER.answered(usage, free=served_fast)
            return text.strip()
        except Exception as exc:
            self._fail_global(f"answer failed: {exc}")
            return ""

    def _answer_call(self, messages, model=None, on_spoken=None, fast=False):
        """Chat call. Returns (text, usage, served_fast).

        `fast` routes to the Groq model on F6 and falls back to the main one on
        any failure, so a machine with no GROQ_API_KEY simply answers normally
        rather than losing the button. The one case it does NOT retry is a
        failure that arrives after speech has already started: a second answer
        talking over the first is worse than a truncated one.

        `served_fast` says who actually answered, not who was asked. The meter
        needs the difference — a fallback runs on the billed model, and
        reporting it as free Groq would quietly understate the session cost.
        """
        if fast and self._groq() is not None:
            spoke = []
            relay = on_spoken
            if on_spoken is not None:
                def relay(text):
                    spoke.append(True)
                    on_spoken(text)
            try:
                text, usage = self._call_once(self._groq(), config.FAST_MODEL,
                                              config.FAST_EFFORT, messages, relay)
                return text, usage, True
            except Exception as exc:
                if spoke:
                    raise
                self._on_event(f"fast answer failed, using "
                               f"{config.ANSWER_MODEL}: {str(exc)[:60]}")
        text, usage = self._call_once(self._client, model or config.ANSWER_MODEL,
                                      config.ANSWER_EFFORT, messages, on_spoken)
        return text, usage, False

    def _call_once(self, client, model, effort, messages, on_spoken=None):
        """One model, one call. Returns (text, usage).

        With `on_spoken`, the response is STREAMED and the callback fires with
        the spoken part the moment it is complete — at the --- marker when the
        answer carries a code block, at stream end otherwise. The point is code
        answers: the sentence you hear is done in under a second while the block
        below the marker can generate for several more, and there is no reason
        for the user's ear to wait on text their eyes will read.

        reasoning_effort is retried without on rejection — it is model-specific,
        and a hard failure here means no answer at all. Tracked per model, since
        F6 and F9 are different models and one rejecting it says nothing about
        the other.
        """

        def create(stream: bool):
            kwargs = {"model": model, "messages": messages}
            if effort and model not in self._no_effort:
                kwargs["reasoning_effort"] = effort
            if stream:
                kwargs["stream"] = True
                # Without this a streamed response reports no token usage and
                # the session cost meter goes quietly blind.
                kwargs["stream_options"] = {"include_usage": True}
            return client.chat.completions.create(**kwargs)

        def run(stream: bool):
            if not stream:
                response = create(False)
                return (response.choices[0].message.content or ""), \
                    getattr(response, "usage", None)
            buffer, fired, usage = "", False, None
            for chunk in create(True):
                if getattr(chunk, "usage", None):
                    usage = chunk.usage
                delta = chunk.choices[0].delta.content if chunk.choices else None
                if not delta:
                    continue
                buffer += delta
                if not fired:
                    # Fire only on a COMPLETE line equal to the marker — the
                    # last line may still be mid-generation, and a substring
                    # test would trip on ---- or a marker still being typed.
                    lines = buffer.splitlines()
                    done = lines[:-1] if not buffer.endswith("\n") else lines
                    if any(l.strip() == config.READ_MARKER for l in done):
                        spoken = _before_marker(buffer)
                        if spoken:
                            fired = True
                            on_spoken(spoken)
            if not fired and buffer.strip():
                on_spoken(_before_marker(buffer))
            return buffer, usage

        try:
            return run(stream=on_spoken is not None)
        except Exception as exc:
            if "reasoning" not in str(exc).lower() or model in self._no_effort:
                raise
            self._no_effort.add(model)
            self._on_event(
                f"{model} rejects reasoning_effort={effort!r}; using the default"
            )
            return run(stream=on_spoken is not None)

    def _fail_global(self, message: str) -> None:
        """A fault that is not any one source's fault (API, network)."""
        self._on_event(f"audio loop error: {message}")
