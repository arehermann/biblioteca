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


async def lookup(client: httpx.AsyncClient, title: str, author: str = "", isbn: str = "") -> dict | None:
    params = {"fields": FIELDS, "limit": 1}
    if isbn:
        params["isbn"] = isbn
    else:
        params["title"] = title
        if author:
            params["author"] = author
    try:
        r = await client.get(SEARCH_URL, params=params, timeout=15)
        r.raise_for_status()
        docs = r.json().get("docs", [])
        if not docs and author and not isbn:
            # Spine authors are often partial; retry with title only.
            r = await client.get(SEARCH_URL, params={"fields": FIELDS, "limit": 1, "title": title}, timeout=15)
            r.raise_for_status()
            docs = r.json().get("docs", [])
    except (httpx.HTTPError, ValueError):
        return None
    return _to_meta(docs[0]) if docs else None


async def lookup_many(items: list[dict], concurrency: int = 4) -> list[dict | None]:
    sem = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(headers=HEADERS) as client:
        async def one(item):
            async with sem:
                return await lookup(client, item.get("title", ""), item.get("author", ""), item.get("isbn", ""))
        return await asyncio.gather(*(one(i) for i in items))
