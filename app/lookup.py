"""Looks up book metadata (ISBN, cover, year, publisher, edition) on Open Library, with Google Books as fallback.

A search result describes the work, so its cover can belong to any edition. When the ISBN is known we
read that exact edition instead, so the cover and publisher match the copy on the shelf."""
import asyncio
import logging
import os
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


log = logging.getLogger("biblioteca.lookup")


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
    if docs:
        return _to_meta(docs[0])
    # Open Library has nothing; Google Books knows many Spanish-language editions it lacks.
    try:
        return await google_lookup(client, title, author, isbn)
    except GoogleFailed as e:
        log.warning("Google Books no respondió: %s", e)
        return None


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
        work_key, own = "", None
        if isbn:
            own = await edition_by_isbn(client, isbn)
            work_key = own["work_key"] if own else ""
            if own and not own["cover_url"]:
                # Open Library often has the picture by ISBN even when the edition record lists no cover id.
                own["cover_url"] = f"https://covers.openlibrary.org/b/isbn/{isbn}-M.jpg?default=false"
        if not work_key and title:
            docs = await _search(client, title=title, author=author) if author else []
            docs = docs or await _search(client, title=title)
            work_key = docs[0].get("key", "") if docs else ""
        if not work_key:
            return [own] if own else []
        data = await _get_json(client, f"{work_key}/editions.json", limit=limit) or {}
    found = [_edition_meta(e) for e in data.get("entries", [])]
    found = sorted(found, key=lambda e: (not e["cover_url"], -(e["year"] or 0)))
    if own:
        # The edition with the book's own ISBN goes first: a popular work has hundreds, and it may not be among them.
        found = [own] + [e for e in found if e["edition_key"] != own["edition_key"]]
    return found


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


GOOGLE_RETRY_DELAYS = (1.0, 3.0)


class GoogleFailed(LookupFailed):
    """Google Books answered with an error (often its daily quota) rather than "no such book"."""


async def _google_items(client: httpx.AsyncClient, q: str, max_results: int = 1) -> list[dict]:
    """Google Books results for a query. Without GOOGLE_BOOKS_API_KEY every keyless app on the internet shares one
    daily quota, which runs out early in the day, so the key matters. A busy answer (429 or 5xx) is retried."""
    params = {"q": q, "maxResults": max_results, "printType": "books"}
    key = os.environ.get("GOOGLE_BOOKS_API_KEY", "").strip()
    if key:
        params["key"] = key
    error = ""
    # Without a key a 429 means the shared daily quota is spent, which waiting a few seconds won't change.
    for delay in (*(GOOGLE_RETRY_DELAYS if key else ()), None):
        try:
            r = await client.get(GOOGLE_BOOKS, params=params, timeout=15)
        except httpx.HTTPError as e:
            error = str(e) or type(e).__name__
        else:
            if r.status_code == 200:
                try:
                    return r.json().get("items") or []
                except ValueError as e:
                    raise GoogleFailed("respuesta ilegible") from e
            if r.status_code == 404:
                return []
            error = f"HTTP {r.status_code}"
            if r.status_code not in (429, 500, 502, 503, 504):
                break  # e.g. 400/403: a bad key won't fix itself by retrying
        if delay is None:
            break
        await asyncio.sleep(delay)
    raise GoogleFailed(error)


def _google_info(item: dict) -> dict:
    info = item.get("volumeInfo", {})
    links = info.get("imageLinks") or {}
    cover = (links.get("thumbnail") or links.get("smallThumbnail") or "").replace("http://", "https://")
    ids = {i.get("type"): i.get("identifier", "") for i in info.get("industryIdentifiers", [])}
    return {
        "title": info.get("title", ""),
        "author": ", ".join(info.get("authors", [])[:2]),
        "publisher": info.get("publisher", ""),
        "year": _year(info.get("publishedDate", "")),
        "cover_url": cover,
        "isbn": clean_isbn(ids.get("ISBN_13") or ids.get("ISBN_10") or ""),
    }


def failure_note(e: LookupFailed) -> str:
    """What to tell the person when a lookup failed."""
    if not isinstance(e, GoogleFailed):
        return "Open Library no respondió"
    if not os.environ.get("GOOGLE_BOOKS_API_KEY", "").strip():
        return "Google Books no respondió (falta GOOGLE_BOOKS_API_KEY en .env)"
    return "Google Books no respondió"


async def _google_books(client: httpx.AsyncClient, isbn: str) -> dict | None:
    """The edition with this ISBN on Google Books, None when it has none. Raises GoogleFailed on errors."""
    items = await _google_items(client, f"isbn:{isbn}")
    if not items:
        # Google's isbn: filter misses many books its plain search finds (often Latin American editions); keep
        # only a result that really carries this ISBN.
        items = [i for i in await _google_items(client, isbn, max_results=5) if _google_info(i)["isbn"] == isbn
                 or isbn in {clean_isbn(x.get("identifier", ""))
                             for x in i.get("volumeInfo", {}).get("industryIdentifiers", [])}]
    if not items:
        return None
    book = _google_info(items[0])
    book.pop("isbn")
    return book


async def google_lookup(client: httpx.AsyncClient, title: str, author: str = "", isbn: str = "") -> dict | None:
    """Google Books as a last resort for lookup(), in the same shape as an Open Library match."""
    items = []
    if isbn:
        items = await _google_items(client, f"isbn:{isbn}")
    if not items and title:
        q = f'intitle:"{title}"' + (f' inauthor:"{author.split(",")[0].strip()}"' if author else "")
        items = await _google_items(client, q)
    if not items:
        return None
    g = _google_info(items[0])
    return {"ol_title": g["title"], "ol_author": g["author"], "isbn": g["isbn"] or isbn, "year": g["year"],
            "publisher": g["publisher"], "cover_url": g["cover_url"], "edition": "", "work_key": "",
            "source_name": "Google Books"}


async def by_isbn(isbn: str) -> dict | None:
    """Everything known about the edition with this ISBN, for adding a book from its barcode.

    Open Library's edition record has the cover and publisher but names authors only by key, so the author comes
    from its search; Google Books fills whatever Open Library leaves empty, or everything when Open Library is
    down. Raises LookupFailed (GoogleFailed when it was Google) when the book wasn't found because a source
    couldn't be reached, so the person knows to try again rather than type it all in."""
    book = {"title": "", "author": "", "publisher": "", "edition": "", "year": None, "cover_url": ""}
    failure = None
    async with httpx.AsyncClient(headers=HEADERS) as client:
        try:
            edition = await edition_by_isbn(client, isbn) or {}
            docs = await _search(client, isbn=isbn)
            doc = _to_meta(docs[0]) if docs else {}
            book = {
                "title": edition.get("ol_title") or doc.get("ol_title", ""),
                "author": doc.get("ol_author", ""),
                "publisher": edition.get("publisher") or doc.get("publisher", ""),
                "edition": edition.get("edition", ""),
                "year": edition.get("year") or doc.get("year"),
                "cover_url": edition.get("cover_url") or doc.get("cover_url", ""),
            }
        except LookupFailed as e:
            failure = e
        if not all(book[k] for k in ("title", "author", "publisher", "cover_url")):
            try:
                google = await _google_books(client, isbn) or {}
            except GoogleFailed as e:
                google = {}
                log.warning("Google Books no respondió para %s: %s", isbn, e)
                failure = failure or e
            for k, v in google.items():
                book[k] = book[k] or v
    if not book["title"]:
        if failure:
            raise failure
        return None
    return {**book, "isbn": isbn}


async def by_isbn_many(isbns: list[str], concurrency: int = 4) -> list:
    """by_isbn for a list of scanned codes, a few at a time so Open Library isn't flooded.

    Each result is the book, None when neither source knows the ISBN, or a LookupFailed instance."""
    sem = asyncio.Semaphore(concurrency)

    async def one(isbn):
        async with sem:
            try:
                return await by_isbn(isbn)
            except LookupFailed as e:
                return e
    return await asyncio.gather(*(one(i) for i in isbns))
