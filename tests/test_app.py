import pytest
from fastapi.testclient import TestClient

import asyncio

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

    # Scanning it again in the same library says it is already there.
    assert "ya está en Niños" in client.post("/isbn", data={"isbn": "9780802130303", "library": "Niños"}).text
