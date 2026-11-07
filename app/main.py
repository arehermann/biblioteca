import csv
import io
import os
import re
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from datetime import date
from urllib.parse import quote

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from PIL import Image, ImageOps

from . import auth, covers, db, lookup, storygraph, vision

BASE = Path(__file__).parent
PAGE_SIZE = 60
ALL = "todas"
PUBLIC_PATHS = ("/login", "/static", "/sw.js")

@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    covers.start()  # picks up books left without a cover by an earlier run
    yield


app = FastAPI(title="Biblioteca", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")



@app.get("/sw.js", include_in_schema=False)
def service_worker():
    # Served from the root so its scope covers the whole app, not just /static/.
    return FileResponse(BASE / "static" / "sw.js", media_type="text/javascript",
                        headers={"Cache-Control": "no-cache"})

def _fecha(iso: str | None) -> str:
    """2026-10-18 -> 18/10/2026"""
    return "/".join(reversed(iso[:10].split("-"))) if iso else ""


def _dias(iso: str | None) -> int:
    return (date.today() - date.fromisoformat(iso[:10])).days if iso else 0


def _static(path: str) -> str:
    """URL of a file in app/static that changes when the file does, so phones never keep an old copy after an update."""
    try:
        version = int((BASE / "static" / path).stat().st_mtime)
    except OSError:
        version = 0
    return f"/static/{path}?v={version}"


templates.env.globals["static"] = _static
templates.env.filters["fecha"] = _fecha
templates.env.filters["dias"] = _dias


@app.middleware("http")
async def require_login(request: Request, call_next):
    if not request.url.path.startswith(PUBLIC_PATHS) and not request.session.get("user"):
        return RedirectResponse("/login", status_code=303)
    return await call_next(request)


# Added after require_login so it runs first and request.session is available there.
app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("BIBLIOTECA_SECRET", "dev-only-change-me"),
    max_age=60 * 60 * 24 * 90,
    same_site="lax",
)


# Each library has its own look: dark "Noche" for the main one, colorful "Lúdica" for the kids'.
THEMES = {db.LIBRARIES[0]: "noche", db.LIBRARIES[1]: "ludica"}


def render(request: Request, name: str, **ctx):
    theme = ctx.pop("theme", None) or THEMES[db.clean_library(ctx.get("library") or request.session.get("library"))]
    return templates.TemplateResponse(request, name, {"user": request.session.get("user"), "theme": theme,
                                                      "libraries": db.LIBRARIES, **ctx})


def current_library(request: Request) -> str:
    """Last library used for adding books, so each form starts on the one you were working in."""
    return db.clean_library(request.session.get("library"))


def remember_library(request: Request, library: str) -> str:
    library = db.clean_library(library)
    request.session["library"] = library
    return library


def render_import(request: Request, result=None):
    with db.connect() as conn:
        no_cover = conn.execute("SELECT COUNT(*) FROM books WHERE cover_url = '' AND cover_checked = 1").fetchone()[0]
    return render(request, "import.html", result=result, covers_left=covers.remaining(), library=current_library(request),
                  covers_running=covers.is_running(), no_cover=no_cover)


def back(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def form_book(form) -> dict:
    year = (form.get("year") or "").strip()
    return {
        "title": (form.get("title") or "").strip(),
        "author": (form.get("author") or "").strip(),
        "isbn": (form.get("isbn") or "").strip(),
        "publisher": (form.get("publisher") or "").strip(),
        "edition": (form.get("edition") or "").strip(),
        "year": int(year) if year.isdigit() else None,
        "cover_url": (form.get("cover_url") or "").strip(),
        "shelf": (form.get("shelf") or "").strip(),
        "library": db.clean_library(form.get("library")),
        "notes": (form.get("notes") or "").strip(),
    }


def shelves(conn, library: str = "") -> list[str]:
    if library:
        rows = conn.execute("SELECT DISTINCT shelf FROM books WHERE shelf != '' AND library = ? ORDER BY shelf",
                            (library,))
    else:
        rows = conn.execute("SELECT DISTINCT shelf FROM books WHERE shelf != '' ORDER BY shelf")
    return [r[0] for r in rows]


# --- Sesión ---------------------------------------------------------------

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return render(request, "login.html", error=None)


@app.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...)):
    with db.connect() as conn:
        ok = auth.check_login(conn, username, password)
    if not ok:
        return render(request, "login.html", error="Usuario o contraseña incorrectos.")
    request.session["user"] = username.strip().lower()
    return back("/")


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return back("/login")


# --- Catálogo -------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def catalog(request: Request, q: str = "", shelf: str = "", library: str = "", lent: int = 0, page: int = 1):
    # Opens on the library you were last in; "todas" searches both.
    if library in db.LIBRARIES:
        remember_library(request, library)
    elif library != ALL:
        library = current_library(request)
    if library == ALL:
        library = ""
    where, params = [], []
    if lent:
        where.append("id IN (SELECT book_id FROM loans WHERE returned_on IS NULL)")
    if library:
        where.append("library = ?")
        params.append(library)
    if q:
        where.append("(title LIKE ? OR author LIKE ? OR isbn LIKE ?)")
        params += [f"%{q}%"] * 3
    if shelf:
        where.append("shelf = ?")
        params.append(shelf)
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    page = max(page, 1)
    with db.connect() as conn:
        total = conn.execute(f"SELECT COUNT(*) FROM books {clause}", params).fetchone()[0]
        books = conn.execute(
            f"SELECT books.*, (SELECT person FROM loans WHERE loans.book_id = books.id AND returned_on IS NULL"
            f" ORDER BY loans.id DESC LIMIT 1) AS lent_to"
            f" FROM books {clause} ORDER BY author COLLATE NOCASE, title COLLATE NOCASE LIMIT ? OFFSET ?",
            params + [PAGE_SIZE, (page - 1) * PAGE_SIZE],
        ).fetchall()
        all_count = conn.execute("SELECT COUNT(*) FROM books").fetchone()[0]
        counts = dict(conn.execute("SELECT library, COUNT(*) FROM books GROUP BY library").fetchall())
        shelf_list = shelves(conn, library)
        dup_count = len(db.duplicate_groups(conn))
        out_where = "AND books.library = ?" if library else ""
        out = conn.execute(
            "SELECT loans.person, loans.lent_on, books.id, books.title, books.cover_url FROM loans"
            f" JOIN books ON books.id = loans.book_id WHERE returned_on IS NULL {out_where}"
            " ORDER BY lent_on, loans.id", [library] if library else []).fetchall()
    return render(request, "catalog.html", covers_running=covers.is_running(), covers_left=covers.remaining(),
                  books=books, total=total, all_count=all_count, q=q, shelf=shelf, library=library, counts=counts,
                  lent=lent, out=out, dup_count=dup_count, lib_param=library or ALL, theme=None if library else "noche",
                  shelves=shelf_list, page=page, pages=max(1, -(-total // PAGE_SIZE)))


@app.post("/books/move")
async def move_books(request: Request):
    form = await request.form()
    target = db.clean_library(form.get("to"))
    ids = [int(i) for i in form.getlist("book") if str(i).isdigit()]
    if ids:
        with db.connect() as conn:
            conn.execute(f"UPDATE books SET library = ? WHERE id IN ({','.join('?' * len(ids))})", [target, *ids])
    return back(f"/?library={target}&moved={len(ids)}")


@app.get("/books/new", response_class=HTMLResponse)
def new_book_page(request: Request):
    with db.connect() as conn:
        return render(request, "book.html", book=None, shelves=shelves(conn), library=current_library(request))


@app.post("/books/new")
async def new_book(request: Request):
    form = await request.form()
    book = form_book(form)
    remember_library(request, book["library"])
    if not book["title"]:
        return back("/books/new")
    if not book["cover_url"]:
        meta = (await lookup.lookup_many([book]))[0]
        if meta:
            for k in ("isbn", "publisher", "edition", "year", "cover_url"):
                book[k] = book[k] or meta[k]
    with db.connect() as conn:
        book_id = db.add_book(conn, book, request.session["user"])
    if form.get("next") == "/isbn":
        # Adding from a barcode goes straight back to scan the next one.
        return back(f"/isbn?added={book_id}")
    return back(f"/books/{book_id}?saved=1")


# What a book's page says it is missing, and what filling it from a barcode can add.
FIELD_NAMES = {"author": "autor", "isbn": "ISBN", "publisher": "editorial", "year": "año", "cover_url": "portada"}


@app.get("/books/{book_id}", response_class=HTMLResponse)
def book_page(request: Request, book_id: int, saved: int = 0, completed: str | None = None,
              photo_error: str = ""):
    with db.connect() as conn:
        book = conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
        if book is None:
            return back("/")
        loans = conn.execute("SELECT * FROM loans WHERE book_id = ? ORDER BY id DESC", (book_id,)).fetchall()
        return render(request, "book.html", book=book, shelves=shelves(conn), saved=saved, library=book["library"],
                      loan=db.active_loan(conn, book_id), loans=loans, borrowers=db.borrowers(conn),
                      today=date.today().isoformat(), completed=completed, photo_error=photo_error,
                      missing=[name for f, name in FIELD_NAMES.items() if not book[f]])


@app.post("/books/{book_id}/completar")
async def complete_from_isbn(book_id: int, isbn: str = Form("")):
    """Fills a book from its barcode. A new ISBN means the copy in hand is that edition, so its publisher, year
    and cover replace the old guesses (a cover picked by hand stays); otherwise only empty fields are filled."""
    code = lookup.clean_isbn(isbn)
    with db.connect() as conn:
        book = conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
    if book is None:
        return back("/")
    if not code:
        return back(f"/books/{book_id}?completed=")
    try:
        found = await lookup.by_isbn(code)
    except lookup.LookupFailed:
        found = None
    new_edition = code != book["isbn"]
    changes = {"isbn": code} if new_edition else {}
    for f in ("title", "author", "publisher", "edition", "year", "cover_url"):
        value = (found or {}).get(f)
        if not value or value == book[f]:
            continue
        if f == "cover_url" and book["cover_manual"]:
            continue
        if not book[f] or (new_edition and f in ("publisher", "edition", "year", "cover_url")):
            changes[f] = value
    if changes:
        with db.connect() as conn:
            conn.execute(f"UPDATE books SET {', '.join(f'{f} = ?' for f in changes)}, cover_checked = 1 WHERE id = ?",
                         [*changes.values(), book_id])
            if "author" in changes or "title" in changes:
                row = conn.execute("SELECT title, author FROM books WHERE id = ?", (book_id,)).fetchone()
                conn.execute("UPDATE books SET match_key = ? WHERE id = ?",
                             (db.match_key(row["title"], row["author"]), book_id))
    names = {**FIELD_NAMES, "title": "título", "edition": "edición"}
    return back(f"/books/{book_id}?completed={quote(', '.join(names[f] for f in changes))}")


@app.post("/books/{book_id}")
async def edit_book(request: Request, book_id: int):
    book = form_book(await request.form())
    if not book["title"]:
        return back(f"/books/{book_id}")
    recheck = False
    with db.connect() as conn:
        old = conn.execute("SELECT isbn, cover_url, cover_manual FROM books WHERE id = ?", (book_id,)).fetchone()
        if old is None:
            return back("/")
        db.update_book(conn, book_id, book)
        if book["cover_url"] != old["cover_url"]:
            # A cover typed in by hand wins; clearing the field hands it back to the automatic search.
            manual = 1 if book["cover_url"] else 0
            conn.execute("UPDATE books SET cover_manual = ?, cover_checked = ? WHERE id = ?",
                         (manual, manual, book_id))
            recheck = not manual
        elif book["isbn"] != old["isbn"] and not old["cover_manual"]:
            # Another ISBN is another edition, with its own cover.
            conn.execute("UPDATE books SET cover_checked = 0 WHERE id = ?", (book_id,))
            recheck = True
    if recheck:
        covers.start()
    return back(f"/books/{book_id}?saved=1")


@app.get("/books/{book_id}/ediciones", response_class=HTMLResponse)
async def editions_page(request: Request, book_id: int):
    with db.connect() as conn:
        book = conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
    if book is None:
        return back("/")
    error = None
    try:
        found = await lookup.editions(book["title"], book["author"], book["isbn"])
    except lookup.LookupFailed:
        found, error = [], "Open Library no respondió. Vuelve a intentar en un rato."
    return render(request, "editions.html", book=book, editions=found, error=error)


@app.post("/books/{book_id}/ediciones")
async def choose_edition(request: Request, book_id: int):
    form = await request.form()
    year = (form.get("year") or "").strip()
    with db.connect() as conn:
        conn.execute(
            "UPDATE books SET cover_url = ?, isbn = CASE WHEN ? != '' THEN ? ELSE isbn END,"
            " publisher = CASE WHEN ? != '' THEN ? ELSE publisher END, edition = ?, year = COALESCE(?, year),"
            " cover_manual = 1, cover_checked = 1 WHERE id = ?",
            (form.get("cover_url") or "", form.get("isbn") or "", form.get("isbn") or "",
             form.get("publisher") or "", form.get("publisher") or "", form.get("edition") or "",
             int(year) if year.isdigit() else None, book_id))
    return back(f"/books/{book_id}?saved=1")


# --- Repetidos ------------------------------------------------------------

@app.get("/repetidos", response_class=HTMLResponse)
def duplicates_page(request: Request, merged: int = 0):
    with db.connect() as conn:
        groups = db.duplicate_groups(conn)
    groups.sort(key=lambda g: (g[0]["library"], g[0]["title"].lower()))
    # Suggest keeping the copy that knows the most about itself; ties go to the one added first.
    keep = {id(g): max(g, key=lambda b: (db.completeness(b), bool(b["lent_to"]), -b["id"]))["id"] for g in groups}
    return render(request, "duplicates.html", groups=[(g, keep[id(g)]) for g in groups], merged=merged)


@app.post("/repetidos/unir")
async def merge_duplicates(request: Request):
    form = await request.form()
    ids = [int(i) for i in form.getlist("ids") if str(i).isdigit()]
    keep = int(form.get("keep") or 0)
    if keep in ids:
        with db.connect() as conn:
            db.merge_books(conn, keep, ids)
    return back(f"/repetidos?merged={max(len(ids) - 1, 0)}")


@app.post("/repetidos/ignorar")
async def ignore_duplicates(request: Request):
    form = await request.form()
    ids = [i for i in form.getlist("ids") if str(i).isdigit()]
    if len(ids) > 1:
        with db.connect() as conn:
            conn.execute("INSERT OR IGNORE INTO dup_ignored (group_key) VALUES (?)", (db.group_key(ids),))
    return back("/repetidos")


# --- Préstamos ------------------------------------------------------------

@app.post("/books/{book_id}/prestar")
def lend_book(request: Request, book_id: int, person: str = Form(...), lent_on: str = Form(""),
              notes: str = Form("")):
    if person.strip():
        with db.connect() as conn:
            db.lend(conn, book_id, person, _iso_or_today(lent_on), notes, request.session["user"])
    return back(f"/books/{book_id}")


@app.post("/loans/{loan_id}/devolver")
def return_book(loan_id: int, returned_on: str = Form(""), next: str = Form("/prestamos")):
    with db.connect() as conn:
        db.give_back(conn, loan_id, _iso_or_today(returned_on))
    return back(next if next.startswith("/") and not next.startswith("//") else "/prestamos")


@app.get("/prestamos", response_class=HTMLResponse)
def loans_page(request: Request):
    with db.connect() as conn:
        active = conn.execute(
            "SELECT loans.*, books.title, books.author, books.cover_url, books.library FROM loans"
            " JOIN books ON books.id = loans.book_id WHERE returned_on IS NULL ORDER BY lent_on, loans.id").fetchall()
        history = conn.execute(
            "SELECT loans.*, books.title, books.author FROM loans JOIN books ON books.id = loans.book_id"
            " WHERE returned_on IS NOT NULL ORDER BY returned_on DESC, loans.id DESC LIMIT 50").fetchall()
    return render(request, "loans.html", active=active, history=history)


def _iso_or_today(value: str) -> str:
    try:
        return date.fromisoformat(value.strip()).isoformat()
    except ValueError:
        return date.today().isoformat()


@app.post("/books/{book_id}/delete")
def delete_book(book_id: int):
    with db.connect() as conn:
        conn.execute("DELETE FROM books WHERE id = ?", (book_id,))
    return back("/")


# --- Fotos de estantes ----------------------------------------------------

def scan_form(request: Request, error: str | None = None):
    with db.connect() as conn:
        return render(request, "scan.html", shelves=shelves(conn), library=current_library(request), error=error)


@app.get("/scan", response_class=HTMLResponse)
def scan_page(request: Request):
    return scan_form(request)


@app.post("/scan")
async def scan(request: Request, camera: UploadFile | None = File(None), gallery: UploadFile | None = File(None),
               shelf: str = Form(""), library: str = Form("")):
    library = remember_library(request, library)
    data = b""
    for upload in (camera, gallery):
        if upload is not None and upload.filename:
            data = await upload.read()
            if data:
                break
    if not data:
        return scan_form(request, "Saca una foto o elige una de la galería.")
    try:
        found = await vision.read_spines(data)
    except vision.VisionError as e:
        return scan_form(request, str(e))
    except Exception:
        return scan_form(request, "No se pudo leer la imagen. ¿Es una foto en JPG o PNG?")
    metas = await lookup.lookup_many(found)
    items = []
    with db.connect() as conn:
        for book, meta in zip(found, metas):
            dup = db.find_duplicate(conn, book["title"], book["author"], library)
            item = {**book, **(meta or {}), "matched": bool(meta), "duplicate_id": dup["id"] if dup else None}
            # The publisher printed on the spine describes the copy on the shelf better than a search match.
            item["publisher"] = book.get("publisher") or (meta or {}).get("publisher", "")
            items.append(item)
        scan_id = db.save_scan(conn, shelf.strip(), items, request.session["user"], library)
    return back(f"/scan/{scan_id}")


@app.get("/scan/{scan_id}", response_class=HTMLResponse)
def review_scan(request: Request, scan_id: int):
    with db.connect() as conn:
        data = db.load_scan(conn, scan_id)
    if data is None:
        return back("/scan")
    from_codes = any(i.get("source") == "codigo" for i in data["items"])
    return render(request, "review.html", scan=data, from_codes=from_codes)


@app.post("/scan/{scan_id}")
async def save_scan(request: Request, scan_id: int):
    form = await request.form()
    added = 0
    with db.connect() as conn:
        data = db.load_scan(conn, scan_id)
        if data is None:
            return back("/scan")
        shelf = (form.get("shelf") or data["shelf"]).strip()
        library = db.clean_library(form.get("library") or data["library"])
        merged = 0
        for i, item in enumerate(data["items"]):
            if form.get(f"merge_{i}") and item.get("duplicate_id"):
                # Recomputed now, against the book as it is, with the shelf as finally chosen.
                dup = conn.execute("SELECT * FROM books WHERE id = ?", (item["duplicate_id"],)).fetchone()
                if dup is not None:
                    changes = {c["field"]: c["new"] for c in merge_plan(dup, item, item["isbn"], shelf)
                               if c["checked"]}
                    apply_merge(conn, dup["id"], changes)
                    merged += bool(changes)
            if not form.get(f"add_{i}"):
                continue
            title = (form.get(f"title_{i}") or item["title"]).strip()
            author = (form.get(f"author_{i}") or item["author"]).strip()
            if not title:
                continue  # an ISBN nobody knew, left without a title
            publisher = (form.get(f"publisher_{i}") or "").strip()
            db.add_book(conn, {
                "title": title, "author": author, "isbn": item.get("isbn", ""),
                "publisher": publisher, "edition": item.get("edition", ""), "year": item.get("year"),
                "cover_url": item.get("cover_url", ""), "shelf": shelf, "library": library,
                "source": item.get("source", "foto"),
            }, request.session["user"])
            added += 1
        conn.execute("DELETE FROM scans WHERE id = ?", (scan_id,))
    return back(f"/?added={added}&library={quote(library)}" + (f"&shelf={quote(shelf)}" if shelf else "")
                + (f"&merged={merged}" if merged else ""))


# --- Importar y exportar --------------------------------------------------

# --- Código de barras -------------------------------------------------------

# A barcode scanned for a book that is already catalogued completes that book instead of adding it again: many
# books came from StoryGraph or a shelf photo with little more than a title, and the barcode knows much more.
MERGE_FIELDS = {"title": "título", "author": "autor", "isbn": "ISBN", "publisher": "editorial", "edition": "edición",
                "year": "año", "cover_url": "portada", "shelf": "estante"}
EDITION_FIELDS = ("publisher", "edition", "year", "cover_url")


def catalogued_copy(conn, code: str, found: dict | None, library: str):
    """The book a scanned barcode is, if already in this library: by ISBN, or else by title and author.
    Returns (book, can_merge); a title match with another ISBN is another edition, which isn't merged."""
    book = conn.execute("SELECT * FROM books WHERE isbn = ? AND library = ? ORDER BY id LIMIT 1",
                        (code, library)).fetchone()
    if book is None:
        # Imports keep ISBNs as written (often ISBN-10, or with dashes): compare them as ISBN-13.
        book = next((b for b in conn.execute("SELECT * FROM books WHERE isbn != '' AND library = ? ORDER BY id",
                                             (library,)) if lookup.clean_isbn(b["isbn"]) == code), None)
    if book is not None:
        return book, True
    if not found or not found.get("title"):
        return None, False
    book = conn.execute("SELECT * FROM books WHERE match_key = ? AND library = ? ORDER BY isbn != '', id LIMIT 1",
                        (db.match_key(found["title"], found.get("author", "")), library)).fetchone()
    if book is None:
        return None, False
    return book, not book["isbn"]


def merge_plan(book, found: dict, code: str, shelf: str = "") -> list[dict]:
    """What the barcode's data would change in a catalogued book, field by field. Empty fields are filled; when
    the book had no ISBN, the copy in hand defines its edition, so publisher, edition, year and cover are replaced
    too. Anything else that differs (a title or author written another way) is offered but not ticked. Notes,
    library and loans are never touched, a shelf only when the book had none, and a cover picked by hand stays."""
    new_edition = not book["isbn"]
    offered = {**found, "isbn": code, "shelf": shelf}
    plan = []
    for f, name in MERGE_FIELDS.items():
        old, new = book[f], offered.get(f)
        if not new or new == old:
            continue
        if f in ("title", "author") and old and db._fold(old).split() == db._fold(str(new)).split():
            continue
        if f == "cover_url" and book["cover_manual"]:
            continue
        if old and f in ("shelf", "isbn"):
            continue
        checked = not old or (new_edition and f in EDITION_FIELDS)
        plan.append({"field": f, "name": name, "old": old or "", "new": new, "checked": checked})
    return plan


def apply_merge(conn, book_id: int, changes: dict) -> None:
    if not changes:
        return
    if "year" in changes:
        year = str(changes["year"] or "").strip()
        changes["year"] = int(year) if year.isdigit() else None
    conn.execute(f"UPDATE books SET {', '.join(f'{f} = ?' for f in changes)} WHERE id = ?",
                 [*changes.values(), book_id])
    if "cover_url" in changes:
        photo = str(changes["cover_url"]).startswith(db.PHOTO_COVER_PREFIX)
        conn.execute("UPDATE books SET cover_checked = 1, cover_manual = ? WHERE id = ?", (int(photo), book_id))
    if "title" in changes or "author" in changes:
        row = conn.execute("SELECT title, author FROM books WHERE id = ?", (book_id,)).fetchone()
        conn.execute("UPDATE books SET match_key = ? WHERE id = ?", (db.match_key(row["title"], row["author"]), book_id))

def isbn_form(request: Request, **ctx):
    with db.connect() as conn:
        return render(request, "isbn.html", library=current_library(request),
                      shelves=shelves(conn, current_library(request)), **ctx)


@app.get("/isbn", response_class=HTMLResponse)
def isbn_page(request: Request, added: int = 0, merged: int = 0, filled: str = ""):
    with db.connect() as conn:
        added_book = conn.execute("SELECT id, title FROM books WHERE id = ?", (added,)).fetchone() if added else None
        merged_book = conn.execute("SELECT id, title FROM books WHERE id = ?", (merged,)).fetchone() if merged else None
    return isbn_form(request, added=added_book, merged=merged_book, filled=filled,
                     shelf=request.session.get("isbn_shelf", ""))


@app.post("/isbn", response_class=HTMLResponse)
async def isbn_lookup(request: Request, isbn: str = Form(""), library: str = Form(""), shelf: str = Form("")):
    library = remember_library(request, library)
    shelf = shelf.strip()
    request.session["isbn_shelf"] = shelf
    code = lookup.clean_isbn(isbn)
    if not code:
        return isbn_form(request, isbn=isbn, shelf=shelf,
                         error="Ese número no es un ISBN. Revisa que sea el código de barras que empieza con 978 o 979.")
    error = None
    try:
        found = await lookup.by_isbn(code)
    except lookup.LookupFailed as e:
        found, error = None, f"{lookup.failure_note(e)}. Vuelve a buscar en un rato o saca una foto a la portada."
    with db.connect() as conn:
        dup, can_merge = catalogued_copy(conn, code, found, library)
    if dup is not None and can_merge:
        # Already catalogued: offer what the barcode adds to it instead of adding it twice.
        plan = merge_plan(dup, found, code, shelf) if found else []
        return isbn_form(request, isbn="", shelf=shelf, dup=dup, plan=plan, found_for_dup=found or {"isbn": code})
    if dup is not None:
        error = error or f"Ya tienes otra edición de este libro en {library}."
    if found is None:
        found = {"title": "", "author": "", "publisher": "", "edition": "", "year": None, "cover_url": "", "isbn": code}
        error = error or ("No encontré este ISBN en Open Library ni en Google Books. Saca una foto a la portada"
                          " y Claude completa los datos, o escríbelos a mano.")
    return isbn_form(request, found=found, shelf=shelf, error=error, other_edition=dup)


@app.post("/books/{book_id}/unificar")
async def merge_scanned(request: Request, book_id: int):
    """Applies the changes ticked on the /isbn page to a book that was already catalogued."""
    form = await request.form()
    changes = {f: (form.get(f"new_{f}") or "").strip() for f in form.getlist("use") if f in MERGE_FIELDS}
    changes = {f: v for f, v in changes.items() if v}
    with db.connect() as conn:
        if conn.execute("SELECT id FROM books WHERE id = ?", (book_id,)).fetchone() is None:
            return back("/isbn")
        apply_merge(conn, book_id, changes)
    return back(f"/isbn?merged={book_id}&filled={quote(', '.join(MERGE_FIELDS[f] for f in changes))}")


BATCH_LIMIT = 150


def batch_form(request: Request, error: str | None = None):
    library = current_library(request)
    with db.connect() as conn:
        return render(request, "isbn_batch.html", library=library, shelves=shelves(conn, library),
                      shelf=request.session.get("isbn_shelf", ""), error=error)


@app.get("/isbn/lote", response_class=HTMLResponse)
def isbn_batch_page(request: Request):
    return batch_form(request)


@app.post("/isbn/lote")
async def isbn_batch(request: Request, isbns: str = Form(""), library: str = Form(""), shelf: str = Form("")):
    """Looks up every scanned ISBN at once and opens the same review page a shelf photo uses."""
    library = remember_library(request, library)
    shelf = shelf.strip()
    request.session["isbn_shelf"] = shelf
    codes = list(dict.fromkeys(c for c in map(lookup.clean_isbn, isbns.split()) if c))[:BATCH_LIMIT]
    if not codes:
        return batch_form(request, "La lista no tiene ningún ISBN válido. Escanea los códigos que empiezan con 978 o 979.")
    results = await lookup.by_isbn_many(codes)
    items = []
    with db.connect() as conn:
        for code, found in zip(codes, results):
            failed = isinstance(found, lookup.LookupFailed)
            book = found if isinstance(found, dict) else {}
            item = {"title": book.get("title", ""), "author": book.get("author", ""),
                    "publisher": book.get("publisher", ""), "edition": book.get("edition", ""),
                    "year": book.get("year"), "cover_url": book.get("cover_url", ""), "isbn": code,
                    "matched": bool(book), "source": "codigo", "confidence": "high" if book else "low",
                    "note": "" if book else (lookup.failure_note(found) if failed else
                                             "No encontré este ISBN en Open Library ni en Google Books")}
            dup, can_merge = catalogued_copy(conn, code, book, library)
            item["duplicate_id"] = dup["id"] if dup else None
            if dup is not None and can_merge and book:
                item["merge"] = [c for c in merge_plan(dup, item, code, shelf) if c["checked"]]
            items.append(item)
        scan_id = db.save_scan(conn, shelf, items, request.session["user"], library)
    return back(f"/scan/{scan_id}")


# --- Foto de la portada ------------------------------------------------------
# For books neither Open Library nor Google Books knows: Claude reads the cover (and the copyright page, if
# photographed too) and the cover photo itself becomes the book's cover.

COVER_SIDE = 800


def photo_covers_dir() -> Path:
    return db.DATA_DIR / "portadas"


def save_cover_photo(data: bytes) -> str:
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    img.thumbnail((COVER_SIDE, COVER_SIDE))
    photo_covers_dir().mkdir(parents=True, exist_ok=True)
    name = f"{uuid.uuid4().hex}.jpg"
    img.save(photo_covers_dir() / name, format="JPEG", quality=82)
    return db.PHOTO_COVER_PREFIX + name


@app.get("/portadas/{name}")
def photo_cover(name: str):
    path = photo_covers_dir() / name
    if not re.fullmatch(r"[0-9a-f]{32}\.jpg", name) or not path.exists():
        return Response(status_code=404)
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "max-age=31536000, immutable"})


async def read_cover_photos(form) -> tuple[dict | None, str | None]:
    """The book in the uploaded photos (fields "portada" and "legal"), or (None, error message)."""
    images = []
    for field in ("portada", "legal"):
        upload = form.get(field)
        if upload is not None and getattr(upload, "filename", ""):
            data = await upload.read()
            if data:
                images.append(data)
    if not images:
        return None, "Saca una foto a la portada."
    try:
        found = await vision.read_cover(images)
    except vision.VisionError as e:
        return None, str(e)
    except Exception:
        return None, "No se pudo leer la foto. ¿Es una imagen en JPG o PNG?"
    if not found["title"]:
        return None, "Claude no pudo leer el título en la foto. Prueba con otra, más de cerca y con buena luz."
    year = lookup._year(found["year"])
    book = {"title": found["title"], "author": found["author"], "publisher": found["publisher"],
            "edition": found["edition"], "year": year, "isbn": lookup.clean_isbn(found["isbn"]), "cover_url": ""}
    if found["front_cover"] >= 0:
        try:
            book["cover_url"] = save_cover_photo(images[found["front_cover"]])
        except Exception:
            pass  # the details are still worth keeping without the picture
    return book, None


def fill_empty(target: dict, found: dict, fields) -> list[str]:
    """Copies found values into target's empty fields; returns the fields filled."""
    filled = []
    for f in fields:
        if found.get(f) and not target.get(f):
            target[f] = found[f]
            filled.append(f)
    return filled


@app.post("/isbn/portada", response_class=HTMLResponse)
async def isbn_cover_photo(request: Request):
    """The /isbn page's book, completed from photos of its cover."""
    form = await request.form()
    current = {f: (form.get(f) or "").strip() for f in ("isbn", "title", "author", "publisher", "edition",
                                                          "cover_url")}
    year = (form.get("year") or "").strip()
    current["year"] = int(year) if year.isdigit() else None
    remember_library(request, form.get("library"))
    shelf = (form.get("shelf") or "").strip()
    found, error = await read_cover_photos(form)
    if found:
        if not current["isbn"]:
            current["isbn"] = found["isbn"]
        fill_empty(current, found, ("title", "author", "publisher", "edition", "year", "cover_url"))
    return isbn_form(request, found=current, shelf=shelf, error=error, from_photo=bool(found))


@app.post("/books/{book_id}/portada")
async def book_cover_photo(request: Request, book_id: int):
    """Fills a book's empty fields from photos of its cover; the photo becomes the cover if it had none."""
    with db.connect() as conn:
        book = conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
    if book is None:
        return back("/")
    found, error = await read_cover_photos(await request.form())
    if not found:
        return back(f"/books/{book_id}?photo_error={quote(error)}")
    values = {f: found[f] for f in ("author", "isbn", "publisher", "edition", "year", "cover_url")
              if found[f] and not book[f]}
    if not values:
        return back(f"/books/{book_id}?photo_error={quote('La foto no agregó datos que le falten a este libro.')}")
    if values:
        with db.connect() as conn:
            extra = ", cover_manual = 1, cover_checked = 1" if "cover_url" in values else ""
            conn.execute(f"UPDATE books SET {', '.join(f'{f} = ?' for f in values)}{extra} WHERE id = ?",
                         [*values.values(), book_id])
    names = {**FIELD_NAMES, "edition": "edición"}
    return back(f"/books/{book_id}?completed={quote(', '.join(names[f] for f in values))}")


@app.post("/scan/{scan_id}/portada/{index}")
async def scan_item_cover_photo(request: Request, scan_id: int, index: int):
    """Completes one book on the review page from photos of its cover."""
    form = await request.form()
    with db.connect() as conn:
        data = db.load_scan(conn, scan_id)
    if data is None:
        return back("/scan")
    if not 0 <= index < len(data["items"]):
        return back(f"/scan/{scan_id}")
    found, error = await read_cover_photos(form)
    item = data["items"][index]
    if found:
        if not item.get("isbn"):
            item["isbn"] = found["isbn"]
        fill_empty(item, found, ("title", "author", "publisher", "edition", "year", "cover_url"))
        item.update(confidence="high", note="Datos leídos de la foto", photo_error="")
    else:
        item["photo_error"] = error
    with db.connect() as conn:
        db.update_scan_items(conn, scan_id, data["items"])
    return back(f"/scan/{scan_id}#libro-{index}")


@app.get("/import", response_class=HTMLResponse)
def import_page(request: Request):
    return render_import(request)


@app.post("/import", response_class=HTMLResponse)
async def import_csv(request: Request, file: UploadFile = File(...), only_owned: str = Form(""),
                     library: str = Form("")):
    library = remember_library(request, library)
    data = await file.read()
    owned_filter = bool(only_owned) and storygraph.has_owned_column(data)
    books, skipped_not_owned = storygraph.parse(data, only_owned=owned_filter)
    added = duplicates = 0
    with db.connect() as conn:
        for book in books:
            book["library"] = library
            if db.find_duplicate(conn, book["title"], book["author"], library):
                duplicates += 1
                continue
            db.add_book(conn, book, request.session["user"])
            added += 1
    if added:
        covers.start()
    result = {"added": added, "duplicates": duplicates, "skipped": skipped_not_owned,
              "owned_missing": bool(only_owned) and not owned_filter}
    return render_import(request, result)


@app.post("/covers")
async def fill_covers(retry: str = Form("")):
    if retry:
        # Books looked up before without a match get another chance (e.g. after Open Library was down).
        with db.connect() as conn:
            conn.execute("UPDATE books SET cover_checked = 0 WHERE cover_url = ''")
    covers.start()
    return back("/import")


@app.get("/export.csv")
def export_csv():
    out = io.StringIO()
    writer = csv.writer(out)
    cols = ["title", "author", "isbn", "publisher", "edition", "year", "library", "shelf", "notes", "added_by",
            "created_at"]
    writer.writerow(cols + ["lent_to", "lent_on"])
    with db.connect() as conn:
        for row in conn.execute(
                f"SELECT {', '.join('books.' + c for c in cols)}, loans.person, loans.lent_on FROM books"
                " LEFT JOIN loans ON loans.book_id = books.id AND loans.returned_on IS NULL"
                " ORDER BY library, author, title"):
            writer.writerow(list(row))
    return Response(out.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=biblioteca.csv"})
