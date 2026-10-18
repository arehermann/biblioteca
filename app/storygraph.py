"""Parses the CSV that StoryGraph exports (Manage Account > Export StoryGraph Library)."""
import csv
import io
import re


def _col(row: dict, *names: str) -> str:
    lowered = {(k or "").strip().lower(): v for k, v in row.items()}
    for name in names:
        value = lowered.get(name.lower())
        if value:
            return value.strip()
    return ""


def _clean_isbn(raw: str) -> str:
    digits = re.sub(r"[^0-9Xx]", "", raw or "")
    return digits.upper() if len(digits) in (10, 13) else ""


def parse(data: bytes, only_owned: bool = True) -> tuple[list[dict], int]:
    """Returns (books, skipped_not_owned)."""
    text = data.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    books, skipped = [], 0
    for row in reader:
        title = _col(row, "Title")
        if not title:
            continue
        owned = _col(row, "Owned?", "Owned").lower()
        if only_owned and owned not in ("yes", "true", "sí", "si", "1"):
            skipped += 1
            continue
        books.append({
            "title": title,
            "author": _col(row, "Authors", "Author"),
            "isbn": _clean_isbn(_col(row, "ISBN/UID", "ISBN")),
            "source": "storygraph",
        })
    return books, skipped


def has_owned_column(data: bytes) -> bool:
    header = data.decode("utf-8-sig", errors="replace").splitlines()[:1]
    return bool(header) and "owned" in header[0].lower()
