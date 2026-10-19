import csv
import io
import os
from contextlib import asynccontextmanager
from pathlib import Path
from datetime import date
from urllib.parse import quote

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import auth, covers, db, lookup, storygraph, vision

BASE = Path(__file__).parent
PAGE_SIZE = 60
ALL = "todas"
PUBLIC_PATHS = ("/login", "/static")

@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    covers.start()  # picks up books left without a cover by an earlier run
    yield


app = FastAPI(title="Biblioteca", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")


def _fecha(iso: str | None) -> str:
    """2026-10-18 -> 18/10/2026"""
    return "/".join(reversed(iso[:10].split("-"))) if iso else ""


def _dias(iso: str | None) -> int:
    return (date.today() - date.fromisoformat(iso[:10])).days if iso else 0


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
        out_where = "AND books.library = ?" if library else ""
        out = conn.execute(
            "SELECT loans.person, loans.lent_on, books.id, books.title, books.cover_url FROM loans"
            f" JOIN books ON books.id = loans.book_id WHERE returned_on IS NULL {out_where}"
            " ORDER BY lent_on, loans.id", [library] if library else []).fetchall()
    return render(request, "catalog.html", covers_running=covers.is_running(), covers_left=covers.remaining(),
                  books=books, total=total, all_count=all_count, q=q, shelf=shelf, library=library, counts=counts,
                  lent=lent, out=out, lib_param=library or ALL, theme=None if library else "noche",
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


@app.get("/books/{book_id}", response_class=HTMLResponse)
def book_page(request: Request, book_id: int, saved: int = 0):
    with db.connect() as conn:
        book = conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
        if book is None:
            return back("/")
        loans = conn.execute("SELECT * FROM loans WHERE book_id = ? ORDER BY id DESC", (book_id,)).fetchall()
        return render(request, "book.html", book=book, shelves=shelves(conn), saved=saved, library=book["library"],
                      loan=db.active_loan(conn, book_id), loans=loans, borrowers=db.borrowers(conn),
                      today=date.today().isoformat())


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
    return render(request, "review.html", scan=data)


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
        for i, item in enumerate(data["items"]):
            if not form.get(f"add_{i}"):
                continue
            title = (form.get(f"title_{i}") or item["title"]).strip()
            author = (form.get(f"author_{i}") or item["author"]).strip()
            publisher = (form.get(f"publisher_{i}") or "").strip()
            db.add_book(conn, {
                "title": title, "author": author, "isbn": item.get("isbn", ""),
                "publisher": publisher, "edition": item.get("edition", ""), "year": item.get("year"),
                "cover_url": item.get("cover_url", ""), "shelf": shelf, "library": library, "source": "foto",
            }, request.session["user"])
            added += 1
        conn.execute("DELETE FROM scans WHERE id = ?", (scan_id,))
    return back(f"/?added={added}&library={quote(library)}" + (f"&shelf={quote(shelf)}" if shelf else ""))


# --- Importar y exportar --------------------------------------------------

# --- Código de barras -------------------------------------------------------

def isbn_form(request: Request, **ctx):
    with db.connect() as conn:
        return render(request, "isbn.html", library=current_library(request),
                      shelves=shelves(conn, current_library(request)), **ctx)


@app.get("/isbn", response_class=HTMLResponse)
def isbn_page(request: Request, added: int = 0):
    with db.connect() as conn:
        added_book = conn.execute("SELECT id, title FROM books WHERE id = ?", (added,)).fetchone() if added else None
    return isbn_form(request, added=added_book, shelf=request.session.get("isbn_shelf", ""))


@app.post("/isbn", response_class=HTMLResponse)
async def isbn_lookup(request: Request, isbn: str = Form(""), library: str = Form(""), shelf: str = Form("")):
    library = remember_library(request, library)
    shelf = shelf.strip()
    request.session["isbn_shelf"] = shelf
    code = lookup.clean_isbn(isbn)
    if not code:
        return isbn_form(request, isbn=isbn, shelf=shelf,
                         error="Ese número no es un ISBN. Revisa que sea el código de barras que empieza con 978 o 979.")
    with db.connect() as conn:
        dup = conn.execute("SELECT id, title FROM books WHERE isbn = ? AND library = ?", (code, library)).fetchone()
    if dup:
        return isbn_form(request, isbn="", shelf=shelf, dup=dup)
    error = None
    try:
        found = await lookup.by_isbn(code)
    except lookup.LookupFailed:
        found, error = None, "Open Library no respondió. Puedes completar los datos a mano."
    if found is None:
        found = {"title": "", "author": "", "publisher": "", "edition": "", "year": None, "cover_url": "", "isbn": code}
        error = error or "No encontré este ISBN en Open Library ni en Google Books. Puedes completar los datos a mano."
    return isbn_form(request, found=found, shelf=shelf, error=error)


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
