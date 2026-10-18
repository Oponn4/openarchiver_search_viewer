"""Sender logos for the app's mail list, by sender domain.

The phone only ever asks this backend; the backend asks, in this order:
1. BIMI: the brand logo a domain publishes in DNS (default._bimi.<domain>, an SVG), rendered to PNG.
2. The website's own apple-touch-icon (from its HTML, then the well-known path).
3. Google's favicon service (Christian's choice 2026-10-18; Google learns the domain, not the user).
4. The website's favicon.ico.

Freemail domains get no logo (the app shows initials), otherwise every private mail would carry
the Gmail logo. Results are kept on disk: hits for two weeks, misses for a day.

Every fetch goes only to public addresses: the domain comes from a mail's From header, so anyone
can pick it, and a name resolving to an internal address must not turn this into a probe of the
container network (Open Archiver's database and search sit next to it).
"""

from __future__ import annotations

import ipaddress
import os
import re
import socket
import threading
import time
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

router = APIRouter()

HIT_SECONDS = 14 * 24 * 3600
MISS_SECONDS = 24 * 3600
MAX_BYTES = 1_000_000
TIMEOUT = 5
MIN_GOOGLE_BYTES = 200
USER_AGENT = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148"

FREEMAIL = {
    "gmail.com", "googlemail.com", "gmx.de", "gmx.net", "gmx.at", "gmx.ch", "gmx.com", "web.de",
    "t-online.de", "icloud.com", "me.com", "mac.com", "outlook.com", "outlook.de", "hotmail.com",
    "hotmail.de", "live.com", "live.de", "msn.com", "yahoo.com", "yahoo.de", "aol.com", "aol.de",
    "freenet.de", "posteo.de", "posteo.net", "mailbox.org", "proton.me", "protonmail.com",
    "arcor.de", "online.de", "vodafone.de", "1und1.de", "kabelmail.de", "mail.de", "email.de",
    "gmx.org",
}
# Two-label public suffixes common in our mail; enough to cut "news.shop.co.uk" to "shop.co.uk".
TWO_LABEL_SUFFIXES = {"co.uk", "org.uk", "ac.uk", "com.au", "co.at", "or.at", "co.jp", "com.br", "co.nz"}
DOMAIN_PATTERN = re.compile(r"^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")

_LOCK = threading.Lock()
_INFLIGHT: dict[str, threading.Lock] = {}


def base_domain(domain: str) -> str:
    labels = domain.strip().lower().rstrip(".").split(".")
    if len(labels) >= 3 and ".".join(labels[-2:]) in TWO_LABEL_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def _cache_dir() -> Path:
    path = Path(os.environ.get("APPORT_STATE_DIR", "/data")) / "logos"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _public(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except OSError:
        return False
    addresses = {info[4][0] for info in infos}
    return bool(addresses) and all(ipaddress.ip_address(address).is_global for address in addresses)


def _get(url: str, accept_html: bool = False) -> tuple[bytes, str] | None:
    """GET over https to public hosts only, redirects checked hop by hop, body capped."""
    for _ in range(4):
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.hostname or not _public(parts.hostname):
            return None
        try:
            response = requests.get(
                url, timeout=TIMEOUT, allow_redirects=False, stream=True,
                headers={"User-Agent": USER_AGENT, "Accept": "text/html,image/*;q=0.9,*/*;q=0.5"},
            )
        except requests.RequestException:
            return None
        with response:
            if response.is_redirect and response.headers.get("location"):
                url = urljoin(url, response.headers["location"])
                continue
            if response.status_code != 200:
                return None
            content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
            limit = 300_000 if accept_html else MAX_BYTES
            body = bytearray()
            for chunk in response.iter_content(16384):
                body.extend(chunk)
                if len(body) > limit:
                    if accept_html:
                        break
                    return None
            return bytes(body), content_type
    return None


def _image(data: bytes, content_type: str) -> tuple[bytes, str] | None:
    if data.startswith(b"\x89PNG"):
        return data, "image/png"
    if data.startswith(b"\xff\xd8"):
        return data, "image/jpeg"
    if data.startswith(b"GIF8"):
        return data, "image/gif"
    if data[:4] == b"\x00\x00\x01\x00":
        return data, "image/x-icon"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return data, "image/webp"
    if b"<svg" in data[:2048] or content_type == "image/svg+xml":
        return _svg_to_png(data)
    return None


def _svg_to_png(data: bytes) -> tuple[bytes, str] | None:
    try:
        import cairosvg

        return cairosvg.svg2png(bytestring=data, output_width=256, output_height=256), "image/png"
    except Exception:
        return None


def _bimi(domain: str) -> tuple[bytes, str] | None:
    try:
        import dns.resolver

        answers = dns.resolver.resolve(f"default._bimi.{domain}", "TXT", lifetime=TIMEOUT)
    except Exception:
        return None
    for answer in answers:
        record = b"".join(answer.strings).decode(errors="ignore")
        match = re.search(r"(?:^|;)\s*l=([^;\s]+)", record)
        if record.lower().startswith("v=bimi1") and match:
            fetched = _get(match.group(1))
            if fetched:
                return _svg_to_png(fetched[0])
    return None


class _IconLinks(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[int, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "link":
            return
        values = {key.lower(): (value or "") for key, value in attrs}
        rel = values.get("rel", "").lower()
        href = values.get("href", "")
        if not href or "icon" not in rel or "mask-icon" in rel:
            return
        size = 0
        match = re.search(r"(\d+)x\d+", values.get("sizes", ""))
        if match:
            size = int(match.group(1))
        elif "apple-touch-icon" in rel:
            size = 180
        self.links.append((size, href))


def _website_icons(domain: str) -> list[str]:
    for host in (domain, f"www.{domain}"):
        page = _get(f"https://{host}/", accept_html=True)
        if not page:
            continue
        parser = _IconLinks()
        try:
            parser.feed(page[0].decode("utf-8", errors="ignore"))
        except Exception:
            pass
        links = [urljoin(f"https://{host}/", href) for size, href in sorted(parser.links, reverse=True) if size >= 64]
        return links + [f"https://{host}/apple-touch-icon.png"]
    return [f"https://{domain}/apple-touch-icon.png", f"https://www.{domain}/apple-touch-icon.png"]


def _google(domain: str) -> tuple[bytes, str] | None:
    fetched = _get(f"https://www.google.com/s2/favicons?domain={domain}&sz=128")
    # Google answers unknown domains with a small default globe (and 404); only a real icon counts.
    if fetched and len(fetched[0]) >= MIN_GOOGLE_BYTES:
        return _image(*fetched)
    return None


def resolve(domain: str) -> tuple[bytes, str] | None:
    result = _bimi(domain)
    if result:
        return result
    for url in _website_icons(domain):
        fetched = _get(url)
        if fetched and (image := _image(*fetched)):
            return image
    result = _google(domain)
    if result:
        return result
    fetched = _get(f"https://{domain}/favicon.ico")
    return _image(*fetched) if fetched else None


def logo(domain: str) -> tuple[bytes, str] | None:
    domain = base_domain(domain)
    if not DOMAIN_PATTERN.match(domain) or domain in FREEMAIL:
        return None
    directory = _cache_dir()
    with _LOCK:
        domain_lock = _INFLIGHT.setdefault(domain, threading.Lock())
    # One lookup per domain at a time: the whole list asks for the same logos at once.
    with domain_lock:
        for path in directory.glob(f"{domain}.*"):
            age = time.time() - path.stat().st_mtime
            if path.suffix == ".miss":
                if age < MISS_SECONDS:
                    return None
            elif age < HIT_SECONDS:
                return path.read_bytes(), _content_type(path.suffix)
            path.unlink(missing_ok=True)
        result = resolve(domain)
        if result is None:
            (directory / f"{domain}.miss").touch()
            return None
        data, content_type = result
        (directory / f"{domain}{_suffix(content_type)}").write_bytes(data)
        return result


SUFFIXES = {"image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/x-icon": ".ico", "image/webp": ".webp"}


def _suffix(content_type: str) -> str:
    return SUFFIXES.get(content_type, ".png")


def _content_type(suffix: str) -> str:
    return next((key for key, value in SUFFIXES.items() if value == suffix), "image/png")


@router.get("/api/logo/{domain}")
def get_logo(domain: str) -> Response:
    result = logo(domain.lower())
    if result is None:
        raise HTTPException(status_code=404, detail="No logo")
    data, content_type = result
    return Response(content=data, media_type=content_type, headers={"Cache-Control": "private, max-age=604800"})
