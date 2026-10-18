import json
import os
import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from pathlib import Path

DATA_DIR = Path(os.environ.get("BIBLIOTECA_DATA", "./data"))
LIBRARIES = ("Principal", "Niños")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS books (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    author TEXT NOT NULL DEFAULT '',
    isbn TEXT NOT NULL DEFAULT '',
    publisher TEXT NOT NULL DEFAULT '',
    year INTEGER,
    cover_url TEXT NOT NULL DEFAULT '',
    shelf TEXT NOT NULL DEFAULT '',
    library TEXT NOT NULL DEFAULT 'Principal',
    notes TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'manual',
    match_key TEXT NOT NULL DEFAULT '',
    added_by TEXT NOT NULL DEFAULT '',
    cover_checked INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS books_match_key ON books(match_key);
CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY,
    shelf TEXT NOT NULL DEFAULT '',
    library TEXT NOT NULL DEFAULT 'Principal',
    items TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

BOOK_FIELDS = ("title", "author", "isbn", "publisher", "year", "cover_url", "shelf", "library", "notes", "source")

# Columns added after the first release; init_db adds them to existing databases.
MIGRATIONS = {
    "books": {"library": "TEXT NOT NULL DEFAULT 'Principal'"},
    "scans": {"library": "TEXT NOT NULL DEFAULT 'Principal'"},
}


def db_path() -> Path:
    return DATA_DIR / "biblioteca.db"


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)
        for table, columns in MIGRATIONS.items():
            existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            for name, decl in columns.items():
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        conn.execute("CREATE INDEX IF NOT EXISTS books_library ON books(library)")


def clean_library(value: str | None) -> str:
    return value if value in LIBRARIES else LIBRARIES[0]


@contextmanager
def connect():
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9 ]+", " ", text)


def match_key(title: str, author: str) -> str:
    """Key used to spot duplicates: folded title (no subtitle, no leading article) + author surname."""
    t = _fold((title or "").split(":")[0])
    t = re.sub(r"^(the|a|an|el|la|los|las|un|una|lo)\s+", "", t.strip())
    t = " ".join(t.split())
    first_author = re.split(r",|;| y | and |&", author or "")[0]
    surname = (_fold(first_author).split() or [""])[-1]
    return f"{t}|{surname}"


def add_book(conn, book: dict, added_by: str = "") -> int:
    values = {f: book.get(f) for f in BOOK_FIELDS}
    for f in BOOK_FIELDS:
        if f != "year" and values[f] is None:
            values[f] = "manual" if f == "source" else ""
    values["title"] = values["title"].strip()
    values["author"] = values["author"].strip()
    values["library"] = clean_library(values["library"])
    cur = conn.execute(
        "INSERT INTO books (title, author, isbn, publisher, year, cover_url, shelf, library, notes, source, match_key,"
        " added_by) VALUES (:title, :author, :isbn, :publisher, :year, :cover_url, :shelf, :library, :notes, :source,"
        " :mk, :added_by)",
        {**values, "mk": match_key(values["title"], values["author"]), "added_by": added_by},
    )
    return cur.lastrowid


def update_book(conn, book_id: int, book: dict) -> None:
    values = {f: book.get(f, "") for f in BOOK_FIELDS if f != "source"}
    values["library"] = clean_library(values["library"])
    conn.execute(
        "UPDATE books SET title=:title, author=:author, isbn=:isbn, publisher=:publisher, year=:year,"
        " cover_url=:cover_url, shelf=:shelf, library=:library, notes=:notes, match_key=:mk WHERE id=:id",
        {**values, "mk": match_key(values["title"], values["author"]), "id": book_id},
    )


def find_duplicate(conn, title: str, author: str, library: str | None = None):
    """Same book already catalogued; within one library when given, since a copy can sit in each."""
    if library:
        return conn.execute("SELECT * FROM books WHERE match_key = ? AND library = ? LIMIT 1",
                            (match_key(title, author), library)).fetchone()
    return conn.execute("SELECT * FROM books WHERE match_key = ? LIMIT 1", (match_key(title, author),)).fetchone()


def save_scan(conn, shelf: str, items: list, created_by: str, library: str = LIBRARIES[0]) -> int:
    cur = conn.execute(
        "INSERT INTO scans (shelf, library, items, created_by) VALUES (?, ?, ?, ?)",
        (shelf, clean_library(library), json.dumps(items, ensure_ascii=False), created_by),
    )
    return cur.lastrowid


def load_scan(conn, scan_id: int):
    row = conn.execute("SELECT * FROM scans WHERE id = ?", (scan_id,)).fetchone()
    if row is None:
        return None
    return {"id": row["id"], "shelf": row["shelf"], "library": row["library"], "items": json.loads(row["items"])}
