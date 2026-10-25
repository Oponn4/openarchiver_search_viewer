"""Write, reply and forward from Apport: the mail is built and sent here, not on the phone.

The app sends the new text as HTML, the recipients and, for a reply or forward, the inbox item it
refers to. This module adds the quote (or the forwarded header block and attachments), sets
In-Reply-To and References so the reply threads at the recipient, and sends it over SMTP with the
same credentials Open Archiver uses for IMAP. Providers that do not keep a copy of SMTP mail (all
but Gmail) get one appended to their Sent folder.

Sending waits SEND_DELAY_SECONDS in an outbox under APPORT_STATE_DIR first, so "Undo" can stop it.
The outbox survives a restart: a queued mail is sent when the server is back.

Microsoft 365 cannot send yet: Apport's Entra app has no Mail.Send permission, and asking for it in
the token refresh before it is granted would break reading that mailbox as well.
"""

from __future__ import annotations

import base64
import html
import imaplib
import json
import mimetypes
import os
import re
import secrets
import smtplib
import ssl
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import format_datetime, formataddr, getaddresses, make_msgid, parsedate_to_datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

import inbox
import users
from inbox_m365 import GraphAccount

router = APIRouter()

SEND_DELAY_SECONDS = int(os.environ.get("APPORT_SEND_DELAY_SECONDS", "10"))
SMTP_TIMEOUT_SECONDS = 30
MAX_UPLOAD_BYTES = 25_000_000
MAX_MESSAGE_BYTES = 30_000_000
UPLOAD_KEEP_SECONDS = 24 * 3600
OUTBOX_KEEP_SECONDS = 7 * 24 * 3600
MODES = {"new", "reply", "replyAll", "forward"}


@dataclass(frozen=True)
class SmtpServer:
    host: str
    port: int
    starttls: bool
    # The provider files SMTP mail into Sent by itself; appending would store it twice.
    keeps_sent: bool


SMTP_BY_IMAP_HOST = {
    "imap.gmail.com": SmtpServer("smtp.gmail.com", 465, False, True),
    "imap.mail.me.com": SmtpServer("smtp.mail.me.com", 587, True, False),
    "imap.gmx.net": SmtpServer("mail.gmx.net", 465, False, False),
}


def smtp_server(account: inbox.Account) -> SmtpServer:
    # Hosts like netcup's take SMTP on the IMAP host name.
    return SMTP_BY_IMAP_HOST.get(account.host.lower(), SmtpServer(account.host, 465, False, False))


def _state_dir() -> Path:
    return Path(os.environ.get("APPORT_STATE_DIR", "/data"))


def _outbox_dir() -> Path:
    folder = _state_dir() / "outbox"
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    return folder


def _uploads_dir() -> Path:
    folder = _outbox_dir() / "uploads"
    folder.mkdir(mode=0o700, exist_ok=True)
    return folder


def _write_json(path: Path, data: Any) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=1))
    temporary.chmod(0o600)
    temporary.replace(path)


# --- Settings: sender name and signature per account ---------------------------------------


def _settings_file() -> Path:
    return _state_dir() / f"{users.state_name('compose')}.json"


def _settings() -> dict[str, dict[str, Any]]:
    try:
        return json.loads(_settings_file().read_text())
    except (OSError, ValueError):
        return {}


def _account_settings(account_name: str) -> dict[str, Any]:
    entry = _settings().get(account_name.lower(), {})
    return {
        "name": entry.get("name", ""),
        "signature": entry.get("signature", ""),
        "signReplies": bool(entry.get("signReplies", False)),
    }


class AccountSettings(BaseModel):
    name: str = Field("", max_length=200)
    signature: str = Field("", max_length=100_000)
    signReplies: bool = False


def _can_send(account: inbox.Account | GraphAccount) -> bool:
    return isinstance(account, inbox.Account)


@router.get("/api/compose/accounts")
def compose_accounts() -> dict[str, Any]:
    return {
        "accounts": [
            {"id": account.id, "address": account.name, "canSend": _can_send(account), **_account_settings(account.name)}
            for account in inbox.load_accounts()
        ],
        "sendDelaySeconds": SEND_DELAY_SECONDS,
    }


@router.put("/api/compose/accounts/{account_id}")
def save_account_settings(account_id: str, body: AccountSettings) -> dict[str, Any]:
    account = inbox.account_by_id(account_id)
    settings = _settings()
    settings[account.name.lower()] = body.model_dump()
    _write_json(_settings_file(), settings)
    return {"ok": True}


# --- Reply and forward: what the compose screen starts with -------------------------------


def _original(item_id: str, box: str) -> tuple[inbox.Account | GraphAccount, Any]:
    if box not in ("inbox", "spam"):
        raise HTTPException(status_code=400, detail="Unknown box")
    account, payload = inbox._raw(item_id, box)
    return account, BytesParser(policy=policy.default).parsebytes(payload)


def _own_addresses() -> set[str]:
    return {account.name.lower() for account in inbox.load_accounts()}


def _addresses(message: Any, header: str) -> list[tuple[str, str]]:
    return [(name, address) for name, address in getaddresses([str(v) for v in message.get_all(header, [])]) if address]


def _format(pairs: list[tuple[str, str]]) -> list[str]:
    seen: set[str] = set()
    result = []
    for name, address in pairs:
        if address.lower() in seen:
            continue
        seen.add(address.lower())
        result.append(formataddr((name, address)) if name else address)
    return result


def _prefixed(subject: str, prefix: str) -> str:
    subject = subject.replace("\x00", "").strip()
    known = ("re:", "aw:") if prefix == "Re:" else ("fwd:", "fw:", "wg:")
    return subject if subject.lower().startswith(known) else f"{prefix} {subject}".strip()


def _sent_at(message: Any) -> datetime | None:
    try:
        parsed = parsedate_to_datetime(str(message.get("Date", "")))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _german_date(value: datetime | None) -> str:
    if value is None:
        return ""
    local = value.astimezone(ZoneInfo("Europe/Berlin"))
    return local.strftime("%d.%m.%Y um %H:%M")


def _body_inner(document: str) -> str:
    """The inside of <body>, with the <style> blocks of <head>, so it can sit in a blockquote."""
    styles = "".join(re.findall(r"(?is)<style\b.*?</style>", document.split("<body", 1)[0])) if "<body" in document.lower() else ""
    match = re.search(r"(?is)<body\b[^>]*>(.*)</body>", document)
    inner = match.group(1) if match else re.sub(r"(?is)</?(html|head|body)\b[^>]*>", "", document)
    return styles + inner


def _original_html(message: Any) -> tuple[str, str]:
    """(HTML, text) of the original, HTML built from the text for plain mails."""
    content = inbox._viewer().extract_message_content(message)
    text = content["text"]
    if content["html"]:
        return _body_inner(content["html"]), text
    return f'<div style="white-space:pre-wrap">{html.escape(text)}</div>', text


def _sender_line(message: Any) -> str:
    sender = _addresses(message, "From")
    if not sender:
        return ""
    name, address = sender[0]
    return f"{name} <{address}>" if name else address


def _quote(message: Any, mode: str) -> tuple[str, str]:
    """(HTML, text) put below the new text: quoted reply or forwarded header block."""
    original_html, original_text = _original_html(message)
    if mode == "forward":
        lines = [
            "---------- Weitergeleitete Nachricht ----------",
            f"Von: {_sender_line(message)}",
            f"Datum: {_german_date(_sent_at(message))}",
            f"Betreff: {str(message.get('Subject', '')).replace(chr(0), '')}",
            f"An: {', '.join(_format(_addresses(message, 'To')))}",
        ]
        cc = _format(_addresses(message, "Cc"))
        if cc:
            lines.append(f"Kopie: {', '.join(cc)}")
        header_html = "".join(f"<div>{html.escape(line)}</div>" for line in lines)
        return (
            f'<div class="apport-forward"><br>{header_html}<br>{original_html}</div>',
            "\n".join(lines) + "\n\n" + original_text,
        )
    attribution = f"Am {_german_date(_sent_at(message))} schrieb {_sender_line(message)}:"
    quoted_text = "\n".join(f"> {line}" if line else ">" for line in original_text.splitlines())
    return (
        f'<div class="apport-quote"><br><div>{html.escape(attribution)}</div>'
        f'<blockquote type="cite" style="margin:0 0 0 .8ex;border-left:1px solid #ccc;padding-left:1ex">'
        f"{original_html}</blockquote></div>",
        f"{attribution}\n{quoted_text}",
    )


@router.get("/api/compose/start")
def compose_start(item: str, mode: str, box: str = "inbox") -> dict[str, Any]:
    """Recipients, subject and quote for a reply or forward of an inbox item."""
    if mode not in MODES - {"new"}:
        raise HTTPException(status_code=400, detail="Unknown mode")
    account, message = _original(item, box)
    own = _own_addresses()
    to: list[tuple[str, str]] = []
    cc: list[tuple[str, str]] = []
    if mode in ("reply", "replyAll"):
        to = _addresses(message, "Reply-To") or _addresses(message, "From")
        # A reply to one's own sent mail goes to its recipients, as in Apple Mail.
        if all(address.lower() in own for _, address in to):
            to = _addresses(message, "To")
        if mode == "replyAll":
            taken = {address.lower() for _, address in to}
            cc = [
                pair
                for pair in _addresses(message, "To") + _addresses(message, "Cc")
                if pair[1].lower() not in own and pair[1].lower() not in taken
            ]
    quote_html, _ = _quote(message, mode)
    attachments = []
    if mode == "forward":
        for index, part in enumerate(inbox._attachment_parts(message)):
            data = part.get_payload(decode=True) or b""
            attachments.append({"index": index, "filename": part.get_filename() or f"Anhang {index + 1}", "sizeBytes": len(data)})
    subject = str(message.get("Subject", ""))
    return {
        "accountId": account.id,
        "to": _format(to),
        "cc": _format(cc),
        "subject": _prefixed(subject, "Fwd:" if mode == "forward" else "Re:"),
        "quoteHtml": quote_html,
        "attachments": attachments,
    }


# --- Attachments the user adds -------------------------------------------------------------


def _clean_uploads() -> None:
    now = time.time()
    for path in _uploads_dir().glob("*"):
        try:
            if now - path.stat().st_mtime > UPLOAD_KEEP_SECONDS:
                path.unlink()
        except OSError:
            pass


@router.post("/api/compose/uploads")
async def upload(request: Request, filename: str) -> dict[str, Any]:
    """Raw file as the request body; returns the id the send request refers to."""
    _clean_uploads()
    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="Empty file")
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File too large")
    upload_id = secrets.token_hex(16)
    name = os.path.basename(filename).replace("\x00", "")[:200] or "Anhang"
    content_type = request.headers.get("content-type", "").split(";")[0].strip() or mimetypes.guess_type(name)[0] or "application/octet-stream"
    folder = _uploads_dir()
    (folder / upload_id).write_bytes(data)
    (folder / upload_id).chmod(0o600)
    _write_json(folder / f"{upload_id}.json", {"user": users.CURRENT.get(), "filename": name, "contentType": content_type})
    return {"id": upload_id, "filename": name, "sizeBytes": len(data)}


def _load_upload(upload_id: str) -> tuple[str, str, bytes]:
    if not re.fullmatch(r"[0-9a-f]{32}", upload_id):
        raise HTTPException(status_code=400, detail="Malformed upload id")
    folder = _uploads_dir()
    try:
        meta = json.loads((folder / f"{upload_id}.json").read_text())
        data = (folder / upload_id).read_bytes()
    except (OSError, ValueError):
        raise HTTPException(status_code=404, detail="Upload expired, add the file again")
    if meta.get("user") != users.CURRENT.get():
        raise HTTPException(status_code=404, detail="Upload expired, add the file again")
    return meta["filename"], meta["contentType"], data


# --- Building and queueing the mail --------------------------------------------------------


class SendRequest(BaseModel):
    accountId: str
    to: list[str] = []
    cc: list[str] = []
    bcc: list[str] = []
    subject: str = Field("", max_length=1000)
    html: str = Field("", max_length=5_000_000)
    mode: str = "new"
    item: str | None = None
    box: str = "inbox"
    # Forward: indexes of the original's attachments to send along.
    forwardAttachments: list[int] = []
    uploads: list[str] = []


def _recipients(values: list[str]) -> list[tuple[str, str]]:
    pairs = [(name, address.strip()) for name, address in getaddresses(values) if address.strip()]
    for _, address in pairs:
        if not re.fullmatch(r"[^@\s<>,;]+@[^@\s<>,;]+\.[^@\s<>,;]+", address):
            raise HTTPException(status_code=400, detail=f"Not a mail address: {address}")
    return pairs


DATA_IMAGE = re.compile(r'(?i)src\s*=\s*(["\'])data:(image/[a-z0-9.+-]+);base64,([a-z0-9+/=\s]+)\1')


def _with_inline_images(document: str) -> tuple[str, list[tuple[str, str, bytes]]]:
    """data: images (e.g. from a quoted mail) become cid: parts; many clients block data: URLs."""
    images: list[tuple[str, str, bytes]] = []

    def replace(match: re.Match[str]) -> str:
        try:
            data = base64.b64decode(re.sub(r"\s", "", match.group(3)))
        except ValueError:
            return match.group(0)
        cid = make_msgid(domain="apport.local")[1:-1]
        images.append((cid, match.group(2).lower(), data))
        return f"src={match.group(1)}cid:{cid}{match.group(1)}"

    return DATA_IMAGE.sub(replace, document), images


def _build(body: SendRequest, account: inbox.Account) -> tuple[EmailMessage, list[str], dict[str, Any] | None]:
    to, cc, bcc = _recipients(body.to), _recipients(body.cc), _recipients(body.bcc)
    if not (to or cc or bcc):
        raise HTTPException(status_code=400, detail="No recipient")
    if body.mode not in MODES:
        raise HTTPException(status_code=400, detail="Unknown mode")
    viewer = inbox._viewer()
    new_html = viewer.sanitize_mail_html(body.html)
    new_text = viewer.html_to_text(re.sub(r"(?i)</div\s*>", "</div><br>", new_html))
    quote_html, quote_text = "", ""
    original = None
    reference: dict[str, Any] | None = None
    if body.mode != "new":
        if not body.item:
            raise HTTPException(status_code=400, detail="Reply or forward without the original")
        original_account, original = _original(body.item, body.box)
        quote_html, quote_text = _quote(original, body.mode)
        reference = {"item": body.item, "box": body.box, "accountId": original_account.id, "mode": body.mode}

    settings = _account_settings(account.name)
    message = EmailMessage(policy=policy.SMTP)
    message["From"] = formataddr((settings["name"], account.name)) if settings["name"] else account.name
    if to:
        message["To"] = ", ".join(formataddr(pair) for pair in to)
    if cc:
        message["Cc"] = ", ".join(formataddr(pair) for pair in cc)
    message["Subject"] = body.subject.replace("\x00", "").replace("\n", " ").strip()
    message["Date"] = format_datetime(datetime.now(ZoneInfo("Europe/Berlin")))
    message["Message-ID"] = make_msgid(domain=account.name.rsplit("@", 1)[-1])
    message["MIME-Version"] = "1.0"
    if original is not None and body.mode in ("reply", "replyAll"):
        parent = str(original.get("Message-ID", "")).strip()
        if parent:
            message["In-Reply-To"] = parent
            chain = inbox.MESSAGE_ID_PATTERN.findall(str(original.get("References", "")))
            message["References"] = " ".join([*chain, parent][-20:])

    text = new_text + ("\n\n" + quote_text if quote_text else "")
    document, images = _with_inline_images(
        '<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>'
        f'<div dir="auto">{new_html}</div>{quote_html}</body></html>'
    )
    message.set_content(text)
    message.add_alternative(document, subtype="html")
    html_part = message.get_payload()[-1]
    for cid, content_type, data in images:
        maintype, subtype = content_type.split("/", 1)
        html_part.add_related(data, maintype=maintype, subtype=subtype, cid=f"<{cid}>")

    if original is not None and body.mode == "forward":
        parts = inbox._attachment_parts(original)
        for index in body.forwardAttachments:
            if not 0 <= index < len(parts):
                raise HTTPException(status_code=400, detail="Unknown attachment")
            part = parts[index]
            filename = part.get_filename() or f"Anhang {index + 1}"
            maintype, subtype = part.get_content_type().split("/", 1)
            message.add_attachment(part.get_payload(decode=True) or b"", maintype=maintype, subtype=subtype, filename=filename)
    for upload_id in body.uploads:
        filename, content_type, data = _load_upload(upload_id)
        maintype, _, subtype = content_type.partition("/")
        message.add_attachment(data, maintype=maintype or "application", subtype=subtype or "octet-stream", filename=filename)

    envelope = [address for _, address in to + cc + bcc]
    return message, envelope, reference


def _record_path(send_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", send_id):
        raise HTTPException(status_code=404, detail="Unknown mail")
    return _outbox_dir() / f"{send_id}.json"


def _read_record(send_id: str) -> dict[str, Any]:
    try:
        record = json.loads(_record_path(send_id).read_text())
    except (OSError, ValueError):
        raise HTTPException(status_code=404, detail="Unknown mail")
    if record.get("user") != users.CURRENT.get():
        raise HTTPException(status_code=404, detail="Unknown mail")
    return record


_OUTBOX_LOCK = threading.Lock()
_WAKE = threading.Event()


@router.post("/api/compose/send")
def send(body: SendRequest) -> dict[str, Any]:
    account = inbox.account_by_id(body.accountId)
    if not isinstance(account, inbox.Account):
        raise HTTPException(status_code=409, detail="Sending from this account is not set up yet")
    message, envelope, reference = _build(body, account)
    raw = message.as_bytes(policy=policy.SMTP)
    if len(raw) > MAX_MESSAGE_BYTES:
        raise HTTPException(status_code=413, detail="Mail too large")
    send_id = secrets.token_hex(16)
    folder = _outbox_dir()
    (folder / f"{send_id}.eml").write_bytes(raw)
    (folder / f"{send_id}.eml").chmod(0o600)
    send_at = time.time() + SEND_DELAY_SECONDS
    with _OUTBOX_LOCK:
        _write_json(
            _record_path(send_id),
            {
                "id": send_id,
                "user": users.CURRENT.get(),
                "accountId": account.id,
                "envelope": envelope,
                "reference": reference,
                "sendAt": send_at,
                "status": "queued",
                "error": None,
                "createdAt": time.time(),
            },
        )
    for upload_id in body.uploads:
        for path in (_uploads_dir() / upload_id, _uploads_dir() / f"{upload_id}.json"):
            path.unlink(missing_ok=True)
    _start_worker()
    _WAKE.set()
    return {"id": send_id, "sendAt": send_at, "status": "queued"}


@router.get("/api/compose/outbox/{send_id}")
def outbox_status(send_id: str) -> dict[str, Any]:
    record = _read_record(send_id)
    return {"id": send_id, "status": record["status"], "error": record.get("error"), "sendAt": record["sendAt"]}


@router.post("/api/compose/outbox/{send_id}/cancel")
def cancel(send_id: str) -> dict[str, Any]:
    with _OUTBOX_LOCK:
        record = _read_record(send_id)
        if record["status"] != "queued":
            raise HTTPException(status_code=409, detail=f"Already {record['status']}")
        record["status"] = "cancelled"
        _write_json(_record_path(send_id), record)
    return {"id": send_id, "status": "cancelled"}


# --- Sending -------------------------------------------------------------------------------


def _smtp_send(account: inbox.Account, raw: bytes, envelope: list[str]) -> None:
    server = smtp_server(account)
    context = ssl.create_default_context()
    # Only connecting is retried: once DATA may have been accepted, a retry could send twice.
    # Seen 25.10.: one "Network is unreachable" to smtp.gmail.com (no IPv6 route), next try fine.
    for attempt in range(3):
        try:
            if server.starttls:
                connection: smtplib.SMTP = smtplib.SMTP(server.host, server.port, timeout=SMTP_TIMEOUT_SECONDS)
                connection.starttls(context=context)
            else:
                connection = smtplib.SMTP_SSL(server.host, server.port, timeout=SMTP_TIMEOUT_SECONDS, context=context)
            break
        except (OSError, smtplib.SMTPConnectError):
            if attempt == 2:
                raise
            time.sleep(2)
    with connection:
        connection.login(account.username, account.password)
        refused = connection.sendmail(account.name, envelope, raw)
    if refused:
        raise RuntimeError("Refused: " + ", ".join(refused))


def _after_send(account: inbox.Account, raw: bytes, reference: dict[str, Any] | None) -> list[str]:
    """Copy into Sent where the provider does not, flag the original answered. Best effort."""
    problems: list[str] = []
    try:
        with inbox.imap_connection(account) as conn:
            if not smtp_server(account).keeps_sent:
                folders = inbox.special_folders(conn)
                sent = folders.get("\\sent")
                if not sent:
                    names = set(folders.get("\\x-names", "").split("\n"))
                    sent = next((name for name in ("Sent", "Gesendet", "Gesendete Objekte", "INBOX.Sent", "Sent Messages") if name in names), None)
                if sent:
                    status, data = conn.append(inbox._quote_mailbox(sent), "(\\Seen)", imaplib.Time2Internaldate(time.time()), raw)
                    if status != "OK":
                        problems.append(f"APPEND {sent}: {data}")
                else:
                    problems.append("no Sent folder")
    except Exception as error:  # noqa: BLE001 - the mail is out; report, do not fail
        problems.append(f"Sent copy: {error}")
    if reference and reference.get("box") == "inbox" and reference.get("accountId") == account.id:
        try:
            _, uidvalidity, uid = inbox._split_item_id(reference["item"])
            flag = "$Forwarded" if reference.get("mode") == "forward" else "\\Answered"
            with inbox.imap_connection(account) as conn:
                conn.select("INBOX")
                if inbox._uidvalidity(conn) == uidvalidity:
                    conn.uid("STORE", uid, "+FLAGS.SILENT", f"({flag})")
        except Exception as error:  # noqa: BLE001
            problems.append(f"flag original: {error}")
    return problems


def _deliver(record: dict[str, Any]) -> None:
    users.CURRENT.set(record["user"])
    path = _record_path(record["id"])
    raw = (_outbox_dir() / f"{record['id']}.eml").read_bytes()
    try:
        account = inbox.account_by_id(record["accountId"])
        if not isinstance(account, inbox.Account):
            raise RuntimeError("account cannot send")
        _smtp_send(account, raw, record["envelope"])
    except Exception as error:  # noqa: BLE001
        record.update(status="failed", error=str(getattr(error, "detail", error))[:500])
        with _OUTBOX_LOCK:
            _write_json(path, record)
        print(f"compose: send {record['id']} failed: {record['error']}", flush=True)
        return
    record.update(status="sent", sentAt=time.time())
    with _OUTBOX_LOCK:
        _write_json(path, record)
    problems = _after_send(account, raw, record["reference"])
    if problems:
        record["warnings"] = problems
        with _OUTBOX_LOCK:
            _write_json(path, record)
    print(f"compose: sent {record['id']} from {account.name} to {len(record['envelope'])} recipient(s)"
          + (f", warnings: {problems}" if problems else ""), flush=True)


def _due() -> tuple[list[dict[str, Any]], float]:
    now = time.time()
    due, next_at = [], now + 60
    for path in _outbox_dir().glob("*.json"):
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if record.get("status") in ("sent", "failed", "cancelled"):
            if now - record.get("createdAt", now) > OUTBOX_KEEP_SECONDS:
                path.unlink(missing_ok=True)
                (_outbox_dir() / f"{path.stem}.eml").unlink(missing_ok=True)
            continue
        if record.get("status") == "sending":
            # Interrupted mid-send by a restart: whether it went out is unknown, so do not resend.
            record.update(status="failed", error="Server restarted while sending; check Sent before resending")
            _write_json(path, record)
            continue
        if record["sendAt"] <= now:
            record["status"] = "sending"
            _write_json(path, record)
            due.append(record)
        else:
            next_at = min(next_at, record["sendAt"])
    return due, next_at


def _worker() -> None:
    while True:
        with _OUTBOX_LOCK:
            due, next_at = _due()
        for record in due:
            _deliver(record)
        _WAKE.wait(timeout=max(0.2, next_at - time.time()))
        _WAKE.clear()


_WORKER_LOCK = threading.Lock()
_WORKER: threading.Thread | None = None


def _start_worker() -> None:
    global _WORKER
    with _WORKER_LOCK:
        if _WORKER is None or not _WORKER.is_alive():
            _WORKER = threading.Thread(target=_worker, name="outbox", daemon=True)
            _WORKER.start()


@router.on_event("startup")
def _resume_outbox() -> None:
    _start_worker()
