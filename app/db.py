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
    edition TEXT NOT NULL DEFAULT '',
    year INTEGER,
    cover_url TEXT NOT NULL DEFAULT '',
    shelf TEXT NOT NULL DEFAULT '',
    library TEXT NOT NULL DEFAULT 'Principal',
    notes TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'manual',
    match_key TEXT NOT NULL DEFAULT '',
    added_by TEXT NOT NULL DEFAULT '',
    cover_checked INTEGER NOT NULL DEFAULT 0,
    cover_manual INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS books_match_key ON books(match_key);
CREATE TABLE IF NOT EXISTS loans (
    id INTEGER PRIMARY KEY,
    book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    person TEXT NOT NULL,
    lent_on TEXT NOT NULL DEFAULT (date('now', 'localtime')),
    returned_on TEXT,
    notes TEXT NOT NULL DEFAULT '',
    lent_by TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS loans_book ON loans(book_id);
CREATE TABLE IF NOT EXISTS dup_ignored (
    group_key TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY,
    shelf TEXT NOT NULL DEFAULT '',
    library TEXT NOT NULL DEFAULT 'Principal',
    items TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

BOOK_FIELDS = ("title", "author", "isbn", "publisher", "edition", "year", "cover_url", "shelf", "library", "notes",
               "source")

# Columns added after the first release; init_db adds them to existing databases.
MIGRATIONS = {
    "books": {
        "library": "TEXT NOT NULL DEFAULT 'Principal'",
        "edition": "TEXT NOT NULL DEFAULT ''",
        "cover_manual": "INTEGER NOT NULL DEFAULT 0",
    },
    "scans": {"library": "TEXT NOT NULL DEFAULT 'Principal'"},
}
# Run once, right after the column is added to an existing database.
AFTER_MIGRATION = {
    # Covers used to come from the work, not the edition; recheck every book with an ISBN by its exact edition.
    ("books", "edition"): "UPDATE books SET cover_checked = 0 WHERE isbn != ''",
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
                    if (table, name) in AFTER_MIGRATION:
                        conn.execute(AFTER_MIGRATION[(table, name)])
        conn.execute("CREATE INDEX IF NOT EXISTS books_library ON books(library)")
        refresh_match_keys(conn)


def refresh_match_keys(conn) -> None:
    """Recomputes stored keys when match_key's rules change, so older books are compared the same way."""
    for row in conn.execute("SELECT id, title, author, match_key FROM books").fetchall():
        key = match_key(row["title"], row["author"])
        if key != row["match_key"]:
            conn.execute("UPDATE books SET match_key = ? WHERE id = ?", (key, row["id"]))


def clean_library(value: str | None) -> str:
    return value if value in LIBRARIES else LIBRARIES[0]


@contextmanager
def connect():
    conn = sqlite3.connect(db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9 ]+", " ", text)


def _title_key(title: str) -> str:
    """Folded title without subtitle, series or edition notes ("Harry Potter (Book 1)", "Rayuela - Edición
    conmemorativa", "El Aleph [bolsillo]") and without a leading article."""
    t = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", title or "")
    t = _fold(re.split(r":| - | – | — ", t)[0])
    t = re.sub(r"^(the|a|an|el|la|los|las|un|una|lo)\s+", "", t.strip())
    return " ".join(t.split())


def match_key(title: str, author: str) -> str:
    """Key used to spot duplicates: title key + author surname."""
    t = _title_key(title)
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
        "INSERT INTO books (title, author, isbn, publisher, edition, year, cover_url, shelf, library, notes, source,"
        " match_key, added_by) VALUES (:title, :author, :isbn, :publisher, :edition, :year, :cover_url, :shelf,"
        " :library, :notes, :source, :mk, :added_by)",
        {**values, "mk": match_key(values["title"], values["author"]), "added_by": added_by},
    )
    return cur.lastrowid


def update_book(conn, book_id: int, book: dict) -> None:
    values = {f: book.get(f, "") for f in BOOK_FIELDS if f != "source"}
    values["library"] = clean_library(values["library"])
    conn.execute(
        "UPDATE books SET title=:title, author=:author, isbn=:isbn, publisher=:publisher, edition=:edition, year=:year,"
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


def active_loan(conn, book_id: int):
    return conn.execute("SELECT * FROM loans WHERE book_id = ? AND returned_on IS NULL ORDER BY id DESC LIMIT 1",
                        (book_id,)).fetchone()


def lend(conn, book_id: int, person: str, lent_on: str, notes: str, lent_by: str) -> None:
    if active_loan(conn, book_id) is None:
        conn.execute("INSERT INTO loans (book_id, person, lent_on, notes, lent_by) VALUES (?, ?, ?, ?, ?)",
                     (book_id, person.strip(), lent_on, notes.strip(), lent_by))


def give_back(conn, loan_id: int, returned_on: str) -> None:
    conn.execute("UPDATE loans SET returned_on = ? WHERE id = ? AND returned_on IS NULL", (returned_on, loan_id))


def borrowers(conn) -> list[str]:
    return [r[0] for r in conn.execute("SELECT person FROM loans GROUP BY person ORDER BY MAX(id) DESC")]


# --- Repetidos ---------------------------------------------------------------

def duplicate_groups(conn) -> list[list]:
    """Books in the same library that look like the same book: same title and author, same ISBN, or same title
    where one of them has no author (a spine that showed only the title). Groups someone marked as not repeated
    stay hidden until another book joins them."""
    rows = conn.execute(
        "SELECT books.*, (SELECT person FROM loans WHERE loans.book_id = books.id AND returned_on IS NULL"
        " LIMIT 1) AS lent_to FROM books ORDER BY id").fetchall()
    parent = {r["id"]: r["id"] for r in rows}

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def join(by):
        buckets = {}
        for r in rows:
            for key in by(r):
                buckets.setdefault(key, []).append(r["id"])
        for ids in buckets.values():
            for i in ids[1:]:
                parent[find(i)] = find(ids[0])

    join(lambda r: [(r["library"], r["match_key"])])
    join(lambda r: [(r["library"], r["isbn"])] if r["isbn"] else [])
    # A spine that showed only the title gives a book without author: it matches any book with that title.
    authorless = {(r["library"], _title_key(r["title"])) for r in rows if not r["author"].strip()}
    join(lambda r: [(r["library"], _title_key(r["title"]))]
         if (r["library"], _title_key(r["title"])) in authorless else [])

    groups = {}
    for r in rows:
        groups.setdefault(find(r["id"]), []).append(r)
    ignored = {r[0] for r in conn.execute("SELECT group_key FROM dup_ignored")}
    return [g for g in groups.values() if len(g) > 1 and group_key([b["id"] for b in g]) not in ignored]


def group_key(ids) -> str:
    return ",".join(str(i) for i in sorted(int(i) for i in ids))


def completeness(book) -> int:
    """How much a copy knows about itself, to suggest which one to keep."""
    return sum(bool(book[f]) for f in ("author", "isbn", "publisher", "edition", "year", "cover_url", "shelf",
                                       "notes")) + (2 if book["cover_url"] else 0)


def merge_books(conn, keep_id: int, ids: list[int]) -> None:
    """Folds the other copies into keep_id: empty fields are filled from them, notes and loans are kept, and the
    other copies are deleted."""
    others = [i for i in ids if i != keep_id]
    keep = conn.execute("SELECT * FROM books WHERE id = ?", (keep_id,)).fetchone()
    if keep is None or not others:
        return
    merged = dict(keep)
    notes = [keep["notes"]] if keep["notes"] else []
    for book in conn.execute(f"SELECT * FROM books WHERE id IN ({','.join('?' * len(others))}) ORDER BY id",
                             others).fetchall():
        for f in ("author", "isbn", "publisher", "edition", "year", "shelf"):
            merged[f] = merged[f] or book[f]
        if not merged["cover_url"] and book["cover_url"]:
            merged["cover_url"], merged["cover_manual"] = book["cover_url"], book["cover_manual"]
        if book["notes"] and book["notes"] not in notes:
            notes.append(book["notes"])
    merged["notes"] = "\n".join(notes)
    conn.execute(
        "UPDATE books SET author=:author, isbn=:isbn, publisher=:publisher, edition=:edition, year=:year,"
        " shelf=:shelf, cover_url=:cover_url, cover_manual=:cover_manual, notes=:notes, match_key=:mk WHERE id=:id",
        {**merged, "mk": match_key(merged["title"], merged["author"])})
    marks = ",".join("?" * len(others))
    conn.execute(f"UPDATE loans SET book_id = ? WHERE book_id IN ({marks})", [keep_id, *others])
    conn.execute(f"DELETE FROM books WHERE id IN ({marks})", others)
