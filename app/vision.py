"""Reads book spines from a shelf photo, or a book's details from photos of its cover, with Claude."""
import base64
import io
import json

import anthropic
from PIL import Image, ImageOps

MODEL = "claude-opus-5-5"
MAX_SIDE = 2576

PROMPT = """This is a photo of a bookshelf. Read every book spine you can see, left to right and top to bottom.

For each book give the title and the author exactly as printed on the spine (keep the original language, fix only obvious OCR-style errors you are sure about). If the author is not visible, leave it empty. Also give the publisher or imprint when it is printed on the spine (usually a name or logo near the bottom, e.g. "Anagrama", "Alfaguara", "Penguin"); leave it empty if you can't read it. Skip objects that are not books.

confidence:
- "high": title clearly legible
- "medium": mostly legible, some letters guessed
- "low": partly hidden, blurry or very small text; include it anyway so a person can check it"""

SCHEMA = {
    "type": "object",
    "properties": {
        "books": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "author": {"type": "string"},
                    "publisher": {"type": "string"},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                },
                "required": ["title", "author", "publisher", "confidence"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["books"],
    "additionalProperties": False,
}


class VisionError(Exception):
    pass


def prepare_image(data: bytes) -> bytes:
    """Rotate per EXIF, shrink to MAX_SIDE and re-encode as JPEG so uploads stay small."""
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
    img = img.convert("RGB")
    img.thumbnail((MAX_SIDE, MAX_SIDE))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=85)
    return out.getvalue()


def _image_block(data: bytes) -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                         "data": base64.standard_b64encode(prepare_image(data)).decode()}}


async def _ask(content: list, schema: dict, client: anthropic.AsyncAnthropic | None, effort: str,
               max_tokens: int) -> dict:
    client = client or anthropic.AsyncAnthropic()
    try:
        response = await client.beta.messages.create(
            model=MODEL,
            max_tokens=max_tokens,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": content}],
        )
    except anthropic.AuthenticationError as e:
        raise VisionError("La clave de la API de Claude no es válida. Revisa ANTHROPIC_API_KEY.") from e
    except anthropic.RateLimitError as e:
        raise VisionError("Demasiadas fotos seguidas. Espera un minuto y vuelve a intentar.") from e
    except anthropic.APIStatusError as e:
        raise VisionError(f"La API de Claude respondió con un error ({e.status_code}).") from e
    except anthropic.APIConnectionError as e:
        raise VisionError("No se pudo conectar con la API de Claude. Revisa la conexión a internet.") from e

    if response.stop_reason == "refusal":
        raise VisionError("Claude no pudo procesar esta foto. Prueba con otra.")
    if response.stop_reason == "max_tokens":
        raise VisionError("La foto tiene demasiados libros. Prueba con una foto de una sola repisa.")
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        return json.loads(text)
    except ValueError as e:
        raise VisionError("La respuesta de Claude no se pudo leer. Vuelve a intentar.") from e


async def read_spines(image_bytes: bytes, client: anthropic.AsyncAnthropic | None = None) -> list[dict]:
    data = await _ask([_image_block(image_bytes), {"type": "text", "text": PROMPT}], SCHEMA, client, "medium", 16000)
    books = data.get("books") if isinstance(data, dict) else None
    if not isinstance(books, list):
        raise VisionError("La respuesta de Claude no se pudo leer. Vuelve a intentar.")
    return [b for b in books if b.get("title", "").strip()]


COVER_PROMPT = """These are photos of one book: its front cover, and maybe its copyright page (the page with the publisher, year and ISBN) or back cover.

Give the book's details exactly as printed (keep the original language):
- title: the main title, with the subtitle after a colon only if it is clearly part of the title
- author: the author(s), separated by commas; empty if none is printed
- publisher: the publisher or imprint
- year: the year this edition was published or printed (on the copyright page, the most recent printing year of this edition); empty if not printed
- edition: edition or printing statement such as "2a ed." or "Edición de bolsillo"; empty if none
- isbn: the ISBN printed on the copyright page or back cover, digits only; empty if none
- front_cover: the number of the photo (1 for the first) that shows the front cover, or 0 if none does

Only report text you can actually read; leave a field empty rather than guess."""

COVER_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "author": {"type": "string"},
        "publisher": {"type": "string"},
        "year": {"type": "string"},
        "edition": {"type": "string"},
        "isbn": {"type": "string"},
        "front_cover": {"type": "integer"},
    },
    "required": ["title", "author", "publisher", "year", "edition", "isbn", "front_cover"],
    "additionalProperties": False,
}


async def read_cover(images: list[bytes], client: anthropic.AsyncAnthropic | None = None) -> dict:
    """A book's details read from photos of its cover and copyright page, for books no catalog knows.

    front_cover is the index of the photo showing the front cover in images, or -1."""
    content = []
    for n, data in enumerate(images, 1):
        content += [{"type": "text", "text": f"Photo {n}:"}, _image_block(data)]
    content.append({"type": "text", "text": COVER_PROMPT})
    data = await _ask(content, COVER_SCHEMA, client, "low", 4000)
    if not isinstance(data, dict):
        raise VisionError("La respuesta de Claude no se pudo leer. Vuelve a intentar.")
    found = {k: str(data.get(k) or "").strip() for k in ("title", "author", "publisher", "year", "edition", "isbn")}
    front = data.get("front_cover")
    found["front_cover"] = front - 1 if isinstance(front, int) and 1 <= front <= len(images) else -1
    return found


# --- Búsqueda en la web --------------------------------------------------------
# For an ISBN neither Open Library nor Google Books knows, but a bookshop or a national ISBN agency lists. Only run
# when someone presses "Buscar en la web", since each search is billed: Claude Haiku keeps it to a few cents.

WEB_MODEL = "claude-haiku-5-5"
WEB_FIELDS = ("title", "author", "publisher", "year", "edition", "cover_image_url", "page_url")

WEB_PROMPT = """Find the book with ISBN {isbn} on the web.{hint} Bookshops, publishers and national ISBN agencies \
(for example the ones of Uruguay, Argentina, Chile, Mexico or Spain) usually list it. Check that the page really \
is for this ISBN.

Then answer with only a JSON object, no other text, with these keys (empty string when you didn't find it; never \
guess):
- "title": the title in its original language, with the subtitle after a colon only if it is clearly part of it
- "author": the author(s), separated by commas
- "publisher": the publisher or imprint of this edition
- "year": the year this edition was published
- "edition": edition statement such as "2a ed.", empty if none
- "cover_image_url": a direct link to a picture of this edition's front cover (a .jpg, .png or .webp image \
address you saw on a page about this ISBN), empty if you didn't see one
- "page_url": the address of the best page you found about this exact ISBN (a bookshop or publisher page \
showing this edition), empty if none

If no page is about this ISBN, answer with every value empty."""

_TOOLS_SEARCH = [{"type": "web_search_20250305", "name": "web_search", "max_uses": 3}]
_TOOLS_FETCH = [{"type": "web_fetch_20250910", "name": "web_fetch", "max_uses": 2, "max_content_tokens": 8000}]


def _json_object(text: str) -> dict | None:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


async def search_web(isbn: str, title: str = "", author: str = "",
                     client: anthropic.AsyncAnthropic | None = None) -> dict:
    """The book with this ISBN as found on the web, with every field a string (empty when not found)."""
    client = client or anthropic.AsyncAnthropic()
    hint = f' It may be "{title}"' + (f" by {author}" if author else "") + "." if title else ""
    messages = [{"role": "user", "content": WEB_PROMPT.format(isbn=isbn, hint=hint)}]
    tools = _TOOLS_SEARCH + _TOOLS_FETCH
    response = None
    try:
        for _ in range(4):  # a long search can pause; sending it back lets it carry on
            try:
                response = await client.messages.create(model=WEB_MODEL, max_tokens=8000, tools=tools,
                                                        output_config={"effort": "low"}, messages=messages)
            except anthropic.BadRequestError:
                if tools is _TOOLS_SEARCH:
                    raise
                tools = _TOOLS_SEARCH  # reading pages isn't available here: searching alone still helps
                continue
            if response.stop_reason != "pause_turn":
                break
            messages = [*messages, {"role": "assistant", "content": response.content}]
    except anthropic.AuthenticationError as e:
        raise VisionError("La clave de la API de Claude no es válida. Revisa ANTHROPIC_API_KEY.") from e
    except anthropic.RateLimitError as e:
        raise VisionError("Demasiadas búsquedas seguidas. Espera un minuto y vuelve a intentar.") from e
    except anthropic.APIStatusError as e:
        raise VisionError(f"La API de Claude respondió con un error ({e.status_code}).") from e
    except anthropic.APIConnectionError as e:
        raise VisionError("No se pudo conectar con la API de Claude. Revisa la conexión a internet.") from e

    if response is None or response.stop_reason == "refusal":
        raise VisionError("Claude no pudo hacer esta búsqueda. Vuelve a intentar.")
    # The answer is the text after the last search; earlier text is Claude narrating what it looks for.
    blocks = list(response.content)
    last_tool = max((i for i, b in enumerate(blocks) if b.type != "text"), default=-1)
    data = _json_object("".join(b.text for b in blocks[last_tool + 1:] if b.type == "text"))
    if data is None:
        data = _json_object("".join(b.text for b in blocks if b.type == "text"))
    if data is None:
        raise VisionError("La respuesta de Claude no se pudo leer. Vuelve a intentar.")
    return {k: str(data.get(k) or "").strip() for k in WEB_FIELDS}
