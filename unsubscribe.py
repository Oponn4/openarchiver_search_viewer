"""Abmelden von Newslettern über den List-Unsubscribe-Header (RFC 2369, RFC 8058).

Nur der Header, kein Suchen nach Links im HTML: ein Link aus dem Mailtext kann beim Öffnen
bestätigen, dass die Adresse gelesen wird. One-Click (RFC 8058) schickt das Backend selbst ab,
ohne Cookies und ohne Weiterleitungen zu folgen; Web-Links und mailto öffnet die App.
"""

from __future__ import annotations

import re
from email.message import EmailMessage
from typing import Any
from urllib.parse import urlsplit

import requests

import logos

TIMEOUT = 10
_ENTRY = re.compile(r"<\s*([^>]+?)\s*>")


def parse(message: EmailMessage) -> dict[str, Any] | None:
    """Abmeldeweg einer Mail: `oneClick` (POST durchs Backend), `web` (Link) oder `mail`."""
    header = re.sub(r"\s+", "", str(message.get("List-Unsubscribe") or ""))
    if not header:
        return None
    entries = _ENTRY.findall(header)
    web = next((entry for entry in entries if urlsplit(entry).scheme == "https"), None)
    web = web or next((entry for entry in entries if urlsplit(entry).scheme == "http"), None)
    mailto = next((entry for entry in entries if entry.lower().startswith("mailto:")), None)
    post = re.sub(r"\s+", "", str(message.get("List-Unsubscribe-Post") or "")).lower()
    if web and web.startswith("https://") and post == "list-unsubscribe=one-click":
        return {"method": "oneClick", "url": web, "mailto": mailto}
    if web:
        return {"method": "web", "url": web, "mailto": mailto}
    if mailto:
        return {"method": "mail", "url": None, "mailto": mailto}
    return None


def one_click(url: str) -> bool:
    """RFC 8058: POST mit festem Körper, nur an öffentliche Adressen, keine Weiterleitung."""
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or not logos._public(parts.hostname):
        return False
    try:
        response = requests.post(
            url,
            data="List-Unsubscribe=One-Click",
            headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": logos.USER_AGENT},
            timeout=TIMEOUT,
            allow_redirects=False,
        )
    except requests.RequestException:
        return False
    # Manche Anbieter antworten mit einer Weiterleitung auf ihre Bestätigungsseite.
    return 200 <= response.status_code < 400
