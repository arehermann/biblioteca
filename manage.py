"""Tareas de administración.

    python manage.py crear-usuario anya
    python manage.py respaldo
"""
import getpass
import sqlite3
import sys
from datetime import datetime

from app import auth, db

KEEP_BACKUPS = 14


def crear_usuario(username: str) -> None:
    password = getpass.getpass(f"Contraseña para {username}: ")
    if len(password) < 8:
        sys.exit("La contraseña debe tener al menos 8 caracteres.")
    if password != getpass.getpass("Repite la contraseña: "):
        sys.exit("Las contraseñas no coinciden.")
    db.init_db()
    with db.connect() as conn:
        auth.create_user(conn, username, password)
    print(f"Usuario {username.lower()} listo.")


def respaldo() -> None:
    db.init_db()
    backups = db.DATA_DIR / "respaldos"
    backups.mkdir(exist_ok=True)
    target = backups / f"biblioteca-{datetime.now():%Y%m%d-%H%M}.db"
    src = sqlite3.connect(db.db_path())
    dst = sqlite3.connect(target)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    for old in sorted(backups.glob("biblioteca-*.db"))[:-KEEP_BACKUPS]:
        old.unlink()
    print(f"Respaldo guardado en {target}")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "crear-usuario":
        crear_usuario(sys.argv[2])
    elif len(sys.argv) == 2 and sys.argv[1] == "respaldo":
        respaldo()
    else:
        sys.exit(__doc__)
