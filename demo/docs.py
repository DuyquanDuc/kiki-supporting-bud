"""Reference documents, indexed before the meeting and searched at the button.

Drop files into demo/data/docs and the bot can answer from them: a job spec, a
CV, an architecture note, last quarter's numbers, the agenda. Things it cannot
possibly infer from the room.

This fits the project's one principle exactly — do the work before the button.
The documents are known *before* the meeting starts, so all the expensive work
(reading, chunking, embedding) happens at startup and is cached to disk. The
press pays for one small embedding of the question, measured at 250-500ms warm,
and then a local dot product.

Retrieval is by embedding rather than keyword because the meetings are in
Vietnamese, Japanese and English: a question asked in Japanese has to find an
answer written in an English document, and no amount of keyword matching does
that.

Nothing leaves the machine except the text of chunks that actually get used, and
the documents themselves stay in demo/data/docs, which is gitignored.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import config

_TEXT_SUFFIXES = {".txt", ".md", ".markdown", ".csv", ".json", ".yaml", ".yml"}


@dataclass
class Chunk:
    source: str
    text: str


def _read_pdf(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""
    try:
        return "\n".join((page.extract_text() or "") for page in PdfReader(str(path)).pages)
    except Exception:
        return ""


def read_file(path: Path) -> str:
    if path.suffix.lower() == ".pdf":
        return _read_pdf(path)
    if path.suffix.lower() not in _TEXT_SUFFIXES:
        return ""
    for encoding in ("utf-8", "utf-8-sig", "cp932", "latin-1"):
        try:
            return path.read_text(encoding=encoding)
        except (UnicodeDecodeError, OSError):
            continue
    return ""


def _sections(text: str) -> list[str]:
    """Split on markdown headings, which are the author's own topic boundaries.

    Without this a short document becomes a single chunk covering ownership,
    timeline, rollback and traffic numbers all at once. Its embedding then sits
    between all four topics and matches none of them strongly — a question about
    rollback scored 0.30 against the whole note, barely above the noise floor,
    and the same question in Japanese missed entirely.
    """
    blocks, current = [], []
    for line in text.splitlines():
        if line.lstrip().startswith("#") and current:
            blocks.append("\n".join(current).strip())
            current = [line]
        else:
            current.append(line)
    if current:
        blocks.append("\n".join(current).strip())
    return [b for b in blocks if b]


def split(text: str, source: str) -> list[Chunk]:
    """Heading- and paragraph-aware chunks, with a little overlap.

    Overlap matters: a fact split across a boundary is otherwise invisible to
    retrieval, and documents are full of "the deadline is" / "November 14th"
    landing either side of a break.
    """
    chunks: list[Chunk] = []
    for section in _sections(text):
        heading = section.splitlines()[0].strip() if section.lstrip().startswith("#") else ""
        paragraphs = [p.strip() for p in section.split("\n\n") if p.strip()]
        current = ""
        for paragraph in paragraphs:
            if len(current) + len(paragraph) + 2 <= config.DOCS_CHUNK_CHARS:
                current = f"{current}\n\n{paragraph}" if current else paragraph
                continue
            if current:
                chunks.append(Chunk(source, current))
                tail = current[-config.DOCS_CHUNK_OVERLAP:]
                # Repeat the heading: a continuation chunk that has lost "##
                # Rollback" no longer looks like it is about rollback.
                current = f"{heading}\n{tail}\n\n{paragraph}" if heading else \
                          f"{tail}\n\n{paragraph}"
            else:
                for i in range(0, len(paragraph), config.DOCS_CHUNK_CHARS):
                    piece = paragraph[i:i + config.DOCS_CHUNK_CHARS]
                    chunks.append(Chunk(source, f"{heading}\n{piece}" if heading else piece))
                current = ""
        if current.strip():
            chunks.append(Chunk(source, current))
    return chunks


class DocStore:
    """Indexes demo/data/docs and answers similarity queries against it."""

    def __init__(self, client, on_event=None):
        self._client = client
        self._on_event = on_event or (lambda _m: None)
        self._chunks: list[Chunk] = []
        self._vectors: np.ndarray | None = None
        self._lock = threading.Lock()
        self.error = ""

    # --- indexing ----------------------------------------------------------

    def load(self) -> None:
        """Read, chunk and embed everything. Safe to call on a worker thread."""
        directory = config.DOCS_DIR
        if not directory.exists():
            return
        files = sorted(
            p for p in directory.iterdir()
            if p.is_file() and not p.name.startswith(".")
            and p.suffix.lower() in _TEXT_SUFFIXES | {".pdf"}
        )
        if not files:
            return

        chunks: list[Chunk] = []
        skipped: list[str] = []
        for path in files:
            text = read_file(path)
            if not text.strip():
                skipped.append(path.name)
                continue
            chunks.extend(split(text, path.name))
        if skipped:
            self._on_event(
                f"docs: could not read {', '.join(skipped)}"
                + (" (PDF support needs `pip install pypdf`)"
                   if any(s.lower().endswith('.pdf') for s in skipped) else "")
            )
        if not chunks:
            return

        cached = self._read_cache()
        vectors, fresh = [], []
        for chunk in chunks:
            key = _key(chunk)
            if key in cached:
                vectors.append(cached[key])
            else:
                vectors.append(None)
                fresh.append((len(vectors) - 1, chunk))

        if fresh:
            self._on_event(f"docs: embedding {len(fresh)} new chunk(s)...")
            try:
                for start in range(0, len(fresh), 64):
                    batch = fresh[start:start + 64]
                    response = self._client.embeddings.create(
                        model=config.EMBED_MODEL,
                        input=[c.text for _i, c in batch],
                    )
                    for (index, chunk), item in zip(batch, response.data):
                        vector = np.asarray(item.embedding, dtype=np.float32)
                        vectors[index] = vector
                        cached[_key(chunk)] = vector
            except Exception as exc:
                self.error = f"could not embed documents: {exc}"
                self._on_event(f"docs: {self.error}")
                return
            self._write_cache(cached)

        matrix = np.vstack([v for v in vectors if v is not None])
        # Pre-normalise so a search is one dot product, not a division per row.
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        matrix = matrix / np.clip(norms, 1e-9, None)
        with self._lock:
            self._chunks = chunks
            self._vectors = matrix
        self._on_event(
            f"docs: {len(files)} file(s), {len(chunks)} chunks ready"
            + (" (from cache)" if not fresh else "")
        )

    # --- search ------------------------------------------------------------

    def ready(self) -> bool:
        with self._lock:
            return self._vectors is not None and len(self._chunks) > 0

    def search(self, query: str, k: int | None = None) -> list[tuple[float, Chunk]]:
        """Top matching chunks for `query`, best first. Costs one embedding."""
        if not query.strip() or not self.ready():
            return []
        k = k or config.DOCS_TOP_K
        try:
            response = self._client.embeddings.create(
                model=config.EMBED_MODEL, input=query[:4000]
            )
        except Exception as exc:
            self._on_event(f"docs: search failed: {exc}")
            return []
        vector = np.asarray(response.data[0].embedding, dtype=np.float32)
        vector /= max(float(np.linalg.norm(vector)), 1e-9)
        with self._lock:
            scores = self._vectors @ vector
            order = np.argsort(-scores)[:k]
            hits = [(float(scores[i]), self._chunks[i]) for i in order]
        # A weak best match means the documents have nothing to say about this.
        # Passing them anyway invites the model to force a connection.
        return [(s, c) for s, c in hits if s >= config.DOCS_MIN_SCORE]

    def context(self, query: str) -> str:
        """Retrieved chunks formatted for a prompt, or "" if nothing is relevant."""
        hits = self.search(query)
        if not hits:
            return ""
        parts = [f"[{chunk.source}]\n{chunk.text}" for _score, chunk in hits]
        return "\n\n".join(parts)

    # --- cache -------------------------------------------------------------

    def _read_cache(self) -> dict[str, np.ndarray]:
        meta, vectors = config.DOCS_CACHE_META, config.DOCS_CACHE_VECTORS
        if not meta.exists() or not vectors.exists():
            return {}
        try:
            keys = json.loads(meta.read_text(encoding="utf-8"))
            data = np.load(vectors)["v"]
            if len(keys) != len(data):
                return {}
            return {key: data[i] for i, key in enumerate(keys)}
        except Exception:
            return {}

    def _write_cache(self, cached: dict[str, np.ndarray]) -> None:
        try:
            config.DOCS_DIR.mkdir(parents=True, exist_ok=True)
            keys = list(cached)
            config.DOCS_CACHE_META.write_text(json.dumps(keys), encoding="utf-8")
            np.savez_compressed(
                config.DOCS_CACHE_VECTORS, v=np.vstack([cached[k] for k in keys])
            )
        except Exception as exc:
            self._on_event(f"docs: could not write cache: {exc}")


def _key(chunk: Chunk) -> str:
    """Content hash, so editing one file does not re-embed the others."""
    digest = hashlib.sha256()
    digest.update(chunk.source.encode("utf-8"))
    digest.update(b"\0")
    digest.update(chunk.text.encode("utf-8"))
    return digest.hexdigest()
