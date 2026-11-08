import pytest
from fastapi.testclient import TestClient

import asyncio
import io
import re

from app import auth, covers, db, lookup, main, storygraph, vision

CSV = (
    "Title,Authors,Contributors,ISBN/UID,Format,Read Status,Owned?\n"
    "Cien años de soledad,Gabriel García Márquez,,9780307474728,paperback,read,Yes\n"
    "Rayuela,Julio Cortázar,,notanisbn,paperback,to-read,No\n"
    '"The Left Hand of Darkness: 50th Anniversary",Ursula K. Le Guin,,0441478123,hardcover,read,Yes\n'
).encode()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    db.init_db()
    with db.connect() as conn:
        auth.create_user(conn, "anya", "secreta123")

    async def no_lookup(items, **kwargs):
        return [None] * len(items)

    monkeypatch.setattr(lookup, "lookup_many", no_lookup)
    with TestClient(main.app) as c:
        yield c


def login(c):
    r = c.post("/login", data={"username": "Anya", "password": "secreta123"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"


def test_storygraph_parse_owned_only():
    books, skipped = storygraph.parse(CSV, only_owned=True)
    assert [b["title"] for b in books] == ["Cien años de soledad", "The Left Hand of Darkness: 50th Anniversary"]
    assert books[0]["isbn"] == "9780307474728"
    assert skipped == 1


def test_storygraph_parse_all_drops_bad_isbn():
    books, _ = storygraph.parse(CSV, only_owned=False)
    assert len(books) == 3
    assert books[1]["isbn"] == ""


def test_match_key_ignores_accents_articles_and_subtitles():
    assert db.match_key("Cien Años de Soledad", "Gabriel García Márquez") == db.match_key("cien anos de soledad", "Márquez")
    assert db.match_key("The Left Hand of Darkness: 50th Anniversary", "Ursula K. Le Guin") == \
        db.match_key("Left Hand of Darkness", "Le Guin")


def test_password_hashing():
    h = auth.hash_password("abc12345")
    assert auth.verify_password("abc12345", h)
    assert not auth.verify_password("wrong", h)


def test_requires_login(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert client.post("/login", data={"username": "anya", "password": "nope"}).text.count("incorrectos") == 1



def test_installable_without_login(client):
    page = client.get("/login").text
    assert 'rel="manifest"' in page and 'register("/sw.js")' in page
    sw = client.get("/sw.js", follow_redirects=False)
    assert sw.status_code == 200 and "javascript" in sw.headers["content-type"]
    manifest = client.get("/static/manifest.webmanifest").json()
    assert manifest["display"] == "standalone" and manifest["start_url"] == "/"
    for icon in manifest["icons"]:
        assert client.get(icon["src"]).status_code == 200
    assert client.get("/static/offline.html").status_code == 200

def test_import_skips_duplicates(client):
    login(client)
    r = client.post("/import", files={"file": ("sg.csv", CSV, "text/csv")}, data={"only_owned": "1"})
    assert "Se importaron 2 libros" in r.text
    r = client.post("/import", files={"file": ("sg.csv", CSV, "text/csv")}, data={"only_owned": "1"})
    assert "Se importaron 0 libros" in r.text and "2 ya estaban" in r.text
    assert "Cien años de soledad" in client.get("/").text


def test_scan_review_and_save(client, monkeypatch):
    login(client)
    client.post("/import", files={"file": ("sg.csv", CSV, "text/csv")}, data={"only_owned": "1"})

    async def fake_read(data, client=None):
        return [
            {"title": "Cien años de soledad", "author": "García Márquez", "confidence": "high"},
            {"title": "Ficciones", "author": "Borges", "confidence": "high"},
            {"title": "Pedro Pár", "author": "", "confidence": "low"},
        ]

    monkeypatch.setattr(vision, "read_spines", fake_read)
    r = client.post("/scan", files={"camera": ("s.jpg", b"x", "image/jpeg")}, data={"shelf": "Living 1"},
                    follow_redirects=False)
    assert r.status_code == 303
    review = client.get(r.headers["location"]).text
    assert "Ya está en Principal" in review
    # Duplicate and low-confidence rows start unchecked; only Ficciones is pre-selected (plus the library radio).
    assert review.count("checked") == 2

    r = client.post(r.headers["location"], data={"shelf": "Living 1", "add_1": "1", "add_2": "1",
                                                 "title_2": "Pedro Páramo", "author_2": "Juan Rulfo"},
                    follow_redirects=False)
    assert "added=2" in r.headers["location"]
    page = client.get("/?shelf=Living 1").text
    assert "Ficciones" in page and "Pedro Páramo" in page and "Cien años" not in page


def test_scan_error_is_shown(client, monkeypatch):
    login(client)

    async def failing(data, client=None):
        raise vision.VisionError("La clave de la API de Claude no es válida.")

    monkeypatch.setattr(vision, "read_spines", failing)
    r = client.post("/scan", files={"gallery": ("s.jpg", b"x", "image/jpeg")})
    assert "no es válida" in r.text


def test_edit_delete_export(client):
    login(client)
    r = client.post("/books/new", data={"title": "Ficciones", "author": "Jorge Luis Borges", "shelf": "Pieza"},
                    follow_redirects=False)
    book_url = r.headers["location"].split("?")[0]
    client.post(book_url, data={"title": "Ficciones", "author": "Borges", "year": "1944", "shelf": "Pieza"})
    assert "1944" in client.get(book_url).text
    assert "Ficciones,Borges" in client.get("/export.csv").text
    client.post(book_url + "/delete")
    assert "Ficciones" not in client.get("/").text


GOODREADS = (
    "Book Id,Title,Author,Author l-f,Additional Authors,ISBN,ISBN13,My Rating,Exclusive Shelf,Owned Copies\n"
    '1,Ficciones,Jorge Luis Borges,"Borges, Jorge Luis",,"=""0802130305""","=""9780802130303""",5,read,1\n'
    '2,Rayuela,Julio Cortázar,"Cortázar, Julio",,"=""""","=""9788437604572""",4,to-read,0\n'
).encode()


def test_goodreads_parse():
    books, skipped = storygraph.parse(GOODREADS, only_owned=True)
    assert [(b["title"], b["author"], b["isbn"], b["source"]) for b in books] == \
        [("Ficciones", "Jorge Luis Borges", "9780802130303", "goodreads")]
    assert skipped == 1
    books, _ = storygraph.parse(GOODREADS, only_owned=False)
    assert books[1]["isbn"] == "9788437604572"


def test_goodreads_nothing_owned_explains(client):
    login(client)
    csv_none = GOODREADS.replace(b",1\n", b",0\n")
    r = client.post("/import", files={"file": ("gr.csv", csv_none, "text/csv")}, data={"only_owned": "1"})
    assert "Ningún libro del archivo estaba marcado como tuyo" in r.text


def test_cover_batch_retries_when_open_library_fails(client, monkeypatch):
    with db.connect() as conn:
        for t in ("Ficciones", "Rayuela", "Libro raro"):
            db.add_book(conn, {"title": t, "author": "X"})

    async def flaky(items, **kwargs):
        out = []
        for item in items:
            if item["title"] == "Ficciones":
                out.append({"cover_url": "https://covers/1.jpg", "isbn": "9780802130303", "year": 1944, "publisher": "Sur"})
            elif item["title"] == "Rayuela":
                out.append(lookup.LookupFailed("timeout"))
            else:
                out.append(None)
        return out

    monkeypatch.setattr(lookup, "lookup_many", flaky)
    processed, failed = asyncio.run(covers.fill_batch())
    assert (processed, failed) == (3, True)
    with db.connect() as conn:
        rows = {r["title"]: r for r in conn.execute("SELECT * FROM books")}
    assert rows["Ficciones"]["cover_url"] == "https://covers/1.jpg" and rows["Ficciones"]["isbn"] == "9780802130303"
    assert rows["Rayuela"]["cover_checked"] == 0  # retried later, not given up on
    assert rows["Libro raro"]["cover_checked"] == 1 and rows["Libro raro"]["cover_url"] == ""
    assert covers.remaining() == 1


def _ol_handler(routes):
    """Fake Open Library: maps a path to a JSON body (or a status code)."""
    import httpx

    calls = []

    def handler(request):
        calls.append((request.url.path, dict(request.url.params)))
        body = routes.get(request.url.path, {"docs": []} if request.url.path == "/search.json" else 404)
        if callable(body):
            body = body(dict(request.url.params))
        return httpx.Response(body, json={}) if isinstance(body, int) else httpx.Response(200, json=body)
    return handler, calls


def _run_lookup(handler, *args):
    import httpx

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
            return await lookup.lookup(c, *args)
    return asyncio.run(run())


EDITION = {"title": "Ficciones", "publishers": ["Emecé"], "publish_date": "1956", "edition_name": "2a ed.",
           "covers": [555], "isbn_10": ["9500404400"], "works": [{"key": "/works/OL1W"}], "key": "/books/OL9M"}
WORK_DOC = {"key": "/works/OL1W", "title": "Ficciones", "author_name": ["Borges"], "cover_i": 7,
            "isbn": ["0802130305", "9780802130303"], "publisher": ["Grove"]}


def test_lookup_prefers_the_exact_edition_by_isbn():
    handler, calls = _ol_handler({"/isbn/9500404400.json": EDITION, "/search.json": {"docs": [WORK_DOC]}})
    meta = _run_lookup(handler, "Ficciones", "Borges", "9500404400")
    assert meta["cover_url"].endswith("/555-M.jpg") and meta["publisher"] == "Emecé"
    assert meta["edition"] == "2a ed." and meta["year"] == 1956
    assert [c[0] for c in calls] == ["/isbn/9500404400.json"]


def test_edition_without_cover_borrows_the_work_cover():
    handler, _ = _ol_handler({"/isbn/9500404400.json": {**EDITION, "covers": [-1]},
                              "/search.json": {"docs": [WORK_DOC]}})
    meta = _run_lookup(handler, "Ficciones", "Borges", "9500404400")
    assert meta["cover_url"].endswith("/7-M.jpg") and meta["publisher"] == "Emecé"


def test_lookup_falls_back_to_title_and_uses_spine_publisher():
    def search(params):
        if "publisher" in params:
            return {"docs": [{**WORK_DOC, "cover_i": 8}]}
        return {"docs": []}

    handler, calls = _ol_handler({"/search.json": search})
    meta = _run_lookup(handler, "Ficciones", "Borges", "9789875666474", "Debolsillo")
    assert meta["cover_url"].endswith("/8-M.jpg")
    assert calls[0][0] == "/isbn/9789875666474.json" and calls[-1][1]["publisher"] == "Debolsillo"


def test_lookup_raises_on_errors():
    handler, _ = _ol_handler({"/search.json": 503})
    with pytest.raises(lookup.LookupFailed):
        _run_lookup(handler, "Ficciones")


def test_editions_list_puts_covers_first(monkeypatch):
    import httpx

    editions = {"entries": [{**EDITION, "covers": [], "key": "/books/A", "publish_date": "2010"},
                            {**EDITION, "key": "/books/B"}]}
    handler, _ = _ol_handler({"/isbn/9500404400.json": EDITION, "/works/OL1W/editions.json": editions})
    real = httpx.AsyncClient
    monkeypatch.setattr(lookup.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    found = asyncio.run(lookup.editions("Ficciones", "Borges", "9500404400"))
    assert [e["edition_key"] for e in found] == ["/books/B", "/books/A"]


def test_choose_edition_and_manual_cover_are_kept(client, monkeypatch):
    login(client)
    started = []
    monkeypatch.setattr(covers, "start", lambda: started.append(1))
    with db.connect() as conn:
        book_id = db.add_book(conn, {"title": "Ficciones", "author": "Borges", "isbn": "0802130305"})
    client.post(f"/books/{book_id}/ediciones", data={"cover_url": "https://covers/555.jpg", "isbn": "9500404400",
                                                     "publisher": "Emecé", "edition": "2a ed.", "year": "1956"})
    with db.connect() as conn:
        b = conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
    assert (b["cover_url"], b["isbn"], b["publisher"], b["edition"], b["year"], b["cover_manual"]) == \
        ("https://covers/555.jpg", "9500404400", "Emecé", "2a ed.", 1956, 1)
    assert covers.remaining() == 0  # a picked cover is never searched again

    # Changing the ISBN of a book with an automatic cover sends it back to the cover search.
    with db.connect() as conn:
        other = db.add_book(conn, {"title": "Rayuela", "author": "Cortázar", "isbn": "1111111111"})
        conn.execute("UPDATE books SET cover_checked = 1, cover_url = 'x' WHERE id = ?", (other,))
    client.post(f"/books/{other}", data={"title": "Rayuela", "author": "Cortázar", "isbn": "2222222222",
                                         "cover_url": "x", "library": "Principal"})
    with db.connect() as conn:
        assert conn.execute("SELECT cover_checked FROM books WHERE id = ?", (other,)).fetchone()[0] == 0
    assert started


def test_loans_flow(client):
    login(client)
    with db.connect() as conn:
        book_id = db.add_book(conn, {"title": "Ficciones", "author": "Borges"})
    client.post(f"/books/{book_id}/prestar", data={"person": "Tía Marta", "lent_on": "2026-10-01", "notes": "vacaciones"})
    # Lending again while it is out does nothing.
    client.post(f"/books/{book_id}/prestar", data={"person": "Otro"})
    page = client.get(f"/books/{book_id}").text
    assert "<strong>Tía Marta</strong>" in page and "desde el 01/10/2026" in page
    assert "Prestado a Tía Marta" in client.get("/?lent=1").text
    loans = client.get("/prestamos").text
    assert "Tía Marta" in loans and "Otro" not in loans
    assert "Tía Marta,2026-10-01" in client.get("/export.csv").text

    with db.connect() as conn:
        loan_id = db.active_loan(conn, book_id)["id"]
    r = client.post(f"/loans/{loan_id}/devolver", data={"returned_on": "2026-10-15", "next": f"/books/{book_id}"},
                    follow_redirects=False)
    assert r.headers["location"] == f"/books/{book_id}"
    page = client.get(f"/books/{book_id}").text
    assert "Préstamos anteriores (1)" in page and "01/10/2026 → 15/10/2026" in page
    assert "Ficciones" not in client.get("/?lent=1").text
    # Suggests people lent to before.
    assert '<option value="Tía Marta">' in page
    r = client.post(f"/loans/{loan_id}/devolver", data={"next": "//evil.example"}, follow_redirects=False)
    assert r.headers["location"] == "/prestamos"


def test_deleting_a_book_removes_its_loans(client):
    login(client)
    with db.connect() as conn:
        book_id = db.add_book(conn, {"title": "Ficciones"})
        db.lend(conn, book_id, "Ana", "2026-10-01", "", "anya")
    client.post(f"/books/{book_id}/delete")
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM loans").fetchone()[0] == 0


def test_old_database_gets_library_column(tmp_path, monkeypatch):
    import sqlite3
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    old = sqlite3.connect(tmp_path / "biblioteca.db")
    old.executescript("CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT NOT NULL, author TEXT NOT NULL DEFAULT '',"
                      " isbn TEXT NOT NULL DEFAULT '', publisher TEXT NOT NULL DEFAULT '', year INTEGER,"
                      " cover_url TEXT NOT NULL DEFAULT '', shelf TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '',"
                      " source TEXT NOT NULL DEFAULT 'manual', match_key TEXT NOT NULL DEFAULT '',"
                      " added_by TEXT NOT NULL DEFAULT '', cover_checked INTEGER NOT NULL DEFAULT 0,"
                      " created_at TEXT NOT NULL DEFAULT (datetime('now')));"
                      "CREATE TABLE scans (id INTEGER PRIMARY KEY, shelf TEXT NOT NULL DEFAULT '', items TEXT NOT NULL,"
                      " created_by TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT (datetime('now')));"
                      "INSERT INTO books (title, isbn, cover_url, cover_checked) VALUES ('Ficciones', '0802130305', 'x', 1);"
                      "INSERT INTO books (title, cover_url, cover_checked) VALUES ('Sin ISBN', 'y', 1);")
    old.commit()
    old.close()
    db.init_db()
    with db.connect() as conn:
        rows = conn.execute("SELECT library, cover_checked FROM books ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [("Principal", 0), ("Principal", 1)]  # ISBN books recheck their edition
    db.init_db()  # running again changes nothing
    with db.connect() as conn:
        conn.execute("UPDATE books SET cover_checked = 1")
    db.init_db()
    with db.connect() as conn:
        assert conn.execute("SELECT MIN(cover_checked) FROM books").fetchone()[0] == 1


def test_two_libraries_scan_import_move_and_filter(client, monkeypatch):
    login(client)
    client.post("/import", files={"file": ("sg.csv", CSV, "text/csv")}, data={"only_owned": "1", "library": "Principal"})

    async def fake_read(data, client=None):
        return [{"title": "Cien años de soledad", "author": "García Márquez", "confidence": "high"},
                {"title": "Donde viven los monstruos", "author": "Maurice Sendak", "confidence": "high"}]

    monkeypatch.setattr(vision, "read_spines", fake_read)
    r = client.post("/scan", files={"gallery": ("s.jpg", b"x", "image/jpeg")}, data={"library": "Niños"},
                    follow_redirects=False)
    review = client.get(r.headers["location"]).text
    # A copy in Principal is not a duplicate for the kids' library.
    assert "Ya está en" not in review and review.count("checked") == 3  # 2 books + the Niños radio
    r = client.post(r.headers["location"], data={"library": "Niños", "add_0": "1", "add_1": "1"},
                    follow_redirects=False)
    assert "library=Ni%C3%B1os" in r.headers["location"]

    # The scan remembered Niños, so the next form and the catalog start there, in the kids' look.
    assert 'value="Niños" checked' in client.get("/scan").text
    home = client.get("/").text
    assert 'class="theme-ludica"' in home and "Donde viven los monstruos" in home

    ninos = client.get("/?library=Niños").text
    assert "Donde viven los monstruos" in ninos and "Left Hand" not in ninos
    principal = client.get("/?library=Principal").text
    assert "Left Hand" in principal and "Donde viven" not in principal and 'class="theme-noche"' in principal
    # Browsing a library makes it the current one.
    assert 'value="Principal" checked' in client.get("/scan").text
    both = client.get("/?library=todas").text
    assert "Left Hand" in both and "Donde viven" in both

    with db.connect() as conn:
        book_id = conn.execute("SELECT id FROM books WHERE title LIKE 'The Left Hand%'").fetchone()[0]
    client.post("/books/move", data={"book": str(book_id), "to": "Niños"})
    assert "Left Hand" in client.get("/?library=Niños").text
    assert "Niños" in client.get("/export.csv").text


def test_scan_without_photo_explains(client):
    login(client)
    assert "Saca una foto o elige una de la galería" in client.post("/scan", data={"library": "Principal"}).text


def test_clean_isbn():
    assert lookup.clean_isbn("978-84-9908-950-8") == "9788499089508"
    assert lookup.clean_isbn("0802130305") == "9780802130303"  # ISBN-10 becomes ISBN-13
    assert lookup.clean_isbn("9788499089500") == ""  # bad check digit
    assert lookup.clean_isbn("7790001234567") == ""  # a product barcode, not a book


def test_by_isbn_joins_open_library_and_fills_gaps_from_google(monkeypatch):
    import httpx

    edition = {**EDITION, "covers": [], "isbn_13": ["9789500404405"]}
    google = {"items": [{"volumeInfo": {"title": "Ficciones (Google)", "authors": ["Jorge Luis Borges"],
                                        "imageLinks": {"thumbnail": "http://books.google.com/x.jpg"}}}]}
    handler, calls = _ol_handler({"/isbn/9789500404405.json": edition, "/search.json": {"docs": [WORK_DOC]},
                                  "/books/v1/volumes": google})
    real = httpx.AsyncClient
    monkeypatch.setattr(lookup.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    book = asyncio.run(lookup.by_isbn("9789500404405"))
    # Title, publisher and edition come from the exact edition, the author from the search, the cover from the work.
    assert book["title"] == "Ficciones" and book["publisher"] == "Emecé" and book["edition"] == "2a ed."
    assert book["author"] == "Borges" and book["cover_url"].endswith("/7-M.jpg")
    assert "/books/v1/volumes" not in [c[0] for c in calls]

    handler, _ = _ol_handler({"/books/v1/volumes": google})
    monkeypatch.setattr(lookup.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    book = asyncio.run(lookup.by_isbn("9789500404405"))
    assert book["title"] == "Ficciones (Google)" and book["cover_url"] == "https://books.google.com/x.jpg"


def test_add_book_from_barcode(client, monkeypatch):
    login(client)

    async def fake_by_isbn(isbn):
        return {"title": "Ficciones", "author": "Borges", "publisher": "Emecé", "edition": "", "year": 1956,
                "cover_url": "", "isbn": isbn}

    monkeypatch.setattr(lookup, "by_isbn", fake_by_isbn)
    assert "no es un ISBN" in client.post("/isbn", data={"isbn": "123", "library": "Niños"}).text
    page = client.post("/isbn", data={"isbn": "978-0-8021-3030-3", "library": "Niños", "shelf": "Pieza"}).text
    assert 'value="Ficciones"' in page and 'value="9780802130303"' in page and 'value="Niños" checked' in page

    r = client.post("/books/new", data={"next": "/isbn", "title": "Ficciones", "author": "Borges",
                                        "isbn": "9780802130303", "library": "Niños", "shelf": "Pieza"},
                    follow_redirects=False)
    assert r.headers["location"].startswith("/isbn?added=")
    after = client.get(r.headers["location"]).text
    assert "Se agregó" in after and 'value="Pieza"' in after  # the shelf carries over to the next book

    # Scanning it again in the same library says it is already there, and offers what its record lacks.
    again = client.post("/isbn", data={"isbn": "9780802130303", "library": "Niños"}).text
    assert "ya está en Niños" in again and 'name="new_publisher" value="Emecé"' in again
    with db.connect() as conn:
        conn.execute("UPDATE books SET publisher = 'Emecé', year = 1956")
    again = client.post("/isbn", data={"isbn": "9780802130303", "library": "Niños"}).text
    assert "ya tiene todo lo que trae el código" in again


def test_scan_many_barcodes_then_review_together(client, monkeypatch):
    login(client)
    with db.connect() as conn:
        db.add_book(conn, {"title": "Rayuela", "author": "Cortázar", "isbn": "9788437604572", "library": "Niños"})
    known = {
        "9780802130303": {"title": "Ficciones", "author": "Borges", "publisher": "Emecé", "edition": "", "year": 1956,
                          "cover_url": "https://covers/f.jpg"},
        "9788437604572": {"title": "Rayuela", "author": "Julio Cortázar", "publisher": "Cátedra", "edition": "",
                          "year": 1984, "cover_url": ""},
    }
    asked = []

    async def fake_by_isbn(isbn):
        asked.append(isbn)
        if isbn == "9789500404402":
            raise lookup.LookupFailed("timeout")
        return {**known[isbn], "isbn": isbn} if isbn in known else None

    monkeypatch.setattr(lookup, "by_isbn", fake_by_isbn)
    page = client.get("/isbn/lote").text
    assert "Varios códigos" in page and "/static/batch.js" in page
    assert "no tiene ningún ISBN" in client.post("/isbn/lote", data={"isbns": "123\n7790001234567"}).text

    # ISBN-10s become ISBN-13, repeats and junk are dropped, and each code is looked up once.
    codes = "9780802130303\n0802130305\n9788437604572\n9788499089508\nbasura\n9789500404402"
    r = client.post("/isbn/lote", data={"isbns": codes, "library": "Niños", "shelf": "Pieza"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/scan/")
    assert sorted(asked) == ["9780802130303", "9788437604572", "9788499089508", "9789500404402"]

    review = client.get(r.headers["location"]).text
    assert "Revisa los libros escaneados" in review and 'value="Pieza"' in review
    assert "Ya está en Niños" in review  # Rayuela, by its ISBN
    assert "Completar su ficha con editorial, año" in review  # the author written another way isn't offered
    assert "No encontré este ISBN" in review and "Open Library no respondió" in review
    assert "localStorage.removeItem('isbn-lote')" in review and 'href="/isbn/lote"' in review
    # Only Ficciones starts checked (plus the library radio and completing Rayuela): one is already there, two
    # have no title yet.
    assert review.count("checked") == 3

    # A title typed for an unknown ISBN adds it; a checked row left without a title is skipped.
    r = client.post(r.headers["location"], data={"library": "Niños", "shelf": "Pieza", "add_0": "1", "add_2": "1",
                                                 "title_2": "Diario de un gato", "add_3": "1"},
                    follow_redirects=False)
    assert "added=2" in r.headers["location"]
    with db.connect() as conn:
        rows = {b["title"]: b for b in conn.execute("SELECT * FROM books WHERE shelf = 'Pieza'")}
    assert set(rows) == {"Ficciones", "Diario de un gato"}
    assert rows["Ficciones"]["isbn"] == "9780802130303" and rows["Ficciones"]["library"] == "Niños"
    assert rows["Ficciones"]["source"] == "codigo" and rows["Diario de un gato"]["isbn"] == "9788499089508"
    with db.connect() as conn:
        rayuela = conn.execute("SELECT * FROM books WHERE title = 'Rayuela'").fetchone()
    # Its "complete" box was unticked here, so Rayuela stays as it was.
    assert rayuela["publisher"] == ""


def test_static_files_are_versioned_so_phones_reload_them(client):
    login(client)
    page = client.get("/isbn/lote").text
    assert '/static/style.css?v=' in page and '/static/batch.js?v=' in page
    assert '"/static/batch.js"' not in page


def test_match_key_ignores_series_and_edition_notes():
    assert db.match_key("Harry Potter y la piedra filosofal (Harry Potter, #1)", "J.K. Rowling") == \
        db.match_key("Harry Potter y la piedra filosofal", "J. K. Rowling")
    assert db.match_key("Rayuela - Edición conmemorativa", "Julio Cortázar") == db.match_key("Rayuela", "Cortázar")


def test_find_and_merge_duplicates(client):
    login(client)
    with db.connect() as conn:
        a = db.add_book(conn, {"title": "Ficciones", "author": "Borges", "shelf": "Living 1"})
        b = db.add_book(conn, {"title": "Ficciones (Spanish Edition)", "author": "Jorge Luis Borges",
                               "isbn": "9788499089508", "publisher": "Debolsillo", "notes": "de la abuela"})
        c = db.add_book(conn, {"title": "Ficciones", "author": ""})  # a spine read without author
        d = db.add_book(conn, {"title": "Ficciones", "author": "Borges", "library": "Niños"})  # other library
        e = db.add_book(conn, {"title": "Rayuela", "author": "Cortázar"})
        f = db.add_book(conn, {"title": "Rayuela", "author": "Cortázar"})
        db.lend(conn, b, "Lucía", "2026-10-01", "", "anya")
        groups = db.duplicate_groups(conn)
    assert sorted(sorted(x["id"] for x in g) for g in groups) == [[a, b, c], [e, f]]
    assert "2 posibles repetidos" in client.get("/?library=Principal").text

    page = client.get("/repetidos").text
    # The copy with ISBN and publisher is suggested.
    assert f'value="{b}" checked' in page
    client.post("/repetidos/unir", data={"ids": [a, b, c], "keep": a})
    with db.connect() as conn:
        kept = conn.execute("SELECT * FROM books WHERE id = ?", (a,)).fetchone()
        assert kept["isbn"] == "9788499089508" and kept["publisher"] == "Debolsillo" and kept["shelf"] == "Living 1"
        assert kept["notes"] == "de la abuela"
        assert db.active_loan(conn, a)["person"] == "Lucía"  # the loan moved over
        assert conn.execute("SELECT COUNT(*) FROM books WHERE id IN (?, ?)", (b, c)).fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM books WHERE id = ?", (d,)).fetchone()[0] == 1

    # Two real copies: marked as not repeated, they stop showing.
    client.post("/repetidos/ignorar", data={"ids": [e, f]})
    assert "No hay libros repetidos" in client.get("/repetidos").text


def test_complete_book_from_barcode(client, monkeypatch):
    login(client)
    with db.connect() as conn:
        book_id = db.add_book(conn, {"title": "Ficciones", "author": "", "publisher": "Adivinada", "year": 1990})

    async def fake_by_isbn(isbn):
        return {"title": "Ficciones", "author": "Jorge Luis Borges", "publisher": "Debolsillo", "edition": "3ª ed.",
                "year": 2011, "cover_url": "https://covers/x.jpg", "isbn": isbn}

    monkeypatch.setattr(lookup, "by_isbn", fake_by_isbn)
    assert "Para completar: autor, ISBN, cubierta" in client.get(f"/books/{book_id}").text
    r = client.post(f"/books/{book_id}/completar", data={"isbn": "9788499089508"}, follow_redirects=False)
    page = client.get(r.headers["location"]).text
    assert "Se completaron:" in page
    with db.connect() as conn:
        b = conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
    # A scanned ISBN is the edition in hand: its publisher and year replace the guesses.
    assert (b["author"], b["isbn"], b["publisher"], b["year"], b["cover_url"]) == \
        ("Jorge Luis Borges", "9788499089508", "Debolsillo", 2011, "https://covers/x.jpg")
    assert b["match_key"] == db.match_key("Ficciones", "Jorge Luis Borges")


# --- Google Books y fotos de portada -------------------------------------------

def _mock_http(monkeypatch, handler):
    import httpx
    real = httpx.AsyncClient
    monkeypatch.setattr(lookup.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))


GOOGLE_HIT = {"items": [{"volumeInfo": {
    "title": "Los niños del agua", "authors": ["Ana Pérez"], "publisher": "Ediciones B", "publishedDate": "2019-03",
    "industryIdentifiers": [{"type": "ISBN_13", "identifier": "9788466664714"}],
    "imageLinks": {"thumbnail": "http://books.google.com/agua.jpg"}}}]}


def test_google_books_uses_the_key_and_retries_when_busy(monkeypatch):
    import httpx
    monkeypatch.setenv("GOOGLE_BOOKS_API_KEY", "clave123")
    monkeypatch.setattr(lookup, "GOOGLE_RETRY_DELAYS", (0, 0))
    google_calls = []

    def handler(request):
        if request.url.path == "/books/v1/volumes":
            google_calls.append(dict(request.url.params))
            return httpx.Response(429 if len(google_calls) == 1 else 200, json=GOOGLE_HIT)
        return httpx.Response(200, json={"docs": []}) if request.url.path == "/search.json" else httpx.Response(404)

    _mock_http(monkeypatch, handler)
    book = asyncio.run(lookup.by_isbn("9788466664714"))
    assert book["title"] == "Los niños del agua" and book["year"] == 2019
    assert len(google_calls) == 2 and google_calls[0]["key"] == "clave123"


def test_by_isbn_reports_google_quota_instead_of_not_found(monkeypatch):
    import httpx
    monkeypatch.delenv("GOOGLE_BOOKS_API_KEY", raising=False)
    monkeypatch.setattr(lookup, "GOOGLE_RETRY_DELAYS", (0,))

    def handler(request):
        if request.url.path == "/books/v1/volumes":
            return httpx.Response(429, json={})
        return httpx.Response(200, json={"docs": []}) if request.url.path == "/search.json" else httpx.Response(404)

    _mock_http(monkeypatch, handler)
    with pytest.raises(lookup.GoogleFailed) as e:
        asyncio.run(lookup.by_isbn("9788466664714"))
    assert "GOOGLE_BOOKS_API_KEY" in lookup.failure_note(e.value)


def test_by_isbn_uses_google_when_open_library_is_down(monkeypatch):
    import httpx

    def handler(request):
        if request.url.path == "/books/v1/volumes":
            return httpx.Response(200, json=GOOGLE_HIT)
        return httpx.Response(503)

    _mock_http(monkeypatch, handler)
    assert asyncio.run(lookup.by_isbn("9788466664714"))["publisher"] == "Ediciones B"


def test_lookup_falls_back_to_google_by_title():
    handler, calls = _ol_handler({"/books/v1/volumes": GOOGLE_HIT})
    meta = _run_lookup(handler, "Los niños del agua", "Ana Pérez")
    assert meta["isbn"] == "9788466664714" and meta["cover_url"] == "https://books.google.com/agua.jpg"
    assert meta["source_name"] == "Google Books"
    assert calls[-1][1]["q"] == 'intitle:"Los niños del agua" inauthor:"Ana Pérez"'


def _jpeg() -> bytes:
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", (60, 90), "red").save(out, format="JPEG")
    return out.getvalue()


FROM_COVER = {"title": "Los niños del agua", "author": "Ana Pérez", "publisher": "Ediciones B", "year": "2019",
              "edition": "", "isbn": "978-84-666-6471-4", "front_cover": 0}


def test_unknown_isbn_completed_from_cover_photo(client, monkeypatch):
    login(client)
    seen = []

    async def fake_cover(images, client=None):
        seen.append(len(images))
        return dict(FROM_COVER)

    monkeypatch.setattr(vision, "read_cover", fake_cover)
    page = client.post("/isbn/portada", data={"isbn": "9788466664714", "library": "Principal"},
                       files=[("portada", ("p.jpg", _jpeg(), "image/jpeg")), ("legal", ("l.jpg", _jpeg(), "image/jpeg"))]).text
    assert seen == [2] and "Claude leyó estos datos" in page
    assert 'value="Los niños del agua"' in page and 'value="2019"' in page
    cover = re.search(r'name="cover_url" value="(/portadas/[0-9a-f]+\.jpg)"', page).group(1)
    assert client.get(cover).headers["content-type"] == "image/jpeg"
    assert client.get("/portadas/../biblioteca.db").status_code == 404

    client.post("/books/new", data={"title": "Los niños del agua", "isbn": "9788466664714", "cover_url": cover})
    with db.connect() as conn:
        b = conn.execute("SELECT cover_url, cover_manual, cover_checked FROM books").fetchone()
    assert tuple(b) == (cover, 1, 1)  # the background cover search leaves the photo alone


def test_book_completed_from_cover_photo(client, monkeypatch):
    login(client)
    with db.connect() as conn:
        book_id = db.add_book(conn, {"title": "Los niños del agua", "publisher": "B"})

    async def fake_cover(images, client=None):
        return dict(FROM_COVER)

    monkeypatch.setattr(vision, "read_cover", fake_cover)
    r = client.post(f"/books/{book_id}/portada", files={"portada": ("p.jpg", _jpeg(), "image/jpeg")},
                    follow_redirects=False)
    assert "completed=" in r.headers["location"]
    with db.connect() as conn:
        b = conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
    assert (b["author"], b["publisher"], b["isbn"], b["year"], b["cover_manual"]) == \
        ("Ana Pérez", "B", "9788466664714", 2019, 1)
    assert b["cover_url"].startswith("/portadas/")


def test_review_item_completed_from_cover_photo(client, monkeypatch):
    login(client)
    with db.connect() as conn:
        scan_id = db.save_scan(conn, "", [{"title": "", "author": "", "isbn": "9788466664714", "matched": False,
                                           "source": "codigo", "confidence": "low", "cover_url": ""}], "anya")
    assert 'action="/scan/%d/portada/0"' % scan_id in client.get(f"/scan/{scan_id}").text

    async def fake_cover(images, client=None):
        return {**FROM_COVER, "front_cover": -1}

    monkeypatch.setattr(vision, "read_cover", fake_cover)
    client.post(f"/scan/{scan_id}/portada/0", files={"portada": ("p.jpg", _jpeg(), "image/jpeg")})
    with db.connect() as conn:
        item = db.load_scan(conn, scan_id)["items"][0]
    assert (item["title"], item["author"], item["isbn"], item["confidence"]) == \
        ("Los niños del agua", "Ana Pérez", "9788466664714", "high")


def test_scanning_a_catalogued_book_completes_it(client, monkeypatch):
    login(client)
    with db.connect() as conn:
        # Imported from StoryGraph without ISBN; the barcode finds it by title and author.
        poor = db.add_book(conn, {"title": "Ficciones", "author": "Borges", "publisher": "Adivinada",
                                  "notes": "de la abuela", "cover_url": "https://covers/vieja.jpg"})
        other = db.add_book(conn, {"title": "Rayuela", "author": "Cortázar", "isbn": "9788437604572",
                                   "shelf": "Living"})

    async def fake_by_isbn(isbn):
        if isbn == "9788499089508":
            return {"title": "Ficciones", "author": "Jorge Luis Borges", "publisher": "Debolsillo",
                    "edition": "3ª ed.", "year": 2011, "cover_url": "https://covers/x.jpg", "isbn": isbn}
        return {"title": "Rayuela", "author": "Julio Cortázar", "publisher": "Alfaguara", "edition": "",
                "year": 2019, "cover_url": "https://covers/r.jpg", "isbn": isbn}

    monkeypatch.setattr(lookup, "by_isbn", fake_by_isbn)
    page = client.post("/isbn", data={"isbn": "9788499089508", "library": "Principal", "shelf": "Pieza"}).text
    assert f'action="/books/{poor}/unificar"' in page and "Agregar y escanear otro" not in page
    ticked = set(re.findall(r'name="use" value="(\w+)" checked', page))
    # No ISBN before: the copy in hand sets the edition. The author written in full is offered, not ticked.
    assert ticked == {"isbn", "publisher", "edition", "year", "cover_url", "shelf"}
    assert 'name="use" value="author"' in page

    form = {"use": sorted(ticked)}
    form.update({f"new_{f}": v for f, v in re.findall(r'name="new_(\w+)" value="([^"]*)"', page)})
    r = client.post(f"/books/{poor}/unificar", data=form, follow_redirects=False)
    assert r.headers["location"].startswith(f"/isbn?merged={poor}")
    assert "Se completó" in client.get(r.headers["location"]).text
    with db.connect() as conn:
        b = conn.execute("SELECT * FROM books WHERE id = ?", (poor,)).fetchone()
        assert conn.execute("SELECT COUNT(*) FROM books").fetchone()[0] == 2
    assert (b["author"], b["isbn"], b["publisher"], b["edition"], b["year"], b["cover_url"], b["shelf"],
            b["notes"]) == ("Borges", "9788499089508", "Debolsillo", "3ª ed.", 2011, "https://covers/x.jpg",
                            "Pieza", "de la abuela")

    # Found by ISBN: only empty fields are ticked, and its shelf is never moved.
    page = client.post("/isbn", data={"isbn": "9788437604572", "library": "Principal", "shelf": "Pieza"}).text
    assert set(re.findall(r'name="use" value="(\w+)" checked', page)) == {"publisher", "year", "cover_url"}
    assert 'value="shelf"' not in page


def test_batch_scan_completes_catalogued_books(client, monkeypatch):
    login(client)
    with db.connect() as conn:
        poor = db.add_book(conn, {"title": "Ficciones", "author": "Borges", "notes": "firmado"})
        other_edition = db.add_book(conn, {"title": "Rayuela", "author": "Cortázar", "isbn": "9788437604572"})

    async def fake_by_isbn(isbn):
        if isbn == "9788499089508":
            return {"title": "Ficciones", "author": "Borges", "publisher": "Debolsillo", "edition": "",
                    "year": 2011, "cover_url": "https://covers/x.jpg", "isbn": isbn}
        return {"title": "Rayuela", "author": "Cortázar", "publisher": "Cátedra", "edition": "", "year": 1984,
                "cover_url": "", "isbn": isbn}

    monkeypatch.setattr(lookup, "by_isbn", fake_by_isbn)
    r = client.post("/isbn/lote", data={"isbns": "9788499089508\n9789500404402", "library": "Principal"},
                    follow_redirects=False)
    review = client.get(r.headers["location"]).text
    assert "Completar su ficha con ISBN, editorial, año, cubierta" in review
    assert 'name="merge_1"' not in review  # another edition of Rayuela: not merged, shown as already there
    assert review.count("Ya está en Principal") == 2

    r = client.post(r.headers["location"], data={"library": "Principal", "merge_0": "1"}, follow_redirects=False)
    assert "merged=1" in r.headers["location"] and "added=0" in r.headers["location"]
    page = client.get(r.headers["location"]).text
    assert "Se completaron los datos de 1 libro" in page and "Se agregaron" not in page
    with db.connect() as conn:
        b = conn.execute("SELECT * FROM books WHERE id = ?", (poor,)).fetchone()
        r2 = conn.execute("SELECT * FROM books WHERE id = ?", (other_edition,)).fetchone()
    assert (b["isbn"], b["publisher"], b["year"], b["notes"]) == ("9788499089508", "Debolsillo", 2011, "firmado")
    assert r2["publisher"] == ""


def test_barcode_finds_a_book_stored_with_its_isbn_10(client, monkeypatch):
    login(client)
    with db.connect() as conn:
        # From StoryGraph with ISBN-10, plus a copy read from a shelf photo without ISBN.
        imported = db.add_book(conn, {"title": "The Left Hand of Darkness", "author": "Ursula K. Le Guin",
                                      "isbn": "0441478123"})
        db.add_book(conn, {"title": "The Left Hand of Darkness", "author": "Le Guin"})

    async def fake_by_isbn(isbn):
        return {"title": "The Left Hand of Darkness", "author": "Ursula K. Le Guin", "publisher": "Ace",
                "edition": "", "year": 1987, "cover_url": "", "isbn": isbn}

    monkeypatch.setattr(lookup, "by_isbn", fake_by_isbn)
    page = client.post("/isbn", data={"isbn": "9780441478125", "library": "Principal"}).text
    assert f'action="/books/{imported}/unificar"' in page
    assert set(re.findall(r'name="use" value="(\w+)" checked', page)) == {"publisher", "year"}


def test_google_plain_search_finds_isbns_its_isbn_filter_misses(monkeypatch):
    import httpx
    monkeypatch.setenv("GOOGLE_BOOKS_API_KEY", "clave123")
    hit = {"items": [
        {"volumeInfo": {"title": "Otro libro", "industryIdentifiers": [{"type": "ISBN_13", "identifier": "9788466664714"}]}},
        {"volumeInfo": {"title": "Montevideo de noche", "authors": ["Ana Ruiz"], "publisher": "Estuario",
                        "publishedDate": "2023", "imageLinks": {"thumbnail": "http://books.google.com/m.jpg"},
                        "industryIdentifiers": [{"type": "ISBN_13", "identifier": "9789915690940"}]}}]}

    def handler(request):
        if request.url.path == "/books/v1/volumes":
            q = request.url.params["q"]
            return httpx.Response(200, json={} if q.startswith("isbn:") else hit)
        return httpx.Response(404) if "/isbn/" in request.url.path else httpx.Response(200, json={"docs": []})

    _mock_http(monkeypatch, handler)
    book = asyncio.run(lookup.by_isbn("9789915690940"))
    assert (book["title"], book["publisher"], book["cover_url"]) == \
        ("Montevideo de noche", "Estuario", "https://books.google.com/m.jpg")


def test_batch_shows_the_catalogued_book_when_its_isbn_is_unknown(client, monkeypatch):
    login(client)
    with db.connect() as conn:
        db.add_book(conn, {"title": "Montevideo de noche", "author": "Ana Ruiz", "isbn": "9789915690940"})

    async def fake_by_isbn(isbn):
        return None

    monkeypatch.setattr(lookup, "by_isbn", fake_by_isbn)
    r = client.post("/isbn/lote", data={"isbns": "9789915690940", "library": "Principal"}, follow_redirects=False)
    review = client.get(r.headers["location"]).text
    assert 'value="Montevideo de noche"' in review and "Ya está en Principal" in review
    assert "No encontré este ISBN" not in review


# --- Buscar en la web ------------------------------------------------------------

class _Block:
    def __init__(self, type, text=""):
        self.type, self.text = type, text


class _FakeMessages:
    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class _Reply:
    def __init__(self, stop_reason, *blocks):
        self.stop_reason, self.content = stop_reason, list(blocks)


def test_search_web_reads_the_json_after_the_searches():
    import anthropic
    import httpx
    bad = anthropic.BadRequestError("no fetch", response=httpx.Response(400, request=httpx.Request("POST", "https://x")),
                                    body=None)
    messages = _FakeMessages([
        bad,  # web fetch not offered for this model: retried with search alone
        _Reply("pause_turn", _Block("text", "Busco {el ISBN}"), _Block("server_tool_use")),
        _Reply("end_turn", _Block("web_search_tool_result"),
               _Block("text", 'Lo encontré:\n{"title": "Montevideo de noche", "author": "Ana Ruiz", '),
               _Block("text", '"publisher": "Estuario", "year": "2023", "edition": "", "cover_image_url": ""}')),
    ])
    client = type("C", (), {"messages": messages})()
    found = asyncio.run(vision.search_web("9789915690940", client=client))
    assert found["title"] == "Montevideo de noche" and found["publisher"] == "Estuario" and found["year"] == "2023"
    assert messages.calls[0]["model"] == "claude-haiku-5-5" and len(messages.calls[0]["tools"]) == 2
    assert [t["name"] for t in messages.calls[1]["tools"]] == ["web_search"]
    assert messages.calls[2]["messages"][-1]["role"] == "assistant"  # the paused turn was sent back


def test_web_cover_download_refuses_the_home_network():
    assert not main._public_https("https://127.0.0.1/portada.jpg")
    assert not main._public_https("http://example.com/portada.jpg")
    assert not main._public_https("https://192.168.1.10/portada.jpg")
    assert asyncio.run(main.download_cover("https://10.0.0.1/x.jpg")) == ""


def _fake_web(monkeypatch, result):
    asked = []

    async def fake_search_web(isbn, title="", author="", client=None):
        asked.append((isbn, title))
        if isinstance(result, Exception):
            raise result
        return {"title": "", "author": "", "publisher": "", "year": "", "edition": "", "cover_image_url": "",
                **result}

    monkeypatch.setattr(vision, "search_web", fake_search_web)
    return asked


WEB_HIT = {"title": "Montevideo de noche", "author": "Ana Ruiz", "publisher": "Estuario", "year": "2023"}


def test_unknown_isbn_found_on_the_web(client, monkeypatch):
    login(client)

    async def nothing(isbn):
        return None

    monkeypatch.setattr(lookup, "by_isbn", nothing)
    page = client.post("/isbn", data={"isbn": "9789915690940", "library": "Principal"}).text
    assert 'form="web-form"' in page and "Buscar en la web" in page

    asked = _fake_web(monkeypatch, WEB_HIT)
    page = client.post("/isbn/web", data={"isbn": "9789915690940", "library": "Principal", "shelf": "Pieza"}).text
    assert asked == [("9789915690940", "")]
    assert "Claude encontró estos datos en la web" in page and 'value="Montevideo de noche"' in page
    assert 'value="Estuario"' in page and 'value="2023"' in page and 'form="web-form"' not in page

    _fake_web(monkeypatch, vision.VisionError("La clave de la API de Claude no es válida. Revisa ANTHROPIC_API_KEY."))
    page = client.post("/isbn/web", data={"isbn": "9789915690940", "library": "Principal"}).text
    assert "ANTHROPIC_API_KEY" in page


def test_review_item_and_book_found_on_the_web(client, monkeypatch):
    login(client)
    with db.connect() as conn:
        known = db.add_book(conn, {"title": "Montevideo de noche", "author": "", "isbn": "9789915690940",
                                   "notes": "regalo"})

    async def nothing(isbn):
        return None

    monkeypatch.setattr(lookup, "by_isbn", nothing)
    r = client.post("/isbn/lote", data={"isbns": "9788499089508\n9789915690940", "library": "Principal"},
                    follow_redirects=False)
    scan = r.headers["location"]
    review = client.get(scan).text
    assert f'action="{scan}/web/0"' in review and f'action="{scan}/web/1"' in review

    _fake_web(monkeypatch, {**WEB_HIT, "title": "Ficciones", "author": "Borges"})
    client.post(f"{scan}/web/0")
    review = client.get(scan).text
    assert 'value="Ficciones"' in review and "Encontrado en la web" in review

    _fake_web(monkeypatch, WEB_HIT)
    client.post(f"{scan}/web/1")
    review = client.get(scan).text
    assert "Completar su ficha con autor, editorial, año" in review  # the catalogued copy gets what it lacked

    # From the book's own page, only its empty fields are filled.
    assert f'action="/books/{known}/web"' in client.get(f"/books/{known}").text
    r = client.post(f"/books/{known}/web", follow_redirects=False)
    assert "Se completaron: autor, editorial, año" in client.get(r.headers["location"]).text
    with db.connect() as conn:
        b = conn.execute("SELECT * FROM books WHERE id = ?", (known,)).fetchone()
    assert (b["author"], b["publisher"], b["year"], b["notes"]) == ("Ana Ruiz", "Estuario", 2023, "regalo")


def test_book_page_offers_the_cover_found_on_the_web(client, monkeypatch):
    login(client)
    from PIL import Image
    with db.connect() as conn:
        book_id = db.add_book(conn, {"title": "Ficciones", "author": "Borges", "isbn": "9788499089508",
                                     "publisher": "Debolsillo", "year": 2011, "cover_url": "https://covers/mal.jpg"})
    page = client.get(f"/books/{book_id}").text
    assert f'action="/books/{book_id}/web"' in page and "centavos" not in page  # complete books can search too

    saved = []

    async def fake_download(url):
        buf = io.BytesIO()
        Image.new("RGB", (200, 300), "red").save(buf, format="JPEG")
        saved.append(main.save_cover_photo(buf.getvalue()))
        return saved[-1]

    monkeypatch.setattr(main, "download_cover", fake_download)
    _fake_web(monkeypatch, {"title": "Ficciones", "author": "Borges", "cover_image_url": "https://x/f.jpg"})
    r = client.post(f"/books/{book_id}/web", follow_redirects=False)
    page = client.get(r.headers["location"]).text
    assert "Claude encontró esta cubierta" in page and f'src="{saved[0]}"' in page
    assert "sin datos nuevos" not in page

    # Keeping the current one throws the found picture away.
    client.post(f"/books/{book_id}/cubierta", data={"cover_url": saved[0], "keep": "1"})
    assert not main._our_photo(saved[0]).exists()

    client.post(f"/books/{book_id}/web")
    client.post(f"/books/{book_id}/cubierta", data={"cover_url": saved[1], "use": "1"})
    with db.connect() as conn:
        b = conn.execute("SELECT cover_url, cover_manual FROM books WHERE id = ?", (book_id,)).fetchone()
    assert (b["cover_url"], b["cover_manual"]) == (saved[1], 1)

    # Only our own saved pictures can be chosen this way.
    client.post(f"/books/{book_id}/cubierta", data={"cover_url": "https://evil/x.jpg", "use": "1"})
    with db.connect() as conn:
        assert conn.execute("SELECT cover_url FROM books WHERE id = ?", (book_id,)).fetchone()[0] == saved[1]
