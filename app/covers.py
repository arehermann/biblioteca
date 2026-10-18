"""Fills in missing covers from Open Library in the background, a batch at a time so the Pi stays responsive."""
import asyncio
import logging

from . import db, lookup

BATCH = 20
log = logging.getLogger("biblioteca.covers")
_running = False


def is_running() -> bool:
    return _running


def remaining() -> int:
    with db.connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM books WHERE cover_url = '' AND cover_checked = 0").fetchone()[0]


async def fill_batch() -> tuple[int, bool]:
    """Looks up one batch. Returns (books processed, whether Open Library failed)."""
    with db.connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, title, author, isbn FROM books WHERE cover_url = '' AND cover_checked = 0 LIMIT ?", (BATCH,))]
    if not rows:
        return 0, False
    metas = await lookup.lookup_many(rows, strict=True)
    failed = False
    with db.connect() as conn:
        for row, meta in zip(rows, metas):
            if isinstance(meta, lookup.LookupFailed):
                # Leave it unchecked so it is retried later instead of being marked as having no cover.
                failed = True
                continue
            if meta:
                conn.execute(
                    "UPDATE books SET cover_checked = 1, cover_url = ?, isbn = CASE WHEN isbn = '' THEN ? ELSE isbn END,"
                    " year = COALESCE(year, ?), publisher = CASE WHEN publisher = '' THEN ? ELSE publisher END"
                    " WHERE id = ?",
                    (meta["cover_url"], meta["isbn"], meta["year"], meta["publisher"], row["id"]))
            else:
                conn.execute("UPDATE books SET cover_checked = 1 WHERE id = ?", (row["id"],))
    return len(rows), failed


async def run_until_done(pause: float = 1.0) -> None:
    global _running
    if _running:
        return
    _running = True
    try:
        while True:
            processed, failed = await fill_batch()
            if not processed:
                break
            if failed:
                log.warning("Open Library no respondió; se reintentará más tarde")
                break
            await asyncio.sleep(pause)
    finally:
        _running = False


def start() -> None:
    asyncio.get_running_loop().create_task(run_until_done())
