"""Reads book spines from a shelf photo with Claude."""
import base64
import io
import json

import anthropic
from PIL import Image, ImageOps

MODEL = "claude-opus-5-5"
MAX_SIDE = 2576

PROMPT = """This is a photo of a bookshelf. Read every book spine you can see, left to right and top to bottom.

For each book give the title and the author exactly as printed on the spine (keep the original language, fix only obvious OCR-style errors you are sure about). If the author is not visible, leave it empty. Skip objects that are not books.

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
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                },
                "required": ["title", "author", "confidence"],
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


async def read_spines(image_bytes: bytes, client: anthropic.AsyncAnthropic | None = None) -> list[dict]:
    client = client or anthropic.AsyncAnthropic()
    jpeg = prepare_image(image_bytes)
    try:
        response = await client.beta.messages.create(
            model=MODEL,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                 "data": base64.standard_b64encode(jpeg).decode()}},
                    {"type": "text", "text": PROMPT},
                ],
            }],
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
        books = json.loads(text)["books"]
    except (ValueError, KeyError) as e:
        raise VisionError("La respuesta de Claude no se pudo leer. Vuelve a intentar.") from e
    return [b for b in books if b.get("title", "").strip()]
