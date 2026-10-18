"""Parses library CSV exports from StoryGraph (Manage Account > Export StoryGraph Library)
and Goodreads (My Books > Import and export > Export Library)."""
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


def _isbn(row: dict) -> str:
    # Goodreads writes ISBNs as ="0441478123" and often leaves ISBN empty while ISBN13 is set.
    for name in ("ISBN/UID", "ISBN13", "ISBN"):
        isbn = _clean_isbn(_col(row, name))
        if isbn:
            return isbn
    return ""


def _is_owned(row: dict) -> bool:
    owned = _col(row, "Owned?", "Owned", "Owned Copies").lower()
    return owned in ("yes", "true", "sí", "si") or (owned.isdigit() and int(owned) > 0)


def parse(data: bytes, only_owned: bool = True) -> tuple[list[dict], int]:
    """Returns (books, skipped_not_owned)."""
    text = data.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    source = "goodreads" if "book id" in [(f or "").strip().lower() for f in reader.fieldnames or []] else "storygraph"
    books, skipped = [], 0
    for row in reader:
        title = _col(row, "Title")
        if not title:
            continue
        if only_owned and not _is_owned(row):
            skipped += 1
            continue
        books.append({
            "title": title,
            "author": _col(row, "Authors", "Author"),
            "isbn": _isbn(row),
            "source": source,
        })
    return books, skipped


def has_owned_column(data: bytes) -> bool:
    header = data.decode("utf-8-sig", errors="replace").splitlines()[:1]
    return bool(header) and "owned" in header[0].lower()
