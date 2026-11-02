"""Mails archived through Apport, for the "Archiviert" view and for undoing an archive.

An archive folder cannot simply be listed: on Gmail archiving removes the Inbox label, the mail
lives on in All Mail next to everything else, and the newest 50 there are mostly mails that were
never archived. So Apport records what it archived itself, under the mail's id in the archive
folder, with the list fields copied at that moment. Mails archived in Apple Mail are not in it.

One shared file, like deletions.py: accounts are shared, and the view filters by the user's
accounts. An entry can go stale (the mail was moved on elsewhere); opening or restoring it then
answers 404 and the entry is dropped.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

KEEP_SECONDS = 30 * 86400
_LOCK = threading.Lock()


def _path() -> Path:
    return Path(os.environ.get("APPORT_STATE_DIR", "/data")) / "archived.json"


def _load() -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(_path().read_text())
    except (OSError, ValueError):
        return {}
    return {key: value for key, value in data.items() if isinstance(value, dict) and "at" in value}


def _save(entries: dict[str, dict[str, Any]]) -> None:
    path = _path()
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(entries, ensure_ascii=False, indent=1))
    temporary.replace(path)


def record(archive_id: str, message: dict[str, Any]) -> None:
    now = time.time()
    with _LOCK:
        entries = {key: value for key, value in _load().items() if now - value["at"] < KEEP_SECONDS}
        entries[archive_id] = {"at": now, "message": {**message, "id": archive_id}}
        _save(entries)


def forget(archive_id: str) -> None:
    with _LOCK:
        entries = _load()
        if entries.pop(archive_id, None) is not None:
            _save(entries)


def listing(account_ids: set[str]) -> list[dict[str, Any]]:
    """Archived mails of these accounts, last archived first, each with `archived_at`."""
    now = time.time()
    with _LOCK:
        entries = _load()
    items = [
        {**entry["message"], "archived_at": datetime.fromtimestamp(entry["at"], timezone.utc).isoformat()}
        for entry in sorted(entries.values(), key=lambda value: value["at"], reverse=True)
        if now - entry["at"] < KEEP_SECONDS and entry["message"].get("account_id") in account_ids
    ]
    return items
