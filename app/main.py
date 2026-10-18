import csv
import io
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import auth, covers, db, lookup, storygraph, vision

BASE = Path(__file__).parent
PAGE_SIZE = 60
PUBLIC_PATHS = ("/login", "/static")

@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    covers.start()  # picks up books left without a cover by an earlier run
    yield


app = FastAPI(title="Biblioteca", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")


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


def render(request: Request, name: str, **ctx):
    return templates.TemplateResponse(request, name, {"user": request.session.get("user"), **ctx})


def render_import(request: Request, result=None):
    with db.connect() as conn:
        no_cover = conn.execute("SELECT COUNT(*) FROM books WHERE cover_url = '' AND cover_checked = 1").fetchone()[0]
    return render(request, "import.html", result=result, covers_left=covers.remaining(),
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
        "year": int(year) if year.isdigit() else None,
        "cover_url": (form.get("cover_url") or "").strip(),
        "shelf": (form.get("shelf") or "").strip(),
        "notes": (form.get("notes") or "").strip(),
    }


def shelves(conn) -> list[str]:
    return [r[0] for r in conn.execute("SELECT DISTINCT shelf FROM books WHERE shelf != '' ORDER BY shelf")]


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
def catalog(request: Request, q: str = "", shelf: str = "", page: int = 1):
    where, params = [], []
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
            f"SELECT * FROM books {clause} ORDER BY author COLLATE NOCASE, title COLLATE NOCASE LIMIT ? OFFSET ?",
            params + [PAGE_SIZE, (page - 1) * PAGE_SIZE],
        ).fetchall()
        all_count = conn.execute("SELECT COUNT(*) FROM books").fetchone()[0]
        shelf_list = shelves(conn)
    return render(request, "catalog.html", covers_running=covers.is_running(), covers_left=covers.remaining(),
                  books=books, total=total, all_count=all_count, q=q, shelf=shelf,
                  shelves=shelf_list, page=page, pages=max(1, -(-total // PAGE_SIZE)))


@app.get("/books/new", response_class=HTMLResponse)
def new_book_page(request: Request):
    with db.connect() as conn:
        return render(request, "book.html", book=None, shelves=shelves(conn))


@app.post("/books/new")
async def new_book(request: Request):
    book = form_book(await request.form())
    if not book["title"]:
        return back("/books/new")
    if not book["cover_url"]:
        meta = (await lookup.lookup_many([book]))[0]
        if meta:
            for k in ("isbn", "publisher", "year", "cover_url"):
                book[k] = book[k] or meta[k]
    with db.connect() as conn:
        book_id = db.add_book(conn, book, request.session["user"])
    return back(f"/books/{book_id}?saved=1")


@app.get("/books/{book_id}", response_class=HTMLResponse)
def book_page(request: Request, book_id: int, saved: int = 0):
    with db.connect() as conn:
        book = conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
        if book is None:
            return back("/")
        return render(request, "book.html", book=book, shelves=shelves(conn), saved=saved)


@app.post("/books/{book_id}")
async def edit_book(request: Request, book_id: int):
    book = form_book(await request.form())
    if book["title"]:
        with db.connect() as conn:
            db.update_book(conn, book_id, book)
    return back(f"/books/{book_id}?saved=1")


@app.post("/books/{book_id}/delete")
def delete_book(book_id: int):
    with db.connect() as conn:
        conn.execute("DELETE FROM books WHERE id = ?", (book_id,))
    return back("/")


# --- Fotos de estantes ----------------------------------------------------

@app.get("/scan", response_class=HTMLResponse)
def scan_page(request: Request):
    with db.connect() as conn:
        return render(request, "scan.html", shelves=shelves(conn), error=None)


@app.post("/scan")
async def scan(request: Request, photo: UploadFile = File(...), shelf: str = Form("")):
    data = await photo.read()
    try:
        found = await vision.read_spines(data)
    except vision.VisionError as e:
        with db.connect() as conn:
            return render(request, "scan.html", shelves=shelves(conn), error=str(e))
    except Exception:
        with db.connect() as conn:
            return render(request, "scan.html", shelves=shelves(conn),
                          error="No se pudo leer la imagen. ¿Es una foto (JPG, PNG o HEIC convertido)?")
    metas = await lookup.lookup_many(found)
    items = []
    with db.connect() as conn:
        for book, meta in zip(found, metas):
            dup = db.find_duplicate(conn, book["title"], book["author"])
            items.append({**book, **(meta or {}), "matched": bool(meta), "duplicate_id": dup["id"] if dup else None})
        scan_id = db.save_scan(conn, shelf.strip(), items, request.session["user"])
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
        for i, item in enumerate(data["items"]):
            if not form.get(f"add_{i}"):
                continue
            title = (form.get(f"title_{i}") or item["title"]).strip()
            author = (form.get(f"author_{i}") or item["author"]).strip()
            db.add_book(conn, {
                "title": title, "author": author, "isbn": item.get("isbn", ""),
                "publisher": item.get("publisher", ""), "year": item.get("year"),
                "cover_url": item.get("cover_url", ""), "shelf": shelf, "source": "foto",
            }, request.session["user"])
            added += 1
        conn.execute("DELETE FROM scans WHERE id = ?", (scan_id,))
    return back(f"/?added={added}" + (f"&shelf={shelf}" if shelf else ""))


# --- Importar y exportar --------------------------------------------------

@app.get("/import", response_class=HTMLResponse)
def import_page(request: Request):
    return render_import(request)


@app.post("/import", response_class=HTMLResponse)
async def import_csv(request: Request, file: UploadFile = File(...), only_owned: str = Form("")):
    data = await file.read()
    owned_filter = bool(only_owned) and storygraph.has_owned_column(data)
    books, skipped_not_owned = storygraph.parse(data, only_owned=owned_filter)
    added = duplicates = 0
    with db.connect() as conn:
        for book in books:
            if db.find_duplicate(conn, book["title"], book["author"]):
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
    cols = ["title", "author", "isbn", "publisher", "year", "shelf", "notes", "added_by", "created_at"]
    writer.writerow(cols)
    with db.connect() as conn:
        for row in conn.execute(f"SELECT {', '.join(cols)} FROM books ORDER BY author, title"):
            writer.writerow(list(row))
    return Response(out.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=biblioteca.csv"})
