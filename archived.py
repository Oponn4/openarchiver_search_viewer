"""Mails archived through Apport in one list, newest archive first, to get one back.

The list comes from archivelog.py, not from the archive folders (why: see there). "restore" puts
a mail back into the inbox; on Gmail that adds the Inbox label again, All Mail keeps its copy.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

import archivelog
import inbox
import trash

router = APIRouter()


@router.get("/api/archive")
def get_archived() -> dict[str, Any]:
    account_ids = {account.id for account in inbox.load_accounts()}
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "days": archivelog.KEEP_SECONDS // 86400,
        "messages": archivelog.listing(account_ids),
    }


def _owned(item_id: str) -> None:
    # The log is shared; only the user's own accounts may be read through it.
    if item_id.split(":", 1)[0] not in {account.id for account in inbox.load_accounts()}:
        raise HTTPException(status_code=404, detail="Unknown message")


def _stale(item_id: str, error: HTTPException) -> HTTPException:
    if error.status_code == 404:
        archivelog.forget(item_id)
    return error


@router.get("/api/archive/{item_id}/message")
def archived_message(item_id: str) -> dict[str, Any]:
    _owned(item_id)
    try:
        return inbox.message_response(item_id, "archive")
    except HTTPException as error:
        raise _stale(item_id, error)


@router.get("/api/archive/{item_id}/attachments/{index}")
def archived_attachment(item_id: str, index: int, download: bool = False) -> Response:
    _owned(item_id)
    return inbox.attachment_response(item_id, index, download, "archive")


@router.post("/api/archive/{item_id}/restore")
def archived_restore(item_id: str) -> dict[str, Any]:
    _owned(item_id)
    try:
        result = trash.restore(item_id, "archive")
    except HTTPException as error:
        raise _stale(item_id, error)
    archivelog.forget(item_id)
    return result
