"""The static sales table, held in memory, and the number matching against it.

This stands in for the CRM. Swapping in a live integration means replacing
`load_rows` and nothing else.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path

_NUM_RE = re.compile(r"-?[\d,]*\.?\d+")
_SUFFIX = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000, "bn": 1_000_000_000}


def parse_amount(text: str) -> float | None:
    """'$2.4M' -> 2400000.0, '2,400,000' -> 2400000.0, 'Q3' -> None."""
    if text is None:
        return None
    cleaned = str(text).strip().lower().replace("$", "").replace("¥", "").replace(" ", "")
    match = _NUM_RE.search(cleaned)
    if not match:
        return None
    # A digit glued to a preceding letter is an identifier, not an amount:
    # "Q3", "FY26", "H2". Treating those as money produces silent false matches.
    if match.start() > 0 and cleaned[match.start() - 1].isalpha():
        return None
    try:
        value = float(match.group().replace(",", ""))
    except ValueError:
        return None
    tail = cleaned[match.end() :]
    for suffix, factor in sorted(_SUFFIX.items(), key=lambda kv: -len(kv[0])):
        if tail.startswith(suffix):
            return value * factor
    return value


def load_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        row["_amount"] = parse_amount(row.get("amount", "")) or 0.0
    return rows


def match(numbers: list[dict], rows: list[dict], tolerance: float = 0.02):
    """Find the first on-screen number that lines up with a deal.

    `numbers` is what the screen loop extracted: [{"value": "2.4M", "label": ...}].
    Returns (row, number) or None. Tolerance is relative, so 2.4M matches
    2,400,000 and also a rounded 2.38M.
    """
    for number in numbers:
        value = parse_amount(number.get("value", ""))
        if not value:
            continue
        for row in rows:
            amount = row.get("_amount") or 0.0
            if amount <= 0:
                continue
            if abs(amount - value) <= tolerance * amount:
                return row, number
    return None


def format_money(amount: float | str) -> str:
    value = parse_amount(amount) if isinstance(amount, str) else float(amount)
    if value is None:
        return str(amount)
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}".rstrip("0").rstrip(".") + "M"
    if value >= 1_000:
        return f"{value / 1_000:.0f}K"
    return f"{value:.0f}"
