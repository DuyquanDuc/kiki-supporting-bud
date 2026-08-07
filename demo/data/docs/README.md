# Reference documents

Drop files here before a meeting and the bot can answer from them — a job spec,
your CV, an architecture note, last quarter's numbers, the agenda. Things it
cannot possibly infer from the room or the screen.

**This folder is gitignored.** Your documents stay on your machine; only the
passages that actually match a question are ever sent, and only at the moment
you press.

**Only `.txt` and `.md`.** Anything else is reported at startup and skipped —
export or save it as text first. A PDF or Word file dropped in here will say so
in the log rather than quietly doing nothing.

Indexed at startup and cached, so a restart with unchanged files costs nothing.
Edit a file and only that file is re-embedded. The press itself costs one small
embedding of the question — measured at 250-500ms.

Keep it relevant rather than exhaustive. Four passages are attached to any one
answer, so a folder of everything you own competes with itself.
