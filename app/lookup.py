"""Looks up book metadata (ISBN, cover, year, publisher, edition) on Open Library.

A search result describes the work, so its cover can belong to any edition. When the ISBN is known we
read that exact edition instead, so the cover and publisher match the copy on the shelf."""
import asyncio
import re

import httpx

OL = "https://openlibrary.org"
SEARCH_URL = f"{OL}/search.json"
COVER_URL = "https://covers.openlibrary.org/b/id/{}-M.jpg"
FIELDS = "key,title,author_name,isbn,first_publish_year,cover_i,publisher"
HEADERS = {"User-Agent": "Biblioteca/1.0 (personal book catalog)"}


def _pick_isbn(isbns: list[str]) -> str:
    isbn13 = [i for i in isbns if len(i) == 13]
    return (isbn13 or isbns or [""])[0]


def _to_meta(doc: dict) -> dict:
    return {
        "ol_title": doc.get("title", ""),
        "ol_author": ", ".join(doc.get("author_name", [])[:2]),
        "isbn": _pick_isbn(doc.get("isbn", [])),
        "year": doc.get("first_publish_year"),
        "publisher": (doc.get("publisher") or [""])[0],
        "cover_url": COVER_URL.format(doc["cover_i"]) if doc.get("cover_i") else "",
        "edition": "",
        "work_key": doc.get("key", ""),
    }


def _year(text: str) -> int | None:
    m = re.search(r"\b(1[5-9]\d\d|20\d\d)\b", text or "")
    return int(m.group(1)) if m else None


def _edition_meta(ed: dict) -> dict:
    covers = [c for c in ed.get("covers", []) if isinstance(c, int) and c > 0]
    isbns = ed.get("isbn_13", []) + ed.get("isbn_10", [])
    return {
        "ol_title": ed.get("title", ""),
        "ol_author": "",
        "isbn": _pick_isbn(isbns),
        "year": _year(ed.get("publish_date", "")),
        "publisher": (ed.get("publishers") or [""])[0],
        "cover_url": COVER_URL.format(covers[0]) if covers else "",
        "edition": ed.get("edition_name", ""),
        "work_key": (ed.get("works") or [{}])[0].get("key", ""),
        "edition_key": ed.get("key", ""),
    }


class LookupFailed(Exception):
    """Open Library could not be reached or answered with an error."""


async def _search(client: httpx.AsyncClient, **query) -> list[dict]:
    try:
        r = await client.get(SEARCH_URL, params={"fields": FIELDS, "limit": 1, **query}, timeout=15)
        r.raise_for_status()
        return r.json().get("docs", [])
    except (httpx.HTTPError, ValueError) as e:
        raise LookupFailed(str(e)) from e


async def _get_json(client: httpx.AsyncClient, path: str, **params) -> dict | None:
    """GET an Open Library JSON document; None when it doesn't exist."""
    try:
        r = await client.get(f"{OL}{path}", params=params, timeout=15, follow_redirects=True)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()
    except (httpx.HTTPError, ValueError) as e:
        raise LookupFailed(str(e)) from e


async def edition_by_isbn(client: httpx.AsyncClient, isbn: str) -> dict | None:
    ed = await _get_json(client, f"/isbn/{isbn}.json")
    return _edition_meta(ed) if ed else None


async def lookup(client: httpx.AsyncClient, title: str, author: str = "", isbn: str = "",
                 publisher: str = "") -> dict | None:
    """Returns metadata, or None when Open Library has no match. Raises LookupFailed on network errors."""
    if isbn:
        edition = await edition_by_isbn(client, isbn)
        if edition and edition["cover_url"]:
            return edition
        docs = await _search(client, isbn=isbn)
        if edition:
            # The exact edition has no scanned cover; keep its details and borrow the work's cover.
            if docs and docs[0].get("cover_i"):
                edition["cover_url"] = COVER_URL.format(docs[0]["cover_i"])
            return edition
    else:
        docs = []
    if not docs and title and publisher:
        # A publisher read off the spine narrows the match to that publisher's editions.
        docs = await _search(client, title=title, publisher=publisher)
    if not docs and title:
        # Many local editions are missing from Open Library by ISBN; fall back to title and author.
        docs = await _search(client, title=title, author=author) if author else []
        if not docs:
            # Spine authors are often partial; retry with title only.
            docs = await _search(client, title=title)
    return _to_meta(docs[0]) if docs else None


async def lookup_many(items: list[dict], concurrency: int = 4, strict: bool = False) -> list:
    """Looks up each item. A failed lookup comes back as None, or as a LookupFailed instance when strict."""
    sem = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(headers=HEADERS) as client:
        async def one(item):
            async with sem:
                try:
                    return await lookup(client, item.get("title", ""), item.get("author", ""), item.get("isbn", ""),
                                        item.get("publisher", ""))
                except LookupFailed as e:
                    return e if strict else None
        return await asyncio.gather(*(one(i) for i in items))


async def editions(title: str, author: str = "", isbn: str = "", limit: int = 60) -> list[dict]:
    """Editions of the book's work, those with a cover first, so a person can pick the one they own."""
    async with httpx.AsyncClient(headers=HEADERS) as client:
        work_key = ""
        if isbn:
            ed = await edition_by_isbn(client, isbn)
            work_key = ed["work_key"] if ed else ""
        if not work_key and title:
            docs = await _search(client, title=title, author=author) if author else []
            docs = docs or await _search(client, title=title)
            work_key = docs[0].get("key", "") if docs else ""
        if not work_key:
            return []
        data = await _get_json(client, f"{work_key}/editions.json", limit=limit) or {}
    found = [_edition_meta(e) for e in data.get("entries", [])]
    return sorted(found, key=lambda e: (not e["cover_url"], -(e["year"] or 0)))


# --- Barcodes ----------------------------------------------------------------

GOOGLE_BOOKS = "https://www.googleapis.com/books/v1/volumes"


def clean_isbn(text: str) -> str:
    """The ISBN-13 in a scanned or typed code, or "" when it is not a valid book ISBN."""
    code = re.sub(r"[^0-9Xx]", "", text or "").upper()
    if len(code) == 10 and code[:9].isdigit():
        total = sum((10 - i) * (10 if c == "X" else int(c)) for i, c in enumerate(code))
        if total % 11:
            return ""
        code = "978" + code[:9]
        return code + str((10 - sum((3 if i % 2 else 1) * int(c) for i, c in enumerate(code)) % 10) % 10)
    if len(code) == 13 and code.isdigit() and code[:3] in ("978", "979"):
        check = (10 - sum((3 if i % 2 else 1) * int(c) for i, c in enumerate(code[:12])) % 10) % 10
        return code if check == int(code[12]) else ""
    return ""


async def _google_books(client: httpx.AsyncClient, isbn: str) -> dict | None:
    """Google Books knows many Spanish-language editions Open Library lacks. Best effort: errors mean no match."""
    try:
        r = await client.get(GOOGLE_BOOKS, params={"q": f"isbn:{isbn}"}, timeout=15)
        items = r.json().get("items") if r.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None
    if not items:
        return None
    info = items[0].get("volumeInfo", {})
    cover = (info.get("imageLinks") or {}).get("thumbnail", "").replace("http://", "https://")
    return {
        "title": info.get("title", ""),
        "author": ", ".join(info.get("authors", [])[:2]),
        "publisher": info.get("publisher", ""),
        "year": _year(info.get("publishedDate", "")),
        "cover_url": cover,
    }


async def by_isbn(isbn: str) -> dict | None:
    """Everything known about the edition with this ISBN, for adding a book from its barcode.

    Open Library's edition record has the cover and publisher but names authors only by key, so the author comes
    from its search; Google Books fills whatever Open Library leaves empty. Raises LookupFailed when Open Library
    can't be reached."""
    async with httpx.AsyncClient(headers=HEADERS) as client:
        edition = await edition_by_isbn(client, isbn)
        docs = await _search(client, isbn=isbn)
        doc = _to_meta(docs[0]) if docs else {}
        book = {
            "title": (edition or {}).get("ol_title") or doc.get("ol_title", ""),
            "author": doc.get("ol_author", ""),
            "publisher": (edition or {}).get("publisher") or doc.get("publisher", ""),
            "edition": (edition or {}).get("edition", ""),
            "year": (edition or {}).get("year") or doc.get("year"),
            "cover_url": (edition or {}).get("cover_url") or doc.get("cover_url", ""),
        }
        if not all(book[k] for k in ("title", "author", "publisher", "cover_url")):
            google = await _google_books(client, isbn) or {}
            for k, v in google.items():
                book[k] = book[k] or v
    if not book["title"]:
        return None
    return {**book, "isbn": isbn}
