# Component choices

| Piece | Choice | Rate |
|---|---|---|
| Question transcription | GPT Transcribe | $0.0045/min |
| Meeting transcription | GPT-4o Transcribe Diarization | $0.006/min |
| Screen analysis | GPT-5.6 Luna | $0.20 / $1.20 per 1M |
| Answer generation | GPT-5.6 Luna or Terra | $0.20 / $1.20, $2 / $12 |
| Output | Text overlay, TTS optional | — |
| Trigger | USB footswitch | one-off |

Running cost lands around **$1 to $2 per hour-long meeting**.

## Why diarization for meeting audio

Diarization costs 33% more than plain transcription and adds no separate fee.
Worth it: it turns *"someone asked about the timeline"* into *"the client asked
about the timeline."*

## Rejected: Realtime speech-to-speech API

`gpt-realtime-2.1` runs $32 / $64 per 1M audio tokens and is built for natural
back-and-forth conversation.

This use case wants accuracy, retrieval, and readable output instead. Chained
transcription plus a text model is cheaper, faster to first useful output, and
far easier to hang a knowledge base off.

## Output: speech first, text alongside

**Decision: spoken output is the default.** Reading mid-meeting means looking
away from the room, and the point of this tool is not having to.

The original argument for text-first still holds on pure latency — reading
*"Q3 APAC renewal, 2.4M, owner: Trang"* takes about half a second, and the same
sentence spoken takes six or seven while your coworker keeps talking over it.
Three things buy that back:

- **Answers are cut short for speech.** Capped at ~220 characters and trimmed at
  a sentence boundary. The overlay carries the detail that the spoken line drops.
- **Audio streams as raw PCM.** Playback starts on the first chunk rather than
  after a whole file downloads and decodes.
- **Barge-in.** Pressing the button again kills whatever is still talking. A
  stale answer speaking over a new question is worse than no answer at all.

The overlay stays regardless: it holds the full detail after the sentence ends,
and it shows the latency number the design exists to defend.

> **Pin the output device explicitly to your earbuds.**
> It must never reach the meeting mic. `TTS_DEVICE` in `.env` does this by name
> match; `python -m demo.check_setup` prints the exact device names.
