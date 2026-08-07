"""Background meeting-audio loop: capture, chunk, transcribe.

Loop 2 of the three in docs/architecture.md. Listens on **two** sources at once
and merges them into one rolling 15-minute transcript, tagged by who spoke:

    Them  the system output mix (loopback) — your coworkers' voices
    You   your microphone — what you said

Both halves matter. Loopback alone cannot hear you answer, clarify, or commit to
anything, so the bot loses the half of the conversation it is meant to support
you through. And they fail independently: a machine where Stereo Mix is disabled
still has a working mic, so one dead source degrades the transcript instead of
killing it.

An earlier version also pre-answered: any chunk that looked like a question got
an answer composed in the background, ready for an instant press. It was removed.
The press-time flush transcribes the last ten seconds, and *any* fresh speech
invalidates a parked answer — so in a live meeting the fast path almost never
fired, while a model call was spent on every question detected, most never
served. It also filled the log with "pre-answered ..." for answers nobody heard.

Two gates keep this cheap and honest:

1. **Silence gate.** Chunks below an RMS floor are dropped without a call.
   Meetings are mostly not talking, and transcription models are notorious for
   inventing text out of silence.
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

import numpy as np

from . import config, docs as documents

THEM = "Them"
YOU = "You"

_READ_RULE = """SOMETHING TO LOOK AT. A few answers cannot be heard, only read:
code, a command, an exact identifier or path, a precise string. For those, and
only those, write the spoken sentence first, then a line containing only ---,
then the thing to read:

    Use a HashMap and merge counts as you go.
    ---
    Map<String, Integer> counts = new HashMap<>();
    counts.merge(word, 1, Integer::sum);

Everything above --- is spoken and must still obey the length rule on its own.
Everything below is printed for them to read, is not spoken, and is exempt from
the length rule — but keep it to the smallest complete thing that answers the
question, not a tutorial.

Use this only when seeing it genuinely matters. An ordinary answer gets no ---
block; a number, a name, or a yes/no never needs one."""

_LENGTH_RULE = """LENGTH IS THE FAILURE MODE. One sentence, 15-25 words. Two
only if the question genuinely has two halves. This is spoken into someone's ear
while the meeting keeps going, so every extra word costs them the next thing said
in the room.

Front-load it: the answer in the first few words, before any qualifier. If they
stop listening after five words they should already have what they needed.

Cut hedging ("it seems", "generally", "typically", "I think"). Cut qualifiers
that change nothing. Cut anything they can infer. No lists, no "firstly", no
closing summary. Answer in the language being spoken. Plain speech, no markup."""

_TONE_RULE = """This is whispered to ONE person who is listening, not repeating
it. Nobody else will ever hear your words. So this is not a script and not a
line to be said — it is a cue, and the only thing that matters is how fast it
lands on a single listen while the user is also following the room.

That means: strip the packaging, keep the substance. No politeness scaffolding,
no framing, no full-sentence grammar for its own sake. "Asahi, 5.4 million,
still in discovery" beats "The Asahi Group opportunity is valued at 5.4 million
and remains in the discovery stage" — same facts, half the listening.

Talk like a sharp colleague leaning over to help, not like a textbook. Use
contractions. Use the everyday word over the formal one when both work. No
semicolons, no "thus", "therefore", "moreover", "in addition" — those slow an
ear down. Where a textbook would define a thing, say what it does or why it
matters instead.

Natural, not padded. Do not open with filler — no "So basically", no "Well, I
think", no "Great question". Warmth costs nothing; throat-clearing costs a
second of a conversation that is still moving.

When the question is yes/no, start with the actual yes or no. "Probably not",
"Yeah", "Not quite" — that is what front-loading sounds like in speech.

Match these. The left column is what you must not produce:

  TEXTBOOK: "Stack memory stores method calls and local variables; heap memory
            stores objects and is managed by garbage collection."
  SPOKEN:   "Stack holds your local variables and method calls, heap is where
            objects live — and that's the part that gets garbage collected."

  TEXTBOOK: "Dependency injection means providing an object with its
            dependencies externally rather than having it construct them."
  SPOKEN:   "You hand an object what it needs instead of letting it build its
            own stuff — makes it way easier to swap and test."

  TEXTBOOK: "The November deadline is at risk because the authentication
            migration has slipped by two weeks."
  SPOKEN:   "Probably not — auth slipped a couple of weeks, so November's tight."

  TEXTBOOK: "Asahi Group is in the Discovery stage, valued at 5.4 million."
  SPOKEN:   "That's Asahi, 5.4 million, still in discovery."

Notice what changes: no semicolons, contractions throughout, dashes where you
would pause, the definition replaced by what the thing actually does — and not
one word spent on being a well-formed answer.

The same applies in the other languages — natural speech, technical terms left
in English exactly as engineers say them:

  TEXTBOOK: "Bộ nhớ stack lưu trữ các lời gọi phương thức và các biến cục bộ,
            trong khi bộ nhớ heap lưu trữ các đối tượng."
  CUE:      "Stack giữ biến local với lời gọi hàm. Heap chứa object, và heap
            mới bị garbage collect."

  TEXTBOOK: 「スタックメモリはメソッド呼び出しおよびローカル変数を格納し、
            ヒープメモリはオブジェクトを格納いたします。」
  CUE:      「スタックはローカル変数とメソッド呼び出し。オブジェクトはヒープで、
            GC 対象もヒープ側」"""

# The user works across Vietnamese, Japanese and English, often inside the same
# meeting. Left to itself a model drifts to English, translates technical terms
# nobody translates out loud, and — worst — carries the "be conversational"
# instruction into Japanese as plain form, which in a client meeting is a real
# register mistake the user would then repeat.
_LANGUAGE_RULE = f"""The user works in Vietnamese, Japanese and English, and
meetings mix them.

Answer in the language the question was asked in — that is the language the user
is thinking in right now, and making them translate costs them the room. If a
question mixes languages, use whichever one it is mostly in.

Keep technical terms in the form they were spoken. Vietnamese and Japanese
engineers say "deploy", "commit", "deadline", "stack", "heap", "review" in
English inside their own sentences. Translating those into native equivalents
makes the user stop and decode a term they already knew.

Because the user only listens and never repeats this, politeness forms are
wasted breath in every language:

JAPANESE: plain, compact です・ます or bare noun phrases — whatever is shortest to
take in. No 敬語, no 恐れ入りますが, no softening. 「スタックはローカル変数、ヒープは
オブジェクト。GC 対象はヒープ側」is ideal: dense, instantly parsed.

VIETNAMESE: drop pronouns entirely. Vietnamese lets you, they carry status the
bot has no business guessing at, and nobody needs to be addressed — the user is
just listening. No "dạ", no "ạ", no anh/chị/em.

The 15-25 word target is English. Match the listening duration, not the word
count — Japanese in particular says the same thing in far fewer characters."""

_SPEAKERS_RULE = f"""The transcript is tagged by speaker. "{THEM}:" is other
people in the meeting. "{YOU}:" is the user you are helping — treat those lines
as their own words, so do not tell them something they just said themselves, and
if they already answered, do not contradict them without reason."""

# F9. Deliberately blind to the screen: this is the "what was just said" button,
# and mixing in screen context makes it answer a question nobody asked.
#
# This prompt answers questions and does nothing else. An earlier version fell
# back to summarising the discussion when it found no question, and that became
# the behaviour you got most of the time — the user pressed the button mid-
# meeting and was read a recap of a conversation they had just sat through.
_ANSWER_PROMPT = f"""Someone in a meeting just asked a question out loud and the
user needs the answer in their ear immediately. You get a transcript of what was
said. You cannot see their screen.

{_SPEAKERS_RULE}

Your first job is to ANSWER THE MOST RECENT QUESTION. When there is a question,
answer it and nothing else — do not also summarise, do not set the scene.

ONLY THE LAST ONE. JUST SAID may contain several questions in a row, because
people ask "and the totals for A and B?" then "and C and D?" seconds apart.
Answer the FINAL question only. Never roll them together into one reply covering
all of them — that answers a question nobody asked and buries the one they did.

ALREADY ANSWERED lists questions you have just answered out loud. Those are
settled. They are there so a follow-up still makes sense — "and C and D?" is
meaningless without the "A and B" that came before it — but repeating any part
of them wastes the seconds the user has. If the new question is the same one
again, they did not hear you: answer it again, differently and shorter.

The same words may appear more than once: speech is transcribed in overlapping
passes, so a repeated line is one utterance heard twice, not someone asking
twice. Treat it as one.

A line marked [rough re-transcription of the last few seconds] is the newest
audio, but it was transcribed in one long pass and garbles names, letters and
identifiers that the shorter passes got right. When it and an ordinary line
clearly describe the same utterance, take the wording from the ordinary line —
"シナリオCとシナリオD" over "シナリオシート、シナリオリー", "ETL" over "ETR".
Use the rough line for what is new in it, not for how it spells things.

- Lead with the answer itself. First words are the substance.
- Never restate or paraphrase the question. They just heard it.
- Never recap, summarise, or describe the conversation. They were in it.
- No preamble. Not "it sounds like", not "they're asking about", not "based on
  the transcript". Just the answer.
- Older lines are background for understanding the question. Do not answer them.

CORRECTIONS. Speech-to-text mangles acronyms and names, and people correct
themselves out loud. If the latest lines fix or sharpen an earlier question —
"no, ETL, not ETA", "sorry, I meant the staging one", or simply the same term
said again differently — then the question is the CORRECTED one. Answer that.
Do not reply "No question asked" merely because the correction itself is not
phrased as a question, and do not keep answering the version you saw first.

When a term appears in several near-identical forms across the transcript (ETA,
EPA, ETR, ETL), they are one word the transcriber heard badly. Pick the reading
that makes sense with the surrounding words — "data engineer" next to it makes
ETL right and ETA wrong — and answer that. If two readings are genuinely
plausible and mean different things, say which one you assumed in two words.

If NO question has been asked, catch them up instead: say what is being
discussed and where it has got to. The current state, not a history — the point
being argued, the number on the table, the thing that was decided. If someone is
waiting on the user for something, lead with that. Same length limit; this is
for someone who looked away for a minute, not a recap of the meeting.

If a question was asked but the transcript does not contain what is needed to
answer it, say in a few words what is missing — and if the answer would be on
screen, say "check the screen" so they know to press the other button.

Ground everything in the transcript. Never invent a figure, a name, or a
commitment on the user's behalf.

{_LENGTH_RULE}

{_READ_RULE}

{_TONE_RULE}"""

# F10. Same job, but with the actual pixels — so it can read a figure the screen
# loop's one-line summary never bothered to record.
_FULL_PROMPT = f"""The user is in a meeting and needs help right now. You get a
screenshot of what is on their screen, and the recent transcript of what has been
said out loud. Either may be the important one.

{_SPEAKERS_RULE}

THE SCREEN USUALLY CONTAINS THE QUESTION. A coding problem, an exercise, a
failing test, a stack trace, a form, a diff waiting on review — those are asks,
not scenery. Solve what is on screen. Describing it back is the one thing the
user cannot use: they are looking at it.

In priority order:

1. Someone asked something out loud — answer that, using the screen to ground it.
2. Nothing was said, but the screen poses a problem — SOLVE IT. A coding problem
   gets working code. An error gets the fix. A question gets its answer. Use the
   --- block so the code or command is there to read.
3. Only if the screen poses nothing at all — no task, no error, no question —
   say what it shows and the point it is making.

Never answer a coding problem by restating the problem. "The screen shows a Java
problem: move zeros to the right" is a failure; the user can read that. Give the
approach in one spoken line and the code below the marker.

Read the screenshot carefully — exact figures, labels, names, error text, method
signatures, the language it is written in. Match the language and style already
on screen. Never invent anything that is not in the image or the transcript.

{_LENGTH_RULE}

{_READ_RULE}

{_TONE_RULE}"""


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


def _overlap(a: str, b: str) -> float:
    """How much of the shorter string's character trigrams appear in the other.

    Character trigrams rather than words because the transcript is Vietnamese,
    Japanese and English: Japanese has no spaces to split on, and a repeated
    question is never word-identical anyway ("what is ETL" vs "so can you explain
    what ETL is"). Containment, not Jaccard, so a longer restatement of a short
    question still scores high.
    """
    def grams(text: str) -> set[str]:
        squashed = re.sub(r"[\s\.,!?？。、]+", "", text.lower())
        return {squashed[i:i + 3] for i in range(max(0, len(squashed) - 2))}

    first, second = grams(a), grams(b)
    if not first or not second:
        return 0.0
    return len(first & second) / min(len(first), len(second))


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
    device: str = ""
    api: str = ""
    samplerate: int = 48_000
    frames: int = 0
    level: float = 0.0
    chunks: int = 0
    error: str = ""
    floor: float = field(default_factory=lambda: config.AUDIO_SILENCE_RMS)
    # The partial chunk still filling. Held here rather than as a local in the
    # capture thread so the button can cut it short — the words you just heard
    # live in this buffer and nowhere else.
    buf: list = field(default_factory=list)
    buf_frames: int = 0
    buf_lock: threading.Lock = field(default_factory=threading.Lock)
    # A rolling window of the last FLUSH_LOOKBACK_SECONDS, kept regardless of
    # where chunk boundaries fell. This is what the button transcribes, so a
    # question split across a boundary is still one coherent clip.
    lookback: deque = field(default_factory=deque)
    lookback_frames: int = 0

    def remember(self, block) -> None:
        """Keep `block` in the rolling window, evicting whatever aged out."""
        self.lookback.append(block)
        self.lookback_frames += len(block)
        limit = int(config.FLUSH_LOOKBACK_SECONDS * self.samplerate)
        while self.lookback and self.lookback_frames > limit:
            self.lookback_frames -= len(self.lookback.popleft())

    def recent_audio(self, minimum_seconds: float = 0.0):
        """The rolling window as one array, or None if too short to be worth a call."""
        import numpy as _np

        with self.buf_lock:
            if self.lookback_frames < minimum_seconds * self.samplerate:
                return None
            if not self.lookback:
                return None
            return _np.concatenate(list(self.lookback))

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
        self.sources = [
            Source(label, candidates if not isinstance(candidates, int)
                   else [(candidates, f"device {candidates}", "pinned")])
            for label, candidates in sources
        ]
        # A callable, not a value: it changes constantly and the loop must read
        # it at the moment it needs it, not at construction.
        self._is_muted = is_muted or (lambda: False)
        self._on_event = on_event or (lambda _m: None)

        self._halt = threading.Event()
        self._lock = threading.Lock()
        self._transcript: deque[tuple[float, str, str]] = deque()
        # Bounded on purpose: if transcription falls behind, drop the oldest
        # audio rather than grow a backlog that answers questions from minutes ago.
        self._work: "queue.Queue" = queue.Queue(maxsize=6)
        self._worker: threading.Thread | None = None
        self.chunks_transcribed = 0
        self.chunks_processed = 0
        # The press-time transcription of the rolling window. Held apart from the
        # transcript because it overlaps it by design.
        self._tail: tuple = ()
        self._tail_at = 0.0
        # Questions already answered, with what was said. This is what lets the
        # focus window stay wide: earlier questions are marked as dealt with
        # rather than hidden, so a follow-up keeps the context it needs without
        # the answer folding the old questions back in.
        self._history: deque[tuple[str, str]] = deque(maxlen=config.ANSWER_HISTORY)
        self._last_query = ""

    # --- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        self._worker = threading.Thread(target=self._transcribe_worker, daemon=True)
        self._worker.start()
        for source in self.sources:
            source.thread = threading.Thread(
                target=self._capture, args=(source,), daemon=True,
                name=f"audio-{source.label}",
            )
            source.thread.start()

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

        # The press-time clip is the newest audio, so it goes last — but it does
        # NOT get to overwrite the transcript. It re-transcribes a long window in
        # one go and is measurably worse on names: a chunk rendered
        # "シナリオCとシナリオD" correctly while the same speech in the flush came
        # back "シナリオシート、シナリオリー", and letting the flush win meant
        # answering the garbled version.
        tail = self._tail_text() if (now - self._tail_at) < 20 else ""
        if tail:
            with self._lock:
                rows = [(s, l, t) for s, l, t in self._transcript if s >= old_cut]
            # Drop only the rows the clip genuinely restates, not everything in
            # the window. Otherwise a good transcription is thrown away for a
            # worse one covering the same seconds.
            kept = [(s, l, t) for s, l, t in rows if _overlap(t, tail) < 0.6]
            focus_rows = [(s, l, t) for s, l, t in kept if s >= focus_cut]
            background = "\n".join(f"{l}: {t}" for s, l, t in kept if s < focus_cut)
            focus = "\n".join(f"{l}: {t}" for _s, l, t in focus_rows)
            # Flagged, not trusted blindly. The clip is newest but transcribed in
            # one long pass, which mangles names and letters that the shorter
            # chunks got right.
            marked = f"[rough re-transcription of the last few seconds] {tail}"
            return background, f"{focus}\n{marked}".strip() if focus else marked

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

    def has_transcript(self) -> bool:
        with self._lock:
            return bool(self._transcript)

    def flush_and_wait(self, timeout: float = 6.0) -> bool:
        """Transcribe the last few seconds right now, as one clip.

        This is the only way to answer a question you have *just* heard. Chunks
        fill on a fixed cadence, so at the instant you press, the question exists
        only as raw samples. Nothing computed in the background can reach them.

        It re-transcribes the rolling window rather than only the unsent
        remainder, because chunk boundaries fall mid-sentence and short fragments
        hallucinate. The result is kept separate from the background transcript —
        it deliberately overlaps it, and appending would double every line.

        Returns True if it produced anything.
        """
        jobs = []
        for source in self.sources:
            if not source.alive:
                continue
            audio = source.recent_audio(config.FLUSH_MIN_SECONDS)
            if audio is None:
                continue
            if float(np.sqrt(np.mean(np.square(audio)))) < source.floor:
                continue  # the window is silence; nothing was said
            jobs.append((source, audio))
        if not jobs:
            self._tail = ()
            return False

        # Straight to the API, not through the work queue: the queue is behind
        # whatever the background cadence is doing, and this press is waiting.
        lines = []
        deadline = time.monotonic() + timeout
        for source, audio in jobs:
            if time.monotonic() > deadline:
                break
            text = self._transcribe(audio, source.samplerate, source.label)
            if text:
                lines.append((source.label, text))
        self._tail = tuple(lines)
        if lines:
            self._tail_at = time.monotonic()
            for label, text in lines:
                self._on_event(f"just said [{label}]: {text[:70]}")
        return bool(lines)

    def _tail_text(self) -> str:
        if not self._tail:
            return ""
        return "\n".join(f"{label}: {text}" for label, text in self._tail)

    def answer_now(self) -> str:
        """Answer live from the transcript, for a press we did not see coming.

        Every press comes through here: flush the last few seconds, then answer
        from the transcript.
        """
        return self._compose()

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

    def answer_with_screenshot(self, image) -> str:
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
            response = self._client.chat.completions.create(
                model=config.VISION_MODEL,
                messages=[
                    {"role": "system", "content": _system_prompt(_FULL_PROMPT)},
                    {"role": "user", "content": content},
                ],
            )
            return (response.choices[0].message.content or "").strip()
        except Exception as exc:
            self._fail_global(f"full answer failed: {exc}")
            return ""

    # --- capture ------------------------------------------------------------

    def _capture(self, source: Source) -> None:
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
        min_frames = int(source.samplerate * config.AUDIO_CHUNK_MIN_SECONDS)
        max_frames = int(source.samplerate * config.AUDIO_CHUNK_MAX_SECONDS)
        pause_frames = int(source.samplerate * config.AUDIO_PAUSE_SECONDS)
        quiet_frames = 0

        try:
            self._on_event(
                f"audio [{source.label}] listening — {source.device} via {api}, "
                f"{source.samplerate}Hz, cutting at pauses "
                f"({config.AUDIO_CHUNK_MIN_SECONDS:.0f}-{config.AUDIO_CHUNK_MAX_SECONDS:.0f}s)"
            )
            while not self._halt.is_set():
                try:
                    block = blocks.get(timeout=0.5)
                except queue.Empty:
                    continue
                source.frames += len(block)
                block_rms = float(np.sqrt(np.mean(np.square(block))))
                source.level = max(source.level * 0.85, block_rms)
                # Track how long it has been quiet, so a chunk can end where the
                # speaker did rather than wherever the clock happened to land.
                if block_rms < source.floor:
                    quiet_frames += len(block)
                else:
                    quiet_frames = 0

                ready = None
                with source.buf_lock:
                    source.remember(block)
                    source.buf.append(block)
                    source.buf_frames += len(block)
                    at_pause = (
                        source.buf_frames >= min_frames and quiet_frames >= pause_frames
                    )
                    if at_pause or source.buf_frames >= max_frames:
                        ready = np.concatenate(source.buf)
                        source.buf = []
                        source.buf_frames = 0
                if ready is not None:
                    quiet_frames = 0
                    self._submit(source, ready)
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

    def _submit(self, source: Source, chunk: np.ndarray) -> bool:
        source.chunks += 1
        rms = float(np.sqrt(np.mean(np.square(chunk))))
        if rms < source.floor:
            return False  # nothing said, no call, no cost, no invented transcript
        try:
            self._work.put_nowait((source.label, chunk, source.samplerate))
            return True
        except queue.Full:
            self._on_event(f"audio [{source.label}] behind — dropped a chunk")
            return False

    # --- transcription -----------------------------------------------------

    def _transcribe_worker(self) -> None:
        while not self._halt.is_set():
            try:
                item = self._work.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:
                return
            label, chunk, samplerate = item
            text = self._transcribe(chunk, samplerate, label)
            if not text:
                self.chunks_processed += 1
                continue
            now = time.monotonic()
            with self._lock:
                self._transcript.append((now, label, text))
                cutoff = now - config.TRANSCRIPT_WINDOW_MINUTES * 60
                while self._transcript and self._transcript[0][0] < cutoff:
                    self._transcript.popleft()
            self.chunks_transcribed += 1
            self.chunks_processed += 1
            self._on_event(f"heard [{label}]: {text[:70]}")

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

    def _transcribe(self, chunk: np.ndarray, samplerate: int, label: str) -> str:
        try:
            audio = self._to_wav(chunk, samplerate)
            request = {
                "model": config.TRANSCRIBE_MODEL,
                "file": audio,
                "response_format": "text",
            }
            vocabulary = self._vocabulary()
            if vocabulary:
                request["prompt"] = vocabulary
            response = self._client.audio.transcriptions.create(**request)
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

    def _compose(self, question: str = "") -> str:
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
            response = self._client.chat.completions.create(
                model=config.ANSWER_MODEL,
                messages=[
                    {"role": "system", "content": _system_prompt(_ANSWER_PROMPT)},
                    {"role": "user", "content": "\n\n".join(parts)},
                ],
            )
            return (response.choices[0].message.content or "").strip()
        except Exception as exc:
            self._fail_global(f"answer failed: {exc}")
            return ""

    def _fail_global(self, message: str) -> None:
        """A fault that is not any one source's fault (API, network)."""
        self._on_event(f"audio loop error: {message}")
