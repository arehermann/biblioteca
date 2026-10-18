"""Looks up book metadata (ISBN, cover, year, publisher) on Open Library."""
import asyncio

import httpx

SEARCH_URL = "https://openlibrary.org/search.json"
COVER_URL = "https://covers.openlibrary.org/b/id/{}-M.jpg"
FIELDS = "title,author_name,isbn,first_publish_year,cover_i,publisher"
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


async def lookup(client: httpx.AsyncClient, title: str, author: str = "", isbn: str = "") -> dict | None:
    """Returns metadata, or None when Open Library has no match. Raises LookupFailed on network errors."""
    docs = await _search(client, isbn=isbn) if isbn else []
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
                    return await lookup(client, item.get("title", ""), item.get("author", ""), item.get("isbn", ""))
                except LookupFailed as e:
                    return e if strict else None
        return await asyncio.gather(*(one(i) for i in items))
