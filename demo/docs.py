"""Reference documents, handed to the model whole.

Drop .txt or .md files into demo/data/docs and their full text goes into every
answer prompt: a job spec, an architecture note, a glossary, the agenda. Things
the bot cannot possibly infer from the room or the screen.

Deliberately not retrieval. An earlier version embedded chunks and searched them
per press, which worked — but for the handful of small documents one person
brings to a meeting it bought nothing and cost plenty:

  - An embedding round trip on every press, 250-500ms, for a corpus small enough
    to fit in the prompt outright.
  - A relevance threshold that can be wrong, silently dropping the one passage
    that mattered.
  - An index to build, cache, invalidate and re-embed on every edit.

Reading the whole thing costs no network call, cannot retrieve the wrong
passage, and is re-read on every press — so a file dropped in mid-meeting is
picked up on the next button press with no restart.

The trade-off is tokens: every answer carries the full text. That is fine for a
few pages and wasteful for a library, so DOCS_MAX_CHARS warns when the folder
has grown past the point where this is the right design.

Nothing leaves the machine except what is sent with a question, and the folder
itself is gitignored.
"""

from __future__ import annotations

from pathlib import Path

from . import config

# Plain text and markdown only. Everything else — PDF, Word, slides — needs a
# parser that is a dependency, a failure mode and a source of mangled text, for
# formats you can export in seconds. Other files are named at startup rather
# than silently ignored.
SUFFIXES = {".txt", ".md", ".markdown"}


def read_file(path: Path) -> str:
    # cp932 before latin-1: a Japanese note saved by a Windows editor is far
    # more likely here than a latin-1 one, and latin-1 never fails, so putting
    # it earlier would silently turn Japanese into mojibake.
    for encoding in ("utf-8", "utf-8-sig", "cp932", "latin-1"):
        try:
            return path.read_text(encoding=encoding)
        except (UnicodeDecodeError, OSError):
            continue
    return ""


def files() -> tuple[list[Path], list[str]]:
    """(readable documents, names of files that were skipped)."""
    directory = config.DOCS_DIR
    if not directory.exists():
        return [], []
    present = [
        p for p in sorted(directory.iterdir())
        if p.is_file() and not p.name.startswith(".") and p.name != "README.md"
    ]
    keep = [p for p in present if p.suffix.lower() in SUFFIXES]
    skipped = [p.name for p in present if p.suffix.lower() not in SUFFIXES]
    return keep, skipped


def load(on_event=None) -> str:
    """Every document as one block, headed by file name. "" if there are none.

    Called on each press. That is a few small file reads against a network round
    trip, so the cost is noise — and it means editing a document, or dropping a
    new one in, takes effect on the very next press.
    """
    documents, skipped = files()
    if skipped and on_event:
        on_event(
            f"docs: ignoring {', '.join(skipped)} — only .txt and .md are read. "
            "Save or export as text and it will be picked up."
        )
    if not documents:
        return ""

    parts = []
    for path in documents:
        text = read_file(path).strip()
        if not text:
            if on_event:
                on_event(f"docs: {path.name} is empty or unreadable")
            continue
        parts.append(f"--- {path.name} ---\n{text}")
    blob = "\n\n".join(parts)

    if on_event and len(blob) > config.DOCS_MAX_CHARS:
        on_event(
            f"docs: {len(blob):,} characters across {len(parts)} file(s) — this "
            f"is sent with every answer, so past ~{config.DOCS_MAX_CHARS:,} it "
            "starts costing real tokens and slowing replies. Trim to what this "
            "meeting actually needs."
        )
    return blob


def summary() -> str:
    """One line for the startup log."""
    documents, _skipped = files()
    if not documents:
        return "docs: none — drop .txt or .md files into demo/data/docs"
    size = sum(len(read_file(p)) for p in documents)
    return f"docs: {len(documents)} file(s), {size:,} characters, sent with every answer"
