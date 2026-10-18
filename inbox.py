"""Live inbox across all IMAP accounts known to Open Archiver.

Open Archiver is an archive only: it does not know whether a message is read, archived or deleted.
The inbox therefore talks to the real mailboxes over IMAP. The account credentials are not kept
twice; they are read from Open Archiver's own database and decrypted with its ENCRYPTION_KEY, the
same way its CryptoService does (scrypt-derived key, AES-256-GCM, hex of salt|iv|tag|ciphertext).
That ties this module to Open Archiver internals, so re-check it after every Open Archiver update.
"""

from __future__ import annotations

import base64
import hashlib
import imaplib
import json
import os
import re
import ssl
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, wait
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from email import policy
from email.parser import BytesHeaderParser
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any, Iterator

import psycopg
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import APIRouter, HTTPException, Query

SALT_LENGTH = 64
IV_LENGTH = 16
TAG_LENGTH = 16
IMAP_TIMEOUT_SECONDS = 20
HEADER_FIELDS = "FROM TO SUBJECT DATE MESSAGE-ID"
MAX_LIMIT = 200
# A slow account must not hold the whole inbox back: answer after this many seconds with the
# accounts that are done, and serve the others from their last result while they finish.
INBOX_DEADLINE_SECONDS = float(os.environ.get("INBOX_DEADLINE_SECONDS", "4"))
# A result younger than this is answered straight from the cache; a refresh still starts behind it.
INBOX_FRESH_SECONDS = float(os.environ.get("INBOX_FRESH_SECONDS", "30"))
# Servers without SPECIAL-USE flags (iCloud has none for Archive, some hosters have none at all).
FALLBACK_FOLDERS = {
    "archive": ["Archive", "Archives", "Archiv"],
    "delete": ["Trash", "Papierkorb", "Gelöscht", "Deleted Messages", "Deleted Items", "Gelöschte Elemente"],
}

router = APIRouter()


@dataclass(frozen=True)
class Account:
    id: str
    name: str
    host: str
    port: int
    secure: bool
    allow_insecure_cert: bool
    username: str
    password: str


def _encryption_key() -> str:
    key = os.environ.get("OA_ENCRYPTION_KEY", "")
    if not key:
        raise HTTPException(status_code=500, detail="OA_ENCRYPTION_KEY is not set")
    return key


def decrypt_oa(encrypted_hex: str, master_key: str) -> Any:
    data = bytes.fromhex(encrypted_hex)
    salt = data[:SALT_LENGTH]
    iv = data[SALT_LENGTH : SALT_LENGTH + IV_LENGTH]
    tag = data[SALT_LENGTH + IV_LENGTH : SALT_LENGTH + IV_LENGTH + TAG_LENGTH]
    ciphertext = data[SALT_LENGTH + IV_LENGTH + TAG_LENGTH :]
    # Node's scryptSync defaults: N=16384, r=8, p=1.
    key = hashlib.scrypt(master_key.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
    # cryptography expects the tag appended to the ciphertext.
    plain = AESGCM(key).decrypt(iv, ciphertext + tag, None)
    return json.loads(plain.decode("utf-8"))


def enabled_account_names() -> set[str] | None:
    raw = os.environ.get("INBOX_ACCOUNTS", "").strip()
    if not raw:
        return None
    return {name.strip().lower() for name in raw.split(",") if name.strip()}


def load_accounts() -> list[Account]:
    url = os.environ.get("OA_DATABASE_URL", "")
    if not url:
        raise HTTPException(status_code=500, detail="OA_DATABASE_URL is not set")
    master_key = _encryption_key()
    wanted = enabled_account_names()
    with psycopg.connect(url, connect_timeout=5) as conn:
        rows = conn.execute(
            "select id, name, credentials from ingestion_sources "
            "where provider = 'generic_imap' and status <> 'paused' order by name"
        ).fetchall()
    accounts = []
    for source_id, name, credentials in rows:
        if wanted is not None and name.lower() not in wanted:
            continue
        config = decrypt_oa(credentials, master_key)
        accounts.append(
            Account(
                id=str(source_id),
                name=name,
                host=config["host"],
                port=int(config.get("port") or 993),
                secure=bool(config.get("secure", True)),
                allow_insecure_cert=bool(config.get("allowInsecureCert", False)),
                # Open Archiver passes the stored values on unchanged; a pasted app password once
                # carried a leading space there. Strip so this module does not inherit that.
                username=str(config["username"]).strip(),
                password=str(config["password"]).strip(),
            )
        )
    return accounts


def account_by_id(account_id: str) -> Account:
    for account in load_accounts():
        if account.id == account_id:
            return account
    raise HTTPException(status_code=404, detail="Unknown account")


@contextmanager
def imap_connection(account: Account) -> Iterator[imaplib.IMAP4]:
    context = ssl.create_default_context()
    if account.allow_insecure_cert:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    if account.secure:
        conn: imaplib.IMAP4 = imaplib.IMAP4_SSL(
            account.host, account.port, ssl_context=context, timeout=IMAP_TIMEOUT_SECONDS
        )
    else:
        conn = imaplib.IMAP4(account.host, account.port, timeout=IMAP_TIMEOUT_SECONDS)
        conn.starttls(ssl_context=context)
    try:
        conn.login(account.username, account.password)
        yield conn
    finally:
        try:
            conn.logout()
        except Exception:
            pass


LIST_PATTERN = re.compile(rb'\((?P<flags>[^)]*)\) (?:"[^"]*"|NIL) (?P<name>.+)$')


def special_folders(conn: imaplib.IMAP4) -> dict[str, str]:
    """Map special-use flags (\\All, \\Archive, \\Trash, ...) to folder names."""
    status, lines = conn.list()
    if status != "OK":
        return {}
    found: dict[str, str] = {}
    names: list[str] = []
    for line in lines or []:
        if not isinstance(line, bytes):
            continue
        match = LIST_PATTERN.match(line)
        if not match:
            continue
        name = match.group("name").strip()
        if name.startswith(b'"') and name.endswith(b'"'):
            name = name[1:-1].replace(b'\\"', b'"')
        decoded = _decode_modified_utf7(name.decode("ascii", "replace"))
        names.append(decoded)
        for flag in match.group("flags").split():
            found.setdefault(flag.decode().lower(), decoded)
    # All folder names, newline-joined under a key no real flag can have, for name fallbacks.
    found["\\x-names"] = "\n".join(names)
    return found


def _decode_modified_utf7(value: str) -> str:
    # RFC 3501 mailbox names: "&" starts a modified-base64 UTF-16 run, "&-" is a literal "&".
    def replace(match: re.Match[str]) -> str:
        chunk = match.group(1)
        if not chunk:
            return "&"
        padded = chunk.replace(",", "/") + "=" * (-len(chunk) % 4)
        return base64.b64decode(padded).decode("utf-16-be")

    return re.sub(r"&([^-]*)-", replace, value)


def _encode_modified_utf7(value: str) -> str:
    out, buffer = [], ""
    for char in value:
        if 0x20 <= ord(char) <= 0x7E:
            if buffer:
                encoded = base64.b64encode(buffer.encode("utf-16-be")).decode().rstrip("=")
                out.append("&" + encoded.replace("/", ",") + "-")
                buffer = ""
            out.append("&-" if char == "&" else char)
        else:
            buffer += char
    if buffer:
        encoded = base64.b64encode(buffer.encode("utf-16-be")).decode().rstrip("=")
        out.append("&" + encoded.replace("/", ",") + "-")
    return "".join(out)


def _quote_mailbox(name: str) -> str:
    encoded = _encode_modified_utf7(name)
    return '"' + encoded.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _parse_fetch(data: list[Any]) -> list[dict[str, Any]]:
    messages = []
    for item in data:
        if not isinstance(item, tuple) or len(item) < 2:
            continue
        meta, header_bytes = item[0], item[1]
        uid = re.search(rb"UID (\d+)", meta)
        if not uid:
            continue
        flags_match = re.search(rb"FLAGS \(([^)]*)\)", meta)
        flags = flags_match.group(1).decode().split() if flags_match else []
        size_match = re.search(rb"RFC822\.SIZE (\d+)", meta)
        headers = BytesHeaderParser(policy=policy.default).parsebytes(header_bytes)
        sender = getaddresses([str(headers.get("From", ""))])
        sender_name, sender_address = sender[0] if sender else ("", "")
        date_value = None
        try:
            if headers.get("Date"):
                parsed = parsedate_to_datetime(str(headers["Date"]))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                date_value = parsed.astimezone(timezone.utc).isoformat()
        except (TypeError, ValueError):
            date_value = None
        messages.append(
            {
                "uid": int(uid.group(1)),
                "seen": "\\Seen" in flags,
                "flagged": "\\Flagged" in flags,
                "size": int(size_match.group(1)) if size_match else None,
                "from_name": sender_name,
                "from_address": sender_address,
                "subject": str(headers.get("Subject", "")).replace("\x00", ""),
                "date": date_value,
                "message_id": str(headers.get("Message-ID", "")).strip(),
            }
        )
    return messages


def _uidvalidity(conn: imaplib.IMAP4) -> str:
    _, data = conn.response("UIDVALIDITY")
    return data[0].decode() if data and data[0] else "0"


def fetch_inbox(account: Account, limit: int) -> dict[str, Any]:
    # Gmail answers every UID SEARCH in about 1.5 s, so avoid SEARCH entirely: STATUS gives the
    # unread count, SELECT the message count, and the newest messages are a sequence range.
    with imap_connection(account) as conn:
        status, data = conn.status("INBOX", "(UNSEEN)")
        unread_match = re.search(rb"UNSEEN (\d+)", data[0]) if status == "OK" and data and data[0] else None
        unread = int(unread_match.group(1)) if unread_match else 0
        status, data = conn.select("INBOX", readonly=True)
        if status != "OK":
            raise RuntimeError(f"SELECT INBOX failed: {data}")
        uidvalidity = _uidvalidity(conn)
        total = int(data[0]) if data and data[0] else 0
        messages: list[dict[str, Any]] = []
        if total:
            first = max(1, total - limit + 1)
            status, data = conn.fetch(
                f"{first}:{total}",
                f"(UID FLAGS RFC822.SIZE BODY.PEEK[HEADER.FIELDS ({HEADER_FIELDS})])",
            )
            if status != "OK":
                raise RuntimeError(f"FETCH failed: {data}")
            messages = _parse_fetch(data)
    for message in messages:
        message["account_id"] = account.id
        message["account"] = account.name
        message["id"] = f"{account.id}:{uidvalidity}:{message['uid']}"
    return {"account_id": account.id, "account": account.name, "total": total, "unread": unread, "messages": messages}


_POOL = ThreadPoolExecutor(max_workers=16, thread_name_prefix="inbox")
_LOCK = threading.Lock()
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_INFLIGHT: dict[str, Future] = {}


def _start_fetch(account: Account, limit: int) -> Future:
    with _LOCK:
        future = _INFLIGHT.get(account.id)
        if future is None:
            future = _POOL.submit(fetch_inbox, account, limit)

            def done(finished: Future, account_id: str = account.id) -> None:
                with _LOCK:
                    _INFLIGHT.pop(account_id, None)
                    if finished.exception() is None:
                        _CACHE[account_id] = (time.time(), finished.result())

            future.add_done_callback(done)
            _INFLIGHT[account.id] = future
        return future


@router.get("/api/inbox")
def get_inbox(limit: int = Query(50, ge=1, le=MAX_LIMIT)) -> dict[str, Any]:
    accounts = load_accounts()
    futures = {account.id: _start_fetch(account, limit) for account in accounts}
    with _LOCK:
        all_fresh = all(
            account.id in _CACHE and time.time() - _CACHE[account.id][0] < INBOX_FRESH_SECONDS
            for account in accounts
        )
    if not all_fresh:
        wait(futures.values(), timeout=INBOX_DEADLINE_SECONDS)

    results: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    now = time.time()
    for account in accounts:
        future = futures[account.id]
        if future.done() and future.exception() is not None:
            errors.append({"account_id": account.id, "account": account.name, "error": str(future.exception())})
            continue
        if future.done():
            result, age = future.result(), 0.0
        else:
            with _LOCK:
                cached = _CACHE.get(account.id)
            if cached is None:
                errors.append({"account_id": account.id, "account": account.name, "error": "pending"})
                continue
            result, age = cached[1], now - cached[0]
        results.append({**result, "age_seconds": round(age, 1), "stale": not future.done()})

    messages = [message for result in results for message in result["messages"]]
    messages.sort(key=lambda item: item["date"] or "", reverse=True)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "accounts": sorted(
            ({key: value for key, value in result.items() if key != "messages"} for result in results),
            key=lambda item: item["account"],
        ),
        "errors": errors,
        "messages": messages[:limit],
    }


def _forget_cached(item_id: str) -> None:
    """Drop a message from the cached inbox so it does not reappear before the next refresh."""
    account_id = item_id.split(":", 1)[0]
    with _LOCK:
        cached = _CACHE.get(account_id)
        if cached:
            fetched_at, result = cached
            remaining = [message for message in result["messages"] if message["id"] != item_id]
            _CACHE[account_id] = (fetched_at, {**result, "messages": remaining, "total": result["total"] - (len(result["messages"]) - len(remaining))})


def _set_cached_seen(item_id: str, seen: bool) -> None:
    account_id = item_id.split(":", 1)[0]
    with _LOCK:
        cached = _CACHE.get(account_id)
        if cached:
            for message in cached[1]["messages"]:
                if message["id"] == item_id:
                    message["seen"] = seen


def _split_item_id(item_id: str) -> tuple[str, str, str]:
    parts = item_id.split(":")
    if len(parts) != 3 or not parts[2].isdigit():
        raise HTTPException(status_code=400, detail="Malformed message id")
    return parts[0], parts[1], parts[2]


def _target_folder(conn: imaplib.IMAP4, action: str) -> str:
    folders = special_folders(conn)
    if action == "delete":
        target = folders.get("\\trash")
    else:
        # Gmail: moving out of INBOX into All Mail is exactly "remove the Inbox label".
        target = folders.get("\\archive") or folders.get("\\all")
    if not target:
        existing = set(folders.get("\\x-names", "").split("\n"))
        target = next((name for name in FALLBACK_FOLDERS[action] if name in existing), None)
    if not target:
        raise HTTPException(status_code=409, detail=f"No folder for {action} on this account")
    return target


def _move(conn: imaplib.IMAP4, uid: str, target: str) -> None:
    # imaplib keeps the pre-login CAPABILITY list; Gmail only announces MOVE after login.
    status, data = conn.capability()
    capabilities = set(data[0].decode().upper().split()) if status == "OK" and data and data[0] else set()
    mailbox = _quote_mailbox(target)
    if "MOVE" in capabilities:
        status, data = conn.uid("MOVE", uid, mailbox)
        if status != "OK":
            raise HTTPException(status_code=502, detail=f"MOVE failed: {data}")
        return
    status, data = conn.uid("COPY", uid, mailbox)
    if status != "OK":
        raise HTTPException(status_code=502, detail=f"COPY failed: {data}")
    conn.uid("STORE", uid, "+FLAGS.SILENT", "(\\Deleted)")
    if "UIDPLUS" in capabilities:
        conn.uid("EXPUNGE", uid)
    else:
        conn.expunge()


ACTIONS = {"archive", "delete", "read", "unread"}


@router.post("/api/inbox/{item_id}/{action}")
def inbox_action(item_id: str, action: str) -> dict[str, Any]:
    if action not in ACTIONS:
        raise HTTPException(status_code=404, detail="Unknown action")
    account_id, uidvalidity, uid = _split_item_id(item_id)
    account = account_by_id(account_id)
    with imap_connection(account) as conn:
        status, data = conn.select("INBOX")
        if status != "OK":
            raise HTTPException(status_code=502, detail=f"SELECT INBOX failed: {data}")
        current = _uidvalidity(conn)
        if current != uidvalidity:
            raise HTTPException(status_code=409, detail="Mailbox changed, reload the inbox")
        status, data = conn.uid("SEARCH", None, f"UID {uid}")
        if status != "OK" or not data or not data[0]:
            raise HTTPException(status_code=404, detail="Message is no longer in the inbox")
        if action == "read":
            conn.uid("STORE", uid, "+FLAGS.SILENT", "(\\Seen)")
            _set_cached_seen(item_id, True)
            return {"ok": True}
        if action == "unread":
            conn.uid("STORE", uid, "-FLAGS.SILENT", "(\\Seen)")
            _set_cached_seen(item_id, False)
            return {"ok": True}
        target = _target_folder(conn, action)
        _move(conn, uid, target)
        _forget_cached(item_id)
        return {"ok": True, "moved_to": target}
