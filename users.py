"""Who is signed in, and which mail accounts that person sees.

Without users.json Apport has one user and every account, as before. With it, each login maps to
a user and every part of the API is limited to that user's accounts: inbox, spam, archive search
and mail, newsletters. users.json lives in APPORT_STATE_DIR:

    {
      "christian": {"accounts": "*", "exclude": ["sonja@geissler-mail.net"]},
      "sonja": {"accounts": ["sonja@geissler-mail.net", "sotjan@geissler-mail.net"],
                "password": "pbkdf2_sha256$600000$<salt hex>$<hash hex>"}
    }

SITE_PASSWORD stays the owner's login, so the owner entry needs no password. Set a password with
`python users.py passwd <name>` inside the container; it is read from the terminal, never echoed.
The current user travels in a context variable set by a middleware from the session cookie.
Anyone who runs the server can still read every mailbox; this separates the app, not the host.
"""

from __future__ import annotations

import contextvars
import getpass
import hashlib
import hmac
import json
import os
import secrets
import sys
from pathlib import Path
from typing import Any

import psycopg

OWNER = "christian"
ITERATIONS = 600_000
CURRENT: contextvars.ContextVar[str | None] = contextvars.ContextVar("apport_user", default=None)


def _file() -> Path:
    return Path(os.environ.get("APPORT_STATE_DIR", "/data")) / "users.json"


def load() -> dict[str, dict[str, Any]]:
    try:
        return json.loads(_file().read_text())
    except (OSError, ValueError):
        return {}


def _hash(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)


def _verify(password: str, stored: str) -> bool:
    try:
        scheme, iterations, salt, digest = stored.split("$")
    except ValueError:
        return False
    if scheme != "pbkdf2_sha256":
        return False
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations))
    return hmac.compare_digest(candidate.hex(), digest)


def user_for_password(password: str, site_password: str) -> str | None:
    for name, entry in load().items():
        if entry.get("password") and _verify(password, entry["password"]):
            return name
    if site_password and hmac.compare_digest(password.encode(), site_password.encode()):
        return OWNER
    return None


def multi_user() -> bool:
    return bool(load())


def allowed(account_name: str) -> bool:
    users = load()
    if not users:
        return True
    user = CURRENT.get()
    entry = users.get(user or "")
    # Fail closed: with users configured, a request without a known user sees nothing.
    if entry is None:
        return False
    name = account_name.strip().lower()
    if name in {item.lower() for item in entry.get("exclude", [])}:
        return False
    accounts = entry.get("accounts", [])
    return accounts == "*" or name in {item.lower() for item in accounts}


def source_ids() -> list[str] | None:
    """Open Archiver source ids the current user may see; None means no limit (single user)."""
    if not multi_user():
        return None
    url = os.environ.get("OA_DATABASE_URL", "")
    with psycopg.connect(url, connect_timeout=5) as conn:
        rows = conn.execute("select id, name from ingestion_sources").fetchall()
    return [str(source_id) for source_id, name in rows if allowed(name)]


def state_name(base: str) -> str:
    """Per-user state file name; the owner keeps the original names from before users existed."""
    user = CURRENT.get()
    return base if user in (None, OWNER) else f"{base}-{user}"


def _set_password(name: str) -> None:
    users = load()
    if name not in users:
        sys.exit(f"{name} is not in {_file()}")
    password = getpass.getpass(f"Password for {name}: ")
    if len(password) < 12 or password != getpass.getpass("Again: "):
        sys.exit("Passwords differ or are shorter than 12 characters")
    salt = secrets.token_bytes(16)
    users[name]["password"] = f"pbkdf2_sha256${ITERATIONS}${salt.hex()}${_hash(password, salt).hex()}"
    temporary = _file().with_suffix(".tmp")
    temporary.write_text(json.dumps(users, indent=1))
    temporary.chmod(0o600)
    temporary.replace(_file())
    print(f"Password set for {name}")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "passwd":
        _set_password(sys.argv[2])
    else:
        sys.exit("usage: python users.py passwd <name>")
