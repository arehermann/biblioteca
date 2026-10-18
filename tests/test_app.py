import pytest
from fastapi.testclient import TestClient

from app import auth, db, lookup, main, storygraph, vision

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

    async def no_lookup(items, concurrency=4):
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
    r = client.post("/scan", files={"photo": ("s.jpg", b"x", "image/jpeg")}, data={"shelf": "Living 1"},
                    follow_redirects=False)
    assert r.status_code == 303
    review = client.get(r.headers["location"]).text
    assert "Ya está en el catálogo" in review
    # Duplicate and low-confidence rows start unchecked; only Ficciones is pre-selected.
    assert review.count("checked") == 1

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
    r = client.post("/scan", files={"photo": ("s.jpg", b"x", "image/jpeg")})
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
