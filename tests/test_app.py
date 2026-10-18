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


def test_lookup_falls_back_from_isbn_to_title_and_raises_on_errors():
    import httpx

    calls = []

    def handler(request):
        calls.append(dict(request.url.params))
        if "isbn" in request.url.params:
            return httpx.Response(200, json={"docs": []})
        return httpx.Response(200, json={"docs": [{"title": "Ficciones", "author_name": ["Borges"], "cover_i": 7,
                                                    "isbn": ["0802130305", "9780802130303"]}]})

    async def run(h):
        async with httpx.AsyncClient(transport=httpx.MockTransport(h)) as c:
            return await lookup.lookup(c, "Ficciones", "Borges", "9789875666474")

    meta = asyncio.run(run(handler))
    assert meta["cover_url"].endswith("/7-M.jpg") and meta["isbn"] == "9780802130303"
    assert "isbn" in calls[0] and calls[1]["title"] == "Ficciones"

    with pytest.raises(lookup.LookupFailed):
        asyncio.run(run(lambda r: httpx.Response(503)))


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
                      "INSERT INTO books (title) VALUES ('Ficciones');")
    old.commit()
    old.close()
    db.init_db()
    with db.connect() as conn:
        assert conn.execute("SELECT library FROM books").fetchone()[0] == "Principal"


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

    ninos = client.get("/?library=Niños").text
    assert "Donde viven los monstruos" in ninos and "Left Hand" not in ninos
    principal = client.get("/?library=Principal").text
    assert "Left Hand" in principal and "Donde viven" not in principal

    # The scan remembered Niños, so the next form starts there.
    assert 'value="Niños" checked' in client.get("/scan").text

    with db.connect() as conn:
        book_id = conn.execute("SELECT id FROM books WHERE title LIKE 'The Left Hand%'").fetchone()[0]
    client.post("/books/move", data={"book": str(book_id), "to": "Niños"})
    assert "Left Hand" in client.get("/?library=Niños").text
    assert "Niños" in client.get("/export.csv").text


def test_scan_without_photo_explains(client):
    login(client)
    assert "Saca una foto o elige una de la galería" in client.post("/scan", data={"library": "Principal"}).text
