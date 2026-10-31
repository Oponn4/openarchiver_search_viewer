"""Trash folders of all accounts in one list, to undo a delete.

A swipe deletes at once, without asking; until now Apple Mail was the only way back. The folders
are read live (IMAP \\Trash or a known folder name, Graph "deleteditems"). Each account contributes
the mails it moved to the trash last, so a fresh mistake is always in the list even when the trash
holds thousands. "restore" moves a mail back to the inbox. Gmail empties the trash after 30 days.
"""

from __future__ import annotations

import imaplib
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

import inbox
import inbox_m365
from inbox_m365 import GraphAccount

router = APIRouter()

DEADLINE_SECONDS = 20
ACTIONS = {"restore"}
_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="trash")


def _fetch(account: inbox.Account | GraphAccount, limit: int) -> dict[str, Any]:
    if isinstance(account, GraphAccount):
        return inbox_m365.fetch_inbox(account, limit, "trash")
    return inbox.fetch_inbox(account, limit, "trash")


@router.get("/api/trash")
def get_trash(limit: int = Query(50, ge=1, le=inbox.MAX_LIMIT)) -> dict[str, Any]:
    accounts = inbox.load_accounts()
    futures = {account.id: _POOL.submit(_fetch, account, limit) for account in accounts}
    wait(futures.values(), timeout=DEADLINE_SECONDS)
    results, errors = [], []
    for account in accounts:
        future = futures[account.id]
        if not future.done():
            errors.append({"account_id": account.id, "account": account.name, "error": "timeout"})
        elif future.exception() is not None:
            errors.append({"account_id": account.id, "account": account.name, "error": str(future.exception())})
        else:
            results.append(future.result())

    messages = [message for result in results for message in result["messages"]]
    messages.sort(key=lambda m: m.get("date") or "", reverse=True)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "accounts": sorted(
            ({key: value for key, value in result.items() if key != "messages"} for result in results),
            key=lambda item: item["account"],
        ),
        "errors": errors,
        "messages": messages,
    }


@router.get("/api/trash/{item_id}/message")
def trash_message(item_id: str) -> dict[str, Any]:
    return inbox.message_response(item_id, "trash")


@router.get("/api/trash/{item_id}/attachments/{index}")
def trash_attachment(item_id: str, index: int, download: bool = False) -> Response:
    return inbox.attachment_response(item_id, index, download, "trash")


@router.post("/api/trash/{item_id}/{action}")
def trash_action(item_id: str, action: str) -> dict[str, Any]:
    """"restore" moves the mail back to the inbox."""
    if action not in ACTIONS:
        raise HTTPException(status_code=404, detail="Unknown action")
    if item_id.count(":") == 2 and item_id.split(":")[1] == "graph":
        account_id, _, encoded = item_id.split(":")
        account = inbox.account_by_id(account_id)
        if not isinstance(account, GraphAccount):
            raise HTTPException(status_code=400, detail="Malformed message id")
        return inbox_m365.act(account, inbox_m365.decode_id(encoded), action)
    account_id, uidvalidity, uid = inbox._split_item_id(item_id)
    account = inbox.account_by_id(account_id)
    if isinstance(account, GraphAccount):
        raise HTTPException(status_code=400, detail="Malformed message id")
    for attempt in (1, 2):
        try:
            return inbox._imap_action(account, item_id, uidvalidity, uid, action, "trash")
        except (TimeoutError, OSError, imaplib.IMAP4.abort) as error:
            if attempt == 2:
                raise HTTPException(status_code=504, detail=f"Mailbox did not answer in time ({type(error).__name__})")
    raise AssertionError("unreachable")
