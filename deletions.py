"""When a mail went to the trash through Apport, keyed by its id in the trash folder.

IMAP keeps no deletion time, and the Date header is when the mail arrived. So the trash list
(trash.py) sorted a mail deleted today that arrived last week far down. Every delete through Apport
records here when it happened, under the id the mail has in the trash afterwards, so the list can
sort by deletion time. Mails deleted elsewhere (Apple Mail) have no entry; the list falls back to
their arrival date for them. Graph needs none of this, its trash listing carries
lastModifiedDateTime.

One shared file, not one per user: accounts are shared (sotjan), and a mail Sonja deleted is in
Christian's trash too.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

# Gmail empties the trash after 30 days; older entries point at nothing.
KEEP_SECONDS = 40 * 86400
_LOCK = threading.Lock()


def _path() -> Path:
    return Path(os.environ.get("APPORT_STATE_DIR", "/data")) / "deletions.json"


def _load() -> dict[str, float]:
    try:
        data = json.loads(_path().read_text())
    except (OSError, ValueError):
        return {}
    return {key: value for key, value in data.items() if isinstance(value, (int, float))}


def record(trash_id: str) -> None:
    now = time.time()
    with _LOCK:
        entries = {key: value for key, value in _load().items() if now - value < KEEP_SECONDS}
        entries[trash_id] = now
        path = _path()
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(entries, indent=1))
        temporary.replace(path)


def deleted_at(trash_ids: list[str]) -> dict[str, str]:
    """ISO times for the ids that were deleted through Apport; the others are left out."""
    with _LOCK:
        entries = _load()
    return {
        trash_id: datetime.fromtimestamp(entries[trash_id], timezone.utc).isoformat()
        for trash_id in trash_ids
        if trash_id in entries
    }
