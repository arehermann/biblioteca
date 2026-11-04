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
