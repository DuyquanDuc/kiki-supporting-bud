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

Sent whole with every answer, and re-read on every press — so editing a file, or
dropping a new one in, takes effect on the next button press with no restart.

Keep it small. The full text goes with every answer, so this is right for a few
pages and wrong for a library; past ~20,000 characters the app says so.
