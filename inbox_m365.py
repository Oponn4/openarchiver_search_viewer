"""Microsoft 365 inbox over Microsoft Graph, with Apport's own app registration.

Open Archiver reads the walter-reis.de mailbox with its own Entra app and only Mail.Read. Its
tokens are not reused: refresh tokens rotate, so two programs sharing one would log each other
out. Apport has its own app ("Apport", Mail.ReadWrite) and its own token, signed in once per
mailbox by device code and kept encrypted under APPORT_STATE_DIR.

Message ids are Graph immutable ids, so a message keeps its id when it is moved.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from fastapi import HTTPException

GRAPH = "https://graph.microsoft.com/v1.0"
SCOPES = "https://graph.microsoft.com/Mail.ReadWrite https://graph.microsoft.com/User.Read offline_access"
# Sending needs Mail.Send. Asked for only at sign-in (the admin consents there) and in refreshes of
# tokens that already carry it: a refresh asking for an ungranted scope fails and would lock out
# reading as well.
SEND_SCOPES = SCOPES + " https://graph.microsoft.com/Mail.Send"
HTTP_TIMEOUT_SECONDS = 20
IMMUTABLE_IDS = {"Prefer": 'IdType="ImmutableId"'}
# Well-known Graph folder names; they work in every mailbox language.
TARGET_FOLDERS = {"archive": "archive", "delete": "deleteditems", "notspam": "inbox"}
FOLDERS = {"inbox": "inbox", "spam": "junkemail", "sent": "sentitems"}


@dataclass(frozen=True)
class GraphAccount:
    id: str
    name: str


def _setting(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise HTTPException(status_code=500, detail=f"{name} is not set")
    return value


def _authority() -> str:
    return f"https://login.microsoftonline.com/{_setting('APPORT_M365_TENANT')}/oauth2/v2.0"


def _token_path(account: GraphAccount) -> Path:
    return Path(os.environ.get("APPORT_STATE_DIR", "/data")) / f"m365-{account.id}.token"


def _cipher() -> AESGCM:
    return AESGCM(bytes.fromhex(_setting("APPORT_TOKEN_KEY")))


def _save_token(account: GraphAccount, token: dict[str, Any]) -> None:
    path = _token_path(account)
    path.parent.mkdir(parents=True, exist_ok=True)
    nonce = secrets.token_bytes(12)
    sealed = nonce + _cipher().encrypt(nonce, json.dumps(token).encode(), account.id.encode())
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(sealed)
    tmp.chmod(0o600)
    tmp.replace(path)


def _load_token(account: GraphAccount) -> dict[str, Any] | None:
    path = _token_path(account)
    if not path.exists():
        return None
    sealed = path.read_bytes()
    return json.loads(_cipher().decrypt(sealed[:12], sealed[12:], account.id.encode()))


def _token_from_response(data: dict[str, Any], upn: str, can_send: bool | None = None) -> dict[str, Any]:
    if can_send is None:
        can_send = "mail.send" in str(data.get("scope", "")).lower()
    return {
        "access_token": data["access_token"],
        "refresh_token": data["refresh_token"],
        "expires_at": time.time() + int(data.get("expires_in", 3600)),
        "upn": upn,
        "can_send": can_send,
    }


def can_send(account: GraphAccount) -> bool:
    try:
        token = _load_token(account)
    except Exception:  # noqa: BLE001 - unreadable token: cannot send
        return False
    return bool(token and token.get("can_send"))


_TOKEN_LOCK = threading.Lock()


def _access_token(account: GraphAccount) -> str:
    # One lock for all refreshes: the refresh token rotates, two parallel refreshes would race.
    with _TOKEN_LOCK:
        token = _load_token(account)
        if token is None:
            raise RuntimeError("not signed in")
        if token["expires_at"] - 120 > time.time():
            return token["access_token"]
        response = requests.post(
            f"{_authority()}/token",
            data={
                "grant_type": "refresh_token",
                "client_id": _setting("APPORT_M365_CLIENT_ID"),
                "refresh_token": token["refresh_token"],
                "scope": SEND_SCOPES if token.get("can_send") else SCOPES,
            },
            timeout=HTTP_TIMEOUT_SECONDS,
        )
        data = response.json()
        if "access_token" not in data:
            raise RuntimeError(f"token refresh failed: {data.get('error')}")
        refreshed = _token_from_response({"refresh_token": token["refresh_token"], **data}, token["upn"],
                                         bool(token.get("can_send")))
        _save_token(account, refreshed)
        return refreshed["access_token"]


def _graph(account: GraphAccount, method: str, path: str, headers_extra: dict[str, str] | None = None,
           **kwargs: Any) -> requests.Response:
    headers = {"Authorization": f"Bearer {_access_token(account)}", **IMMUTABLE_IDS, **(headers_extra or {})}
    return requests.request(method, f"{GRAPH}{path}", headers=headers, timeout=HTTP_TIMEOUT_SECONDS, **kwargs)


# Device-code sign-in. One pending login per account; the poll runs in a thread so the HTTP call
# that starts it can return the code at once.
_LOGINS: dict[str, dict[str, Any]] = {}
_LOGIN_LOCK = threading.Lock()


def start_login(account: GraphAccount) -> dict[str, Any]:
    response = requests.post(
        f"{_authority()}/devicecode",
        data={"client_id": _setting("APPORT_M365_CLIENT_ID"), "scope": SEND_SCOPES},
        timeout=HTTP_TIMEOUT_SECONDS,
    )
    code = response.json()
    if "device_code" not in code:
        raise HTTPException(status_code=502, detail=f"device code failed: {code.get('error')}")
    state = {"status": "pending", "message": code["message"], "expires_at": time.time() + int(code["expires_in"])}
    with _LOGIN_LOCK:
        _LOGINS[account.id] = state
    threading.Thread(target=_poll_login, args=(account, code, state), daemon=True).start()
    return {
        "status": "pending",
        "user_code": code["user_code"],
        "verification_uri": code["verification_uri"],
        "expires_in": int(code["expires_in"]),
        "message": code["message"],
    }


def _poll_login(account: GraphAccount, code: dict[str, Any], state: dict[str, Any]) -> None:
    interval = int(code.get("interval", 5))
    while time.time() < state["expires_at"]:
        time.sleep(interval)
        data = requests.post(
            f"{_authority()}/token",
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": _setting("APPORT_M365_CLIENT_ID"),
                "device_code": code["device_code"],
            },
            timeout=HTTP_TIMEOUT_SECONDS,
        ).json()
        error = data.get("error")
        if error == "slow_down":
            interval += 5
            continue
        if error == "authorization_pending":
            continue
        if error:
            state.update(status="failed", error=f"{error}: {data.get('error_description', '')[:200]}")
            return
        me = requests.get(
            f"{GRAPH}/me?$select=userPrincipalName",
            headers={"Authorization": f"Bearer {data['access_token']}"},
            timeout=HTTP_TIMEOUT_SECONDS,
        ).json()
        upn = str(me.get("userPrincipalName", ""))
        # Signing in with the wrong user would put another mailbox under this account's name.
        if upn.lower() != account.name.lower():
            state.update(status="failed", error=f"signed in as {upn}, expected {account.name}")
            return
        _save_token(account, _token_from_response(data, upn))
        state.update(status="done", upn=upn)
        return
    state.update(status="failed", error="device code expired")


def login_status(account: GraphAccount) -> dict[str, Any]:
    with _LOGIN_LOCK:
        state = _LOGINS.get(account.id)
    signed_in = _token_path(account).exists()
    if state is None:
        return {"status": "done" if signed_in else "none", "signed_in": signed_in}
    return {key: value for key, value in {**state, "signed_in": signed_in}.items() if key != "expires_at"}


def _encode_id(graph_id: str) -> str:
    # Graph ids may contain "/" and "+", which do not belong in a URL path segment.
    return base64.urlsafe_b64encode(graph_id.encode()).decode().rstrip("=")


def decode_id(encoded: str) -> str:
    try:
        return base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
    except (ValueError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="Malformed message id") from exc


def _iso(value: str | None) -> str | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()


def fetch_inbox(account: GraphAccount, limit: int, box: str = "inbox") -> dict[str, Any]:
    name = FOLDERS[box]
    folder = _graph(account, "GET", f"/me/mailFolders/{name}?$select=totalItemCount,unreadItemCount")
    if folder.status_code != 200:
        raise RuntimeError(f"Graph inbox failed: {folder.status_code}")
    listing = _graph(
        account,
        "GET",
        f"/me/mailFolders/{name}/messages",
        params={
            "$top": str(limit),
            "$orderby": "receivedDateTime desc",
            "$select": "id,subject,from,toRecipients,receivedDateTime,isRead,flag,internetMessageId,conversationId",
        },
    )
    if listing.status_code != 200:
        raise RuntimeError(f"Graph messages failed: {listing.status_code}")
    messages = []
    for item in listing.json().get("value", []):
        sender = (item.get("from") or {}).get("emailAddress") or {}
        messages.append(
            {
                "id": f"{account.id}:graph:{_encode_id(item['id'])}",
                "account_id": account.id,
                "account": account.name,
                "uid": None,
                "seen": bool(item.get("isRead")),
                "flagged": (item.get("flag") or {}).get("flagStatus") == "flagged",
                "size": None,
                "from_name": sender.get("name", ""),
                "from_address": sender.get("address", ""),
                "subject": (item.get("subject") or "").replace("\x00", ""),
                "date": _iso(item.get("receivedDateTime")),
                "message_id": item.get("internetMessageId", ""),
                # Graph has no References in the listing; its conversation id groups the thread.
                "references": [],
                "conversation_id": item.get("conversationId", ""),
                "to": [
                    {"name": (entry.get("emailAddress") or {}).get("name", ""), "address": (entry.get("emailAddress") or {}).get("address", "")}
                    for entry in item.get("toRecipients") or []
                    if (entry.get("emailAddress") or {}).get("address")
                ],
                "auth_failed": False,
            }
        )
    counts = folder.json()
    return {
        "account_id": account.id,
        "account": account.name,
        "total": int(counts.get("totalItemCount", 0)),
        "unread": int(counts.get("unreadItemCount", 0)),
        "messages": messages,
    }


def raw_message(account: GraphAccount, graph_id: str) -> bytes:
    """The message as MIME, the same bytes an IMAP server would hand out."""
    try:
        response = _graph(account, "GET", f"/me/messages/{requests.utils.quote(graph_id, safe='')}/$value")
    except RuntimeError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    if response.status_code in (400, 404):
        raise HTTPException(status_code=404, detail="Message no longer exists")
    if response.status_code >= 300:
        raise HTTPException(status_code=502, detail=f"Graph message failed: {response.status_code}")
    return response.content


def send_mime(account: GraphAccount, raw: bytes) -> None:
    """Sends a finished MIME message; Graph files the copy in Sent Items itself."""
    response = _graph(account, "POST", "/me/sendMail", data=base64.b64encode(raw),
                      headers_extra={"Content-Type": "text/plain"})
    if response.status_code != 202:
        raise RuntimeError(f"Graph sendMail failed: {response.status_code} {response.text[:200]}")


def act(account: GraphAccount, graph_id: str, action: str) -> dict[str, Any]:
    path = f"/me/messages/{requests.utils.quote(graph_id, safe='')}"
    try:
        if action in ("read", "unread"):
            response = _graph(account, "PATCH", path, json={"isRead": action == "read"})
        else:
            response = _graph(account, "POST", f"{path}/move", json={"destinationId": TARGET_FOLDERS[action]})
    except RuntimeError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    # Graph answers 400 for an id it cannot parse and 404 for one it does not know.
    if response.status_code in (400, 404):
        raise HTTPException(status_code=404, detail="Message no longer exists")
    if response.status_code >= 300:
        raise HTTPException(status_code=502, detail=f"Graph {action} failed: {response.status_code}")
    if action in ("read", "unread"):
        return {"ok": True}
    return {"ok": True, "moved_to": TARGET_FOLDERS[action]}
