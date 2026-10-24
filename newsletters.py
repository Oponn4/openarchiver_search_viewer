"""Newsletter-Übersicht aus Open Archiver: wer schickt wie viel, und wie kommt man raus.

Zahlen aus der OA-Datenbank, der Abmeldeweg aus dem List-Unsubscribe-Header der jeweils
letzten Mail eines Absenders (die .eml liegt nur lesbar unter APPORT_ARCHIVE_DIR). Gruppiert
nach Domain, bei Freemailern nach Adresse. Abmeldungen merkt sich das Backend in
APPORT_STATE_DIR, damit sichtbar wird, wer danach trotzdem weiterschickt.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from email import policy
from email.parser import BytesHeaderParser
from pathlib import Path
from typing import Any

import psycopg
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import logos
import unsubscribe
import users

router = APIRouter()

CACHE_SECONDS = 24 * 3600
HEADER_BYTES = 128 * 1024
# Benachrichtigungen statt Newsletter: tragen auch List-Unsubscribe, gehören aber nicht in die Liste.
NOTIFICATION_DOMAINS = {"github.com", "gitlab.com"}

_LOCK = threading.Lock()
# One overview per user (users.py), each built from that user's mail sources only.
_CACHES: dict[str, dict[str, Any]] = {}


def _archive_root() -> Path:
    return Path(os.environ.get("APPORT_ARCHIVE_DIR", "/archive")).resolve()


def _state_file(name: str = "unsubscribed") -> Path:
    return Path(os.environ.get("APPORT_STATE_DIR", "/data")) / f"{users.state_name(name)}.json"


def _source_filter() -> tuple[str, list[Any]]:
    """SQL condition and parameters limiting archived_emails to the user's sources."""
    sources = users.source_ids()
    if sources is None:
        return "", []
    return " and ingestion_source_id::text = any(%s)", [sources]


def _load_marks(name: str = "unsubscribed") -> dict[str, dict[str, Any]]:
    try:
        return json.loads(_state_file(name).read_text())
    except (OSError, ValueError):
        return {}


def _save_marks(marks: dict[str, dict[str, Any]], name: str = "unsubscribed") -> None:
    path = _state_file(name)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(marks, ensure_ascii=False, indent=1))
    temporary.replace(path)


def group_key(address: str) -> str:
    address = address.strip().lower()
    domain = address.rpartition("@")[2]
    if not domain:
        return address
    return address if domain in logos.FREEMAIL else logos.base_domain(domain)


def _headers(storage_path: str) -> Any | None:
    root = _archive_root()
    path = (root / storage_path).resolve()
    if not path.is_relative_to(root):
        return None
    try:
        with path.open("rb") as handle:
            head = handle.read(HEADER_BYTES)
    except OSError:
        return None
    end = head.find(b"\r\n\r\n")
    end = head.find(b"\n\n") if end < 0 else end
    return BytesHeaderParser(policy=policy.default).parsebytes(head[: end + 2] if end >= 0 else head)


def _rows() -> list[tuple[Any, ...]]:
    url = os.environ.get("OA_DATABASE_URL", "")
    if not url:
        raise HTTPException(status_code=500, detail="OA_DATABASE_URL is not set")
    # Dieselbe Mail an zwei eigene Adressen zählt einmal (gleiche Message-ID). Alle Ordner
    # zählen, auch Spam und Papierkorb; Unerwünschtes blendet man in der App aus.
    query = """
        select lower(sender_email),
               max(sender_name),
               count(distinct coalesce(message_id_header, id::text)),
               count(distinct coalesce(message_id_header, id::text))
                   filter (where sent_at > now() - interval '30 days'),
               max(sent_at),
               (array_agg(storage_path order by sent_at desc))[1]
        from archived_emails
        where sent_at > now() - interval '12 months' {condition}
        group by 1
    """
    condition, params = _source_filter()
    with psycopg.connect(url, connect_timeout=5) as conn:
        return conn.execute(query.format(condition=condition), params).fetchall()


def _since(groups: dict[str, set[str]], marks: dict[str, dict[str, Any]]) -> dict[str, tuple[int, datetime | None]]:
    """Mails je abgemeldeter Gruppe seit der Abmeldung."""
    wanted = {key: mark for key, mark in marks.items() if key in groups}
    if not wanted:
        return {}
    url = os.environ.get("OA_DATABASE_URL", "")
    oldest = min(datetime.fromtimestamp(mark["at"] / 1000, timezone.utc) for mark in wanted.values())
    addresses = sorted(set().union(*(groups[key] for key in wanted)))
    condition, params = _source_filter()
    with psycopg.connect(url, connect_timeout=5) as conn:
        rows = conn.execute(
            "select lower(sender_email), sent_at from archived_emails "
            "where sent_at > %s and lower(sender_email) = any(%s)" + condition,
            [oldest, addresses, *params],
        ).fetchall()
    result: dict[str, tuple[int, datetime | None]] = {}
    for key, mark in wanted.items():
        at = datetime.fromtimestamp(mark["at"] / 1000, timezone.utc)
        times = [sent for address, sent in rows if address in groups[key] and sent > at]
        result[key] = (len(times), max(times) if times else None)
    return result


def _ms(value: datetime | None) -> int | None:
    return int(value.timestamp() * 1000) if value else None


def build() -> dict[str, Any]:
    groups: dict[str, dict[str, Any]] = {}
    addresses: dict[str, set[str]] = {}
    for address, name, count, count30, last, path in _rows():
        key = group_key(address)
        addresses.setdefault(key, set()).add(address)
        group = groups.setdefault(key, {"key": key, "count": 0, "count30": 0, "last": None, "candidates": []})
        group["count"] += count
        group["count30"] += count30
        group["last"] = max(filter(None, [group["last"], last]), default=None)
        group["candidates"].append((last, address, name, path))

    senders = []
    for key, group in groups.items():
        # Neueste Mail mit Abmeldeweg: Rechnungen derselben Domain haben oft keinen.
        for last, address, name, path in sorted(group["candidates"], key=lambda item: item[0], reverse=True)[:5]:
            headers = _headers(path)
            way = unsubscribe.parse(headers) if headers is not None else None
            if not way:
                continue
            list_id = str(headers.get("List-Id") or "")
            domain = address.rpartition("@")[2]
            notification = key in NOTIFICATION_DOMAINS or any(d in list_id for d in NOTIFICATION_DOMAINS)
            senders.append({
                "key": key,
                "name": name or address,
                "address": address,
                "domain": domain,
                "count": group["count"],
                "count30": group["count30"],
                "lastAt": _ms(group["last"]),
                "kind": "notification" if notification else "newsletter",
                "unsubscribe": way,
            })
            break
    senders.sort(key=lambda sender: sender["count"], reverse=True)
    return {"generatedAt": int(time.time() * 1000), "senders": senders, "addresses": addresses}


def _cached(refresh: bool) -> dict[str, Any]:
    user = users.CURRENT.get() or ""
    with _LOCK:
        cache = _CACHES.get(user)
        if refresh or not cache or time.time() * 1000 - cache["generatedAt"] > CACHE_SECONDS * 1000:
            cache = _CACHES[user] = build()
        return cache


@router.get("/api/newsletters")
def newsletters(refresh: bool = False) -> dict[str, Any]:
    data = _cached(refresh)
    marks = _load_marks()
    hidden_marks = _load_marks("hidden")
    since = _since(data["addresses"], marks)
    active, done, hidden = [], [], []
    for sender in data["senders"]:
        mark = marks.get(sender["key"])
        if sender["key"] in hidden_marks:
            hidden.append(sender)
        elif mark:
            count, last = since.get(sender["key"], (0, None))
            done.append({**sender, "unsubscribedAt": mark["at"], "sinceCount": count, "sinceLastAt": _ms(last)})
        else:
            active.append(sender)
    # Abgemeldete ohne Mail im Zeitraum (oder ohne Header mehr) trotzdem zeigen.
    shown = {sender["key"] for sender in done}
    for key, mark in marks.items():
        if key not in shown:
            done.append({"key": key, "name": mark.get("name") or key, "address": mark.get("address", ""),
                         "domain": mark.get("address", "").rpartition("@")[2], "unsubscribedAt": mark["at"],
                         "sinceCount": 0, "sinceLastAt": None})
    done.sort(key=lambda sender: sender["unsubscribedAt"], reverse=True)
    return {"generatedAt": data["generatedAt"], "senders": active, "unsubscribed": done, "hidden": hidden}


def remember(address: str, name: str) -> None:
    key = group_key(address)
    with _LOCK:
        marks = _load_marks()
        marks[key] = {"at": int(time.time() * 1000), "name": name, "address": address.strip().lower()}
        _save_marks(marks)


class Mark(BaseModel):
    address: str
    name: str = ""


@router.post("/api/newsletters/mark")
def mark(body: Mark) -> dict[str, Any]:
    """Abmeldung per Web-Link oder Mail erledigt (die App kann den Erfolg nicht sehen)."""
    if "@" not in body.address:
        raise HTTPException(status_code=400, detail="Not an address")
    remember(body.address, body.name)
    return {"ok": True}


@router.delete("/api/newsletters/mark/{key}")
def unmark(key: str) -> dict[str, Any]:
    with _LOCK:
        marks = _load_marks()
        marks.pop(key, None)
        _save_marks(marks)
    return {"ok": True}


@router.post("/api/newsletters/hide/{key}")
def hide(key: str) -> dict[str, Any]:
    """Aus der Liste nehmen, ohne abzumelden: bei Spam bestätigt ein Abmeldeklick nur die Adresse."""
    with _LOCK:
        marks = _load_marks("hidden")
        marks[key] = {"at": int(time.time() * 1000)}
        _save_marks(marks, "hidden")
    return {"ok": True}


@router.delete("/api/newsletters/hide/{key}")
def unhide(key: str) -> dict[str, Any]:
    with _LOCK:
        marks = _load_marks("hidden")
        marks.pop(key, None)
        _save_marks(marks, "hidden")
    return {"ok": True}


@router.post("/api/newsletters/{key}/unsubscribe")
def unsubscribe_sender(key: str) -> dict[str, Any]:
    """One-Click aus der Übersicht; die URL stammt aus der Mail, nicht aus der Anfrage."""
    sender = next((sender for sender in _cached(False)["senders"] if sender["key"] == key), None)
    if not sender or sender["unsubscribe"]["method"] != "oneClick":
        raise HTTPException(status_code=409, detail="No one-click unsubscribe for this sender")
    if not unsubscribe.one_click(sender["unsubscribe"]["url"]):
        raise HTTPException(status_code=502, detail="Unsubscribe request was not accepted")
    remember(sender["address"], sender["name"])
    return {"ok": True}
