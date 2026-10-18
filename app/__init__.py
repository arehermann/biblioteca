import os
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader so the app and manage.py pick up settings without extra packages."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(Path(__file__).resolve().parent.parent / ".env")
