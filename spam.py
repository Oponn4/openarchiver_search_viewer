"""Spam folders of all accounts in one list, with known senders on top.

Mail providers file real newsletters and replies as spam now and then, and Gmail deletes spam after
30 days. The list reads the spam folders live (IMAP \\Junk or a known folder name, Graph
"junkemail") and asks Open Archiver who the senders are: someone you wrote to, or who also sends
mail that did not land in spam, is "known". Only the exact address counts, never the domain, and a
mail whose From failed DMARC is never known: a forged sender must not end up on top.
"""

from __future__ import annotations

import imaplib
import os
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timezone
from typing import Any

import psycopg
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

import inbox
import inbox_m365
from inbox_m365 import GraphAccount

router = APIRouter()

DEADLINE_SECONDS = 20
ACTIONS = {"notspam", "delete"}
_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="spam")


def _fetch(account: inbox.Account | GraphAccount, limit: int) -> dict[str, Any]:
    if isinstance(account, GraphAccount):
        return inbox_m365.fetch_inbox(account, limit, "spam")
    return inbox.fetch_inbox(account, limit, "spam")


def _known(addresses: list[str], spam_ids: list[str]) -> dict[str, dict[str, int]]:
    """Per address: mails outside spam folders, and mails we sent to it, from the archive.

    The listed spam itself does not count, even where the archive caught it in another folder.
    """
    url = os.environ.get("OA_DATABASE_URL", "")
    if not url or not addresses:
        return {}
    known: dict[str, dict[str, int]] = {}
    with psycopg.connect(url, connect_timeout=5) as conn:
        for address, count in conn.execute(
            "select lower(sender_email), count(distinct coalesce(message_id_header, id::text)) "
            "from archived_emails where lower(sender_email) = any(%s) "
            "and coalesce(path, '') !~* '(spam|junk)' "
            "and coalesce(message_id_header, '') <> all(%s) group by 1",
            (addresses, spam_ids),
        ):
            known.setdefault(address, {"received": 0, "wrote": 0})["received"] = count
        for address, count in conn.execute(
            "select lower(r->>'address'), count(distinct coalesce(message_id_header, id::text)) "
            "from archived_emails, jsonb_array_elements("
            "  coalesce(recipients->'to', '[]'::jsonb) || coalesce(recipients->'cc', '[]'::jsonb)) r "
            "where path ~* '(sent|gesendet)' and lower(r->>'address') = any(%s) group by 1",
            (addresses,),
        ):
            known.setdefault(address, {"received": 0, "wrote": 0})["wrote"] = count
    return known


@router.get("/api/spam")
def get_spam(limit: int = Query(100, ge=1, le=inbox.MAX_LIMIT)) -> dict[str, Any]:
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
    try:
        known = _known(
            sorted({m["from_address"].lower() for m in messages if m.get("from_address")}),
            [m["message_id"] for m in messages if m.get("message_id")],
        )
    except psycopg.Error:
        known = {}
    for message in messages:
        facts = known.get((message.get("from_address") or "").lower(), {"received": 0, "wrote": 0})
        message["known_received"] = facts["received"]
        message["known_wrote"] = facts["wrote"]
        message["known"] = (facts["received"] > 0 or facts["wrote"] > 0) and not message.get("auth_failed")
    messages.sort(key=lambda m: (m["known"], m.get("date") or ""), reverse=True)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "accounts": sorted(
            ({key: value for key, value in result.items() if key != "messages"} for result in results),
            key=lambda item: item["account"],
        ),
        "errors": errors,
        "messages": messages,
    }


@router.get("/api/spam/{item_id}/message")
def spam_message(item_id: str) -> dict[str, Any]:
    return inbox.message_response(item_id, "spam")


@router.get("/api/spam/{item_id}/attachments/{index}")
def spam_attachment(item_id: str, index: int, download: bool = False) -> Response:
    return inbox.attachment_response(item_id, index, download, "spam")


@router.post("/api/spam/{item_id}/{action}")
def spam_action(item_id: str, action: str) -> dict[str, Any]:
    """"notspam" moves the mail to the inbox, "delete" to the trash."""
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
            return inbox._imap_action(account, item_id, uidvalidity, uid, action, "spam")
        except (TimeoutError, OSError, imaplib.IMAP4.abort) as error:
            if attempt == 2:
                raise HTTPException(status_code=504, detail=f"Mailbox did not answer in time ({type(error).__name__})")
    raise AssertionError("unreachable")
