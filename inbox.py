"""Live inbox across all IMAP accounts known to Open Archiver.

Open Archiver is an archive only: it does not know whether a message is read, archived or deleted.
The inbox therefore talks to the real mailboxes over IMAP. The account credentials are not kept
twice; they are read from Open Archiver's own database and decrypted with its ENCRYPTION_KEY, the
same way its CryptoService does (scrypt-derived key, AES-256-GCM, hex of salt|iv|tag|ciphertext).
That ties this module to Open Archiver internals, so re-check it after every Open Archiver update.

Microsoft 365 mailboxes (Open Archiver provider "oauth_mailbox") go over Graph instead, with
Apport's own app registration and token; see inbox_m365.
"""

from __future__ import annotations

import base64
import hashlib
import imaplib
import importlib
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
from email.parser import BytesHeaderParser, BytesParser
from email.utils import getaddresses, parsedate_to_datetime
from typing import Any, Iterator

import psycopg
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

import inbox_m365
from inbox_m365 import GraphAccount

SALT_LENGTH = 64
IV_LENGTH = 16
TAG_LENGTH = 16
IMAP_TIMEOUT_SECONDS = 20
HEADER_FIELDS = "FROM TO SUBJECT DATE MESSAGE-ID IN-REPLY-TO REFERENCES"
MAX_LIMIT = 200
# Raw messages never change under their id (the id carries UIDVALIDITY), so they can be kept a
# while. After every inbox fetch the newest few per account are fetched ahead, so opening a mail
# is answered from memory instead of a fresh IMAP login (measured 0.5-1.5 s before).
RAW_CACHE_SECONDS = 1800
RAW_CACHE_ENTRIES = 300
PREFETCH_PER_ACCOUNT = int(os.environ.get("INBOX_PREFETCH_PER_ACCOUNT", "20"))
PREFETCH_MAX_BYTES = 5_000_000
# Credentials change rarely; decrypting all of them for every opened mail cost 0.23 s.
ACCOUNTS_CACHE_SECONDS = 60
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


def load_accounts() -> list[Account | GraphAccount]:
    url = os.environ.get("OA_DATABASE_URL", "")
    if not url:
        raise HTTPException(status_code=500, detail="OA_DATABASE_URL is not set")
    master_key = _encryption_key()
    wanted = enabled_account_names()
    with psycopg.connect(url, connect_timeout=5) as conn:
        rows = conn.execute(
            "select id, name, provider, credentials from ingestion_sources "
            "where provider in ('generic_imap', 'oauth_mailbox') and status <> 'paused' order by name"
        ).fetchall()
    accounts: list[Account | GraphAccount] = []
    for source_id, name, provider, credentials in rows:
        if wanted is not None and name.lower() not in wanted:
            continue
        if provider == "oauth_mailbox":
            # Only the account's identity comes from Open Archiver, never its token.
            if os.environ.get("APPORT_M365_CLIENT_ID"):
                accounts.append(GraphAccount(id=str(source_id), name=name))
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


_ACCOUNTS_LOCK = threading.Lock()
_ACCOUNTS: tuple[float, list[Account | GraphAccount]] | None = None


def cached_accounts() -> list[Account | GraphAccount]:
    global _ACCOUNTS
    with _ACCOUNTS_LOCK:
        if _ACCOUNTS and time.time() - _ACCOUNTS[0] < ACCOUNTS_CACHE_SECONDS:
            return _ACCOUNTS[1]
    accounts = load_accounts()
    with _ACCOUNTS_LOCK:
        _ACCOUNTS = (time.time(), accounts)
    return accounts


def account_by_id(account_id: str) -> Account | GraphAccount:
    for account in cached_accounts():
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
                "references": _references(headers),
            }
        )
    return messages


MESSAGE_ID_PATTERN = re.compile(r"<[^<>\s]+>")


def _references(headers: Any) -> list[str]:
    """Message-IDs this mail answers (References, then In-Reply-To), for grouping into threads."""
    found: list[str] = []
    for name in ("References", "In-Reply-To"):
        for value in MESSAGE_ID_PATTERN.findall(str(headers.get(name, ""))):
            if value not in found:
                found.append(value)
    return found


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
# Actions done through Apport, per account: (time, item id, "seen" | "unseen" | "gone"). A fetch
# that started before an action returns the mailbox as it was; the action is applied to its
# result again, otherwise a moved mail reappears and the unread count stays too high (Build 14:
# "2 ungelesen" after everything was read and archived, until a later reload).
_CHANGES: dict[str, list[tuple[float, str, str]]] = {}
CHANGES_KEEP_SECONDS = 600


def _start_fetch(account: Account, limit: int) -> Future:
    with _LOCK:
        future = _INFLIGHT.get(account.id)
        if future is None:
            fetch = inbox_m365.fetch_inbox if isinstance(account, GraphAccount) else fetch_inbox
            started = time.time()
            future = _POOL.submit(fetch, account, limit)

            def done(finished: Future, account_id: str = account.id, source: Account | GraphAccount = account) -> None:
                with _LOCK:
                    _INFLIGHT.pop(account_id, None)
                    if finished.exception() is None:
                        result = finished.result()
                        for changed_at, item_id, change in _CHANGES.get(account_id, []):
                            if changed_at >= started:
                                _apply_change(result, item_id, change)
                        _CACHE[account_id] = (time.time(), result)
                if finished.exception() is None:
                    newest = sorted(finished.result()["messages"], key=lambda m: m.get("date") or "", reverse=True)
                    _PREFETCH_POOL.submit(_prefetch, source, [m["id"] for m in newest[:PREFETCH_PER_ACCOUNT]])

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


def _apply_change(result: dict[str, Any], item_id: str, change: str) -> None:
    """Apply one action to a fetched inbox, keeping `total` and `unread` in step with the list."""
    message = next((m for m in result["messages"] if m["id"] == item_id), None)
    if message is None:
        return
    if change == "gone":
        result["messages"] = [m for m in result["messages"] if m["id"] != item_id]
        result["total"] = max(0, result["total"] - 1)
        if not message["seen"]:
            result["unread"] = max(0, result["unread"] - 1)
        return
    seen = change == "seen"
    if message["seen"] != seen:
        message["seen"] = seen
        result["unread"] = max(0, result["unread"] + (-1 if seen else 1))


def _record_change(item_id: str, change: str) -> None:
    """Update the cached inbox now and remember the action for fetches already running."""
    account_id = item_id.split(":", 1)[0]
    now = time.time()
    with _LOCK:
        changes = [entry for entry in _CHANGES.get(account_id, []) if now - entry[0] < CHANGES_KEEP_SECONDS]
        changes.append((now, item_id, change))
        _CHANGES[account_id] = changes
        cached = _CACHE.get(account_id)
        if cached:
            _apply_change(cached[1], item_id, change)


def _forget_cached(item_id: str) -> None:
    """Drop a message from the cached inbox so it does not reappear before the next refresh."""
    _record_change(item_id, "gone")


def _set_cached_seen(item_id: str, seen: bool) -> None:
    _record_change(item_id, "seen" if seen else "unseen")


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
    if item_id.count(":") == 2 and item_id.split(":")[1] == "graph":
        account_id, _, encoded = item_id.split(":")
        account = account_by_id(account_id)
        if not isinstance(account, GraphAccount):
            raise HTTPException(status_code=400, detail="Malformed message id")
        result = inbox_m365.act(account, inbox_m365.decode_id(encoded), action)
        if action in ("read", "unread"):
            _set_cached_seen(item_id, action == "read")
        else:
            _forget_cached(item_id)
        return result
    account_id, uidvalidity, uid = _split_item_id(item_id)
    account = account_by_id(account_id)
    if isinstance(account, GraphAccount):
        raise HTTPException(status_code=400, detail="Malformed message id")
    # Gmail sometimes stalls a single SELECT past the timeout (tp.oponn, 2026-10-20, while Open
    # Archiver imported the same mailbox). One retry on a fresh connection; the actions are safe
    # to repeat, a message already moved answers 404.
    for attempt in (1, 2):
        try:
            return _imap_action(account, item_id, uidvalidity, uid, action)
        except (TimeoutError, OSError, imaplib.IMAP4.abort) as error:
            if attempt == 2:
                raise HTTPException(status_code=504, detail=f"Mailbox did not answer in time ({type(error).__name__})")
    raise AssertionError("unreachable")


def _imap_action(account: Account, item_id: str, uidvalidity: str, uid: str, action: str) -> dict[str, Any]:
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


_RAW_LOCK = threading.Lock()
_RAW_CACHE: dict[str, tuple[float, bytes]] = {}
# Separate and small, so prefetching never delays an inbox fetch.
_PREFETCH_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="prefetch")


def _store_raw(item_id: str, payload: bytes) -> None:
    with _RAW_LOCK:
        _RAW_CACHE[item_id] = (time.time(), payload)
        for stale in sorted(_RAW_CACHE, key=lambda key: _RAW_CACHE[key][0])[:-RAW_CACHE_ENTRIES]:
            del _RAW_CACHE[stale]


def _is_cached(item_id: str) -> bool:
    with _RAW_LOCK:
        cached = _RAW_CACHE.get(item_id)
    return bool(cached) and time.time() - cached[0] < RAW_CACHE_SECONDS


def _prefetch(account: Account | GraphAccount, item_ids: list[str]) -> None:
    missing = [item_id for item_id in item_ids if not _is_cached(item_id)]
    if not missing:
        return
    try:
        if isinstance(account, GraphAccount):
            for item_id in missing:
                payload = inbox_m365.raw_message(account, inbox_m365.decode_id(item_id.split(":")[2]))
                if len(payload) <= PREFETCH_MAX_BYTES:
                    _store_raw(item_id, payload)
            return
        uidvalidity = missing[0].split(":")[1]
        uids = {item_id.split(":")[2]: item_id for item_id in missing if item_id.split(":")[1] == uidvalidity}
        with imap_connection(account) as conn:
            status, _ = conn.select("INBOX", readonly=True)
            if status != "OK" or _uidvalidity(conn) != uidvalidity:
                return
            # Sizes first, so one huge attachment mail is not pulled ahead.
            status, data = conn.uid("FETCH", ",".join(uids), "(RFC822.SIZE)")
            small = [
                match.group(1).decode()
                for item in data or []
                if isinstance(item, bytes)
                and (match := re.search(rb"UID (\d+)", item))
                and (size := re.search(rb"RFC822\.SIZE (\d+)", item))
                and int(size.group(1)) <= PREFETCH_MAX_BYTES
            ]
            if not small:
                return
            status, data = conn.uid("FETCH", ",".join(small), "(BODY.PEEK[])")
        for item in data or []:
            if isinstance(item, tuple) and len(item) > 1 and (match := re.search(rb"UID (\d+)", item[0])):
                item_id = uids.get(match.group(1).decode())
                if item_id:
                    _store_raw(item_id, item[1])
    except Exception:
        # Best effort: a failed prefetch only means the mail is fetched when opened.
        pass


def _fetch_raw(item_id: str) -> tuple[Account | GraphAccount, bytes]:
    if item_id.count(":") == 2 and item_id.split(":")[1] == "graph":
        account_id, _, encoded = item_id.split(":")
        account = account_by_id(account_id)
        if not isinstance(account, GraphAccount):
            raise HTTPException(status_code=400, detail="Malformed message id")
        return account, inbox_m365.raw_message(account, inbox_m365.decode_id(encoded))
    account_id, uidvalidity, uid = _split_item_id(item_id)
    account = account_by_id(account_id)
    if isinstance(account, GraphAccount):
        raise HTTPException(status_code=400, detail="Malformed message id")
    with imap_connection(account) as conn:
        # Read-only and BODY.PEEK: opening a mail must not mark it read; the app does that on purpose.
        status, data = conn.select("INBOX", readonly=True)
        if status != "OK":
            raise HTTPException(status_code=502, detail=f"SELECT INBOX failed: {data}")
        if _uidvalidity(conn) != uidvalidity:
            raise HTTPException(status_code=409, detail="Mailbox changed, reload the inbox")
        status, data = conn.uid("FETCH", uid, "(BODY.PEEK[])")
    if status != "OK":
        raise HTTPException(status_code=502, detail=f"FETCH failed: {data}")
    payload = next((item[1] for item in data or [] if isinstance(item, tuple) and len(item) > 1), None)
    if not payload:
        raise HTTPException(status_code=404, detail="Message is no longer in the inbox")
    return account, payload


def _raw(item_id: str) -> tuple[Account | GraphAccount, bytes]:
    now = time.time()
    with _RAW_LOCK:
        cached = _RAW_CACHE.get(item_id)
    if cached and now - cached[0] < RAW_CACHE_SECONDS:
        return account_by_id(item_id.split(":", 1)[0]), cached[1]
    account, payload = _fetch_raw(item_id)
    _store_raw(item_id, payload)
    return account, payload


def _attachment_parts(message: Any) -> list[Any]:
    parts = []
    for part in message.walk():
        if part.is_multipart():
            continue
        filename = part.get_filename()
        disposition = part.get_content_disposition()
        # Inline images with a Content-ID are already embedded in the HTML body.
        if disposition != "attachment" and (not filename or part.get("Content-ID")):
            continue
        parts.append(part)
    return parts


def _viewer() -> Any:
    # app.py imports this module; import it back lazily to reuse its MIME helpers.
    return importlib.import_module("app")


@router.get("/api/inbox/{item_id}/message")
def inbox_message(item_id: str) -> dict[str, Any]:
    account, payload = _raw(item_id)
    viewer = _viewer()
    message = BytesParser(policy=policy.default).parsebytes(payload)
    content = viewer.extract_message_content(message)
    attachments = []
    for index, part in enumerate(_attachment_parts(message)):
        data = part.get_payload(decode=True) or b""
        filename = part.get_filename() or f"Anhang {index + 1}"
        attachments.append(
            {
                "id": str(index),
                "filename": filename,
                "mimeType": viewer.normalized_mime_type(filename, part.get_content_type(), data),
                "sizeBytes": len(data),
                "viewUrl": f"/api/inbox/{item_id}/attachments/{index}",
            }
        )
    timestamp = None
    try:
        if message.get("Date"):
            parsed = parsedate_to_datetime(str(message["Date"]))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            timestamp = int(parsed.timestamp() * 1000)
    except (TypeError, ValueError):
        timestamp = None

    def addresses(header: str) -> list[str]:
        return [
            f"{name} <{address}>" if name else address
            for name, address in getaddresses([str(value) for value in message.get_all(header, [])])
            if address or name
        ]

    sender = addresses("From")
    return {
        "item": {
            "id": item_id,
            "userEmail": account.name,
            "from": sender[0] if sender else "",
            "to": addresses("To"),
            "cc": addresses("Cc"),
            "subject": str(message.get("Subject", "")).replace("\x00", ""),
            "body": content["text"],
            "bodyHtml": content["html"],
            "attachments": attachments,
            "attachmentCount": len(attachments),
            "timestamp": timestamp,
            "messageIdHeader": str(message.get("Message-ID", "")).strip(),
        }
    }


@router.get("/api/inbox/{item_id}/attachments/{index}")
def inbox_attachment(item_id: str, index: int, download: bool = False) -> Response:
    _, payload = _raw(item_id)
    viewer = _viewer()
    parts = _attachment_parts(BytesParser(policy=policy.default).parsebytes(payload))
    if not 0 <= index < len(parts):
        raise HTTPException(status_code=404, detail="Attachment not found")
    part = parts[index]
    data = part.get_payload(decode=True) or b""
    filename = (part.get_filename() or f"Anhang {index + 1}").replace('"', "'")
    mime_type = viewer.normalized_mime_type(filename, part.get_content_type(), data)
    active = viewer.is_active_attachment(filename, mime_type)
    headers = {
        "Content-Disposition": viewer.content_disposition(filename, "attachment" if active or download else "inline"),
        "X-Content-Type-Options": "nosniff",
    }
    return Response(content=data, media_type="application/octet-stream" if active else mime_type, headers=headers)


def _graph_account(account_id: str) -> GraphAccount:
    account = account_by_id(account_id)
    if not isinstance(account, GraphAccount):
        raise HTTPException(status_code=404, detail="Not a Microsoft 365 account")
    return account


@router.post("/api/inbox/accounts/{account_id}/login")
def m365_login(account_id: str) -> dict[str, Any]:
    """Start the device-code sign-in; the caller shows user_code and verification_uri."""
    return inbox_m365.start_login(_graph_account(account_id))


@router.get("/api/inbox/accounts/{account_id}/login")
def m365_login_status(account_id: str) -> dict[str, Any]:
    return inbox_m365.login_status(_graph_account(account_id))
