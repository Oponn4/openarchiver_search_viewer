"""Push notifications for new mail.

The app registers its APNs device token for the signed-in user. A watcher per mail account notices
new mail in the inbox: IMAP accounts hold one connection in IDLE, Microsoft 365 accounts are polled
(Graph change notifications need a public endpoint, Apport stays internal). Every user who sees the
account gets a notification with sender and subject on each of their devices; the same mail
delivered to two own addresses notifies once.

Accounts are only watched while at least one of their users has a registered device. The APNs key
(.p8, token based auth) lives in APPORT_STATE_DIR/apns; its key id comes from the file name
AuthKey_<KEY ID>.p8, the team id from APNS_TEAM_ID.
"""

from __future__ import annotations

import base64
import imaplib
import json
import logging
import os
import re
import socket
import threading
import time
from email import policy
from email.parser import BytesHeaderParser
from email.utils import getaddresses
from pathlib import Path
from typing import Any

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from fastapi import APIRouter
from pydantic import BaseModel, Field

import inbox
import inbox_m365
import users
from inbox_m365 import GraphAccount

logger = logging.getLogger(__name__)
router = APIRouter()

TOPIC = os.environ.get("APNS_TOPIC", "cc.bgw29.apport")
TEAM_ID = os.environ.get("APNS_TEAM_ID", "29XNS3PCPL")
APNS_HOST = os.environ.get("APNS_HOST", "https://api.push.apple.com")
# Apple wants a new provider token at most every 20 minutes and rejects ones older than an hour.
TOKEN_SECONDS = 40 * 60
IDLE_SECONDS = 25 * 60  # RFC 2177: renew IDLE before servers drop it after 30 minutes
GRAPH_POLL_SECONDS = 60
MANAGER_SECONDS = 120
RETRY_SECONDS = (30, 60, 300, 900)
DEDUP_SECONDS = 600
NEW_MAIL_FIELDS = "FROM SUBJECT MESSAGE-ID"


def _state_dir() -> Path:
    return Path(os.environ.get("APPORT_STATE_DIR", "/data"))


# --- Devices -----------------------------------------------------------------------------------

_DEVICES_LOCK = threading.Lock()


def _devices_file() -> Path:
    return _state_dir() / "push-devices.json"


def _load_devices() -> dict[str, dict[str, Any]]:
    try:
        return json.loads(_devices_file().read_text())
    except (OSError, ValueError):
        return {}


def _save_devices(devices: dict[str, dict[str, Any]]) -> None:
    path = _devices_file()
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(devices, indent=1))
    temporary.chmod(0o600)
    temporary.replace(path)


class Device(BaseModel):
    token: str = Field(pattern=r"^[0-9a-f]{64,200}$")


@router.put("/api/push/device")
def register_device(device: Device) -> dict[str, Any]:
    """The app sends its token after every start; a token moves to whoever signed in last."""
    user = users.CURRENT.get() or users.OWNER
    with _DEVICES_LOCK:
        devices = _load_devices()
        devices[device.token] = {"user": user, "updated": time.time()}
        _save_devices(devices)
    _WAKE.set()
    return {"ok": True}


@router.delete("/api/push/device")
def unregister_device(device: Device) -> dict[str, Any]:
    _forget(device.token)
    return {"ok": True}


def _forget(token: str) -> None:
    with _DEVICES_LOCK:
        devices = _load_devices()
        if devices.pop(token, None) is not None:
            _save_devices(devices)


def _tokens_for(user: str) -> list[str]:
    with _DEVICES_LOCK:
        return [token for token, entry in _load_devices().items() if entry.get("user") == user]


def _users_with_devices() -> set[str]:
    with _DEVICES_LOCK:
        return {entry.get("user", "") for entry in _load_devices().values()}


def _account_users(account_name: str) -> list[str]:
    names = list(users.load()) or [users.OWNER]
    return [name for name in names if users.allowed(account_name, name)]


# --- APNs --------------------------------------------------------------------------------------

_APNS_LOCK = threading.Lock()
_provider_token: tuple[float, str] | None = None
_client: httpx.Client | None = None


def _key_file() -> Path | None:
    return next(iter(sorted((_state_dir() / "apns").glob("AuthKey_*.p8"))), None)


def configured() -> bool:
    return _key_file() is not None


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _provider_jwt() -> str:
    global _provider_token
    if _provider_token and time.time() - _provider_token[0] < TOKEN_SECONDS:
        return _provider_token[1]
    key_file = _key_file()
    if key_file is None:
        raise RuntimeError("No APNs key in apns/")
    key_id = key_file.stem.removeprefix("AuthKey_")
    key = serialization.load_pem_private_key(key_file.read_bytes(), password=None)
    assert isinstance(key, ec.EllipticCurvePrivateKey)
    now = int(time.time())
    signing_input = (
        _b64(json.dumps({"alg": "ES256", "kid": key_id}).encode())
        + "."
        + _b64(json.dumps({"iss": TEAM_ID, "iat": now}).encode())
    )
    r, s = decode_dss_signature(key.sign(signing_input.encode(), ec.ECDSA(hashes.SHA256())))
    token = signing_input + "." + _b64(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    _provider_token = (time.time(), token)
    return token


def send(device_token: str, payload: dict[str, Any], collapse_id: str | None = None) -> int:
    """Send one notification; tokens Apple no longer knows are dropped. Returns the HTTP status."""
    global _client
    with _APNS_LOCK:
        if _client is None:
            _client = httpx.Client(http2=True, timeout=15)
        headers = {
            "authorization": f"bearer {_provider_jwt()}",
            "apns-topic": TOPIC,
            "apns-push-type": "alert",
            "apns-priority": "10",
        }
        if collapse_id:
            headers["apns-collapse-id"] = collapse_id[:64]
        client = _client
    response = client.post(f"{APNS_HOST}/3/device/{device_token}", json=payload, headers=headers)
    if response.status_code != 200:
        reason = response.json().get("reason", "") if response.content else ""
        logger.warning("APNs %s for %s…: %s", response.status_code, device_token[:8], reason)
        if response.status_code == 410 or reason in {"BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic"}:
            _forget(device_token)
    return response.status_code


# --- New mail ----------------------------------------------------------------------------------

_SEEN_LOCK = threading.Lock()
_recent: dict[tuple[str, str], float] = {}


def _first_notice(user: str, message_id: str) -> bool:
    """False for a mail this user was just told about through another of their accounts."""
    if not message_id:
        return True
    now = time.time()
    with _SEEN_LOCK:
        for key in [key for key, at in _recent.items() if now - at > DEDUP_SECONDS]:
            del _recent[key]
        if (user, message_id) in _recent:
            return False
        _recent[(user, message_id)] = now
        return True


def notify(account_name: str, item_id: str, sender: str, subject: str, message_id: str) -> None:
    for user in _account_users(account_name):
        if not _first_notice(user, message_id):
            continue
        payload = {
            "aps": {
                "alert": {"title": sender or "Neue Mail", "body": subject or "(kein Betreff)"},
                "sound": "default",
                "thread-id": account_name.lower(),
            },
            "item": item_id,
        }
        for token in _tokens_for(user):
            try:
                send(token, payload, collapse_id=item_id)
            except Exception:
                logger.exception("APNs send failed")


def _sender(name: str, address: str) -> str:
    return name.strip() or address.strip()


# --- IMAP watcher ------------------------------------------------------------------------------


def _uidnext(conn: imaplib.IMAP4) -> int:
    _, data = conn.response("UIDNEXT")
    if data and data[0]:
        return int(data[0])
    status, data = conn.uid("FETCH", "*", "(UID)")
    match = re.search(rb"UID (\d+)", data[0]) if status == "OK" and data and data[0] else None
    return int(match.group(1)) + 1 if match else 1


class _IdleExpired(Exception):
    """IDLE ran its full time without news; the watcher reconnects."""


def _idle(conn: imaplib.IMAP4, seconds: float) -> None:
    """Wait in IDLE until the server reports new mail. A read that times out can leave the
    socket's buffered reader half way through a line, so after a quiet IDLE the connection is
    dropped and opened again instead of being reused."""
    tag = conn._new_tag()  # type: ignore[attr-defined]
    conn.send(tag + b" IDLE\r\n")
    line = conn.readline()
    if not line.startswith(b"+"):
        raise RuntimeError(f"IDLE refused: {line!r}")
    deadline = time.time() + seconds
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            raise _IdleExpired
        conn.sock.settimeout(remaining)
        try:
            line = conn.readline()
        except (socket.timeout, TimeoutError):
            raise _IdleExpired from None
        if not line:
            raise ConnectionError("IMAP connection closed")
        if re.match(rb"\* \d+ (EXISTS|RECENT)", line):
            break
    conn.sock.settimeout(inbox.IMAP_TIMEOUT_SECONDS)
    conn.send(b"DONE\r\n")
    while True:
        line = conn.readline()
        if not line:
            raise ConnectionError("IMAP connection closed")
        if line.startswith(tag):
            return


def _new_imap_mail(account: inbox.Account, conn: imaplib.IMAP4, uidvalidity: str, after: int) -> int:
    status, data = conn.uid("FETCH", f"{after}:*", f"(UID FLAGS BODY.PEEK[HEADER.FIELDS ({NEW_MAIL_FIELDS})])")
    if status != "OK":
        raise RuntimeError(f"FETCH failed: {data}")
    newest = after - 1
    for item in data:
        if not isinstance(item, tuple):
            continue
        uid_match = re.search(rb"UID (\d+)", item[0])
        if not uid_match:
            continue
        # "n:*" always returns the last mail, even when its UID is below n.
        uid = int(uid_match.group(1))
        if uid < after:
            continue
        newest = max(newest, uid)
        if rb"\Seen" in item[0]:
            continue
        headers = BytesHeaderParser(policy=policy.default).parsebytes(item[1])
        found = getaddresses([str(headers.get("From", ""))])
        name, address = found[0] if found else ("", "")
        notify(
            account.name,
            f"{account.id}:{uidvalidity}:{uid}",
            _sender(name, address),
            str(headers.get("Subject", "")),
            str(headers.get("Message-ID", "")).strip(),
        )
    return newest + 1


def _watch_imap(account: inbox.Account, stop: threading.Event) -> None:
    attempt = 0
    next_uid: int | None = None
    known_validity: str | None = None
    while not stop.is_set():
        try:
            with inbox.imap_connection(account) as conn:
                status, data = conn.select("INBOX", readonly=True)
                if status != "OK":
                    raise RuntimeError(f"SELECT INBOX failed: {data}")
                uidvalidity = inbox._uidvalidity(conn)
                if next_uid is None or uidvalidity != known_validity:
                    # Start from now: mail that was already there is no news.
                    next_uid, known_validity = _uidnext(conn), uidvalidity
                attempt = 0
                while not stop.is_set():
                    next_uid = _new_imap_mail(account, conn, uidvalidity, next_uid)
                    _idle(conn, IDLE_SECONDS)
        except _IdleExpired:
            continue
        except Exception as error:
            delay = RETRY_SECONDS[min(attempt, len(RETRY_SECONDS) - 1)]
            attempt += 1
            logger.warning("Push watcher %s: %s; retry in %s s", account.name, error, delay)
            stop.wait(delay)


# --- Microsoft 365 watcher ---------------------------------------------------------------------


def _watch_graph(account: GraphAccount, stop: threading.Event) -> None:
    known: set[str] | None = None
    attempt = 0
    while not stop.is_set():
        try:
            result = inbox_m365.fetch_inbox(account, 20)
            attempt = 0
            current = {message["id"] for message in result["messages"]}
            if known is not None:
                for message in result["messages"]:
                    if message["id"] in known or message["seen"]:
                        continue
                    notify(
                        account.name,
                        message["id"],
                        _sender(message["from_name"], message["from_address"]),
                        message["subject"],
                        message["message_id"],
                    )
            known = current if known is None else (known | current)
            stop.wait(GRAPH_POLL_SECONDS)
        except Exception as error:
            delay = RETRY_SECONDS[min(attempt, len(RETRY_SECONDS) - 1)]
            attempt += 1
            logger.warning("Push watcher %s: %s; retry in %s s", account.name, error, delay)
            stop.wait(delay)


# --- Manager -----------------------------------------------------------------------------------

_WAKE = threading.Event()
_watchers: dict[str, tuple[threading.Thread, threading.Event]] = {}
_started = False


def _manage() -> None:
    while True:
        try:
            wanted: dict[str, inbox.Account | GraphAccount] = {}
            listening = _users_with_devices()
            if configured() and listening:
                for account in inbox.cached_accounts():
                    if listening & set(_account_users(account.name)):
                        wanted[account.id] = account
            for account_id in [key for key in _watchers if key not in wanted]:
                _watchers.pop(account_id)[1].set()
            for account_id, account in wanted.items():
                running = _watchers.get(account_id)
                if running and running[0].is_alive():
                    continue
                stop = threading.Event()
                target = _watch_graph if isinstance(account, GraphAccount) else _watch_imap
                thread = threading.Thread(target=target, args=(account, stop), daemon=True, name=f"push-{account.name}")
                thread.start()
                _watchers[account_id] = (thread, stop)
        except Exception:
            logger.exception("Push manager")
        _WAKE.wait(MANAGER_SECONDS)
        _WAKE.clear()


def start() -> None:
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_manage, daemon=True, name="push-manager").start()


@router.get("/api/push/status")
def status() -> dict[str, Any]:
    user = users.CURRENT.get() or users.OWNER
    return {
        "configured": configured(),
        "devices": len(_tokens_for(user)),
        "watching": sorted(
            account.name for account_id, (thread, _) in _watchers.items() if thread.is_alive()
            for account in inbox.cached_accounts() if account.id == account_id and users.allowed(account.name)
        ),
    }
