import hashlib
import hmac
import secrets


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1)
    return hmac.compare_digest(digest.hex(), digest_hex)


def create_user(conn, username: str, password: str) -> None:
    conn.execute(
        "INSERT INTO users (username, password_hash) VALUES (?, ?)"
        " ON CONFLICT(username) DO UPDATE SET password_hash = excluded.password_hash",
        (username.strip().lower(), hash_password(password)),
    )


def check_login(conn, username: str, password: str) -> bool:
    row = conn.execute("SELECT password_hash FROM users WHERE username = ?", (username.strip().lower(),)).fetchone()
    return row is not None and verify_password(password, row["password_hash"])
