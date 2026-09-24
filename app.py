from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import tomllib
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from html import escape, unescape
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlsplit
from uuid import UUID
import uuid
import random
import asyncio
import requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Depends, Cookie, Request
from fastapi.responses import FileResponse, Response, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel


ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
CONFIG_PATH = ROOT / "config.toml"
MIN_SEARCH_LIMIT = 10
LOGIN_REMEMBER_SECONDS = 7 * 24 * 60 * 60
ACTIVE_ATTACHMENT_EXTENSIONS = {
    ".htm",
    ".html",
    ".mht",
    ".mhtml",
    ".svg",
    ".xht",
    ".xhtml",
    ".xml",
}
ACTIVE_ATTACHMENT_MIME_TYPES = {
    "application/xhtml+xml",
    "application/xml",
    "image/svg+xml",
    "text/html",
    "text/xml",
}

load_dotenv()


def required_env(name: str) -> str:
    value = os.getenv(name)
    if value is None or not value.strip():
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value.strip()


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        raise RuntimeError(f"Missing config file: {CONFIG_PATH}")
    with CONFIG_PATH.open("rb") as config_file:
        return tomllib.load(config_file)


CONFIG = load_config()

SUPPORTED_LANGUAGES = {"ko", "en"}
LOCALES = {
    "ko": "ko-KR",
    "en": "en-US",
}

I18N = {
    "ko": {
        "appTitle": "업무 이메일 검색시스템",
        "indexPageTitle": "{title}",
        "loginPageTitle": "로그인 - {title}",
        "mailViewTitle": "메일 보기",
        "searchResults": "검색 결과",
        "logout": "로그아웃",
        "searchPlaceholder": "검색어를 입력하세요.",
        "searchTermLabel": "검색어",
        "search": "검색",
        "sortMode": "정렬 방식",
        "sortRelevance": "일치도순",
        "sortDesc": "최신순",
        "sortAsc": "오래된순",
        "exactTitle": "단어가 정확히 일치하는 메일만 검색합니다.",
        "exact": "완전일치",
        "limitTitle": "값을 늘릴 수록 검색어와 덜 일치하는 메일까지 나타납니다.",
        "limit": "결과수",
        "clear": "지우기",
        "clearTitle": "검색어와 필터를 모두 지웁니다.",
        "advanced": "고급",
        "fromLabel": "보낸사람",
        "fromPlaceholder": "발신자 이메일",
        "dateFromLabel": "시작일",
        "dateToLabel": "종료일",
        "attachmentLabel": "첨부파일",
        "attachmentAny": "모두",
        "attachmentYes": "있음",
        "attachmentNo": "없음",
        "enterSearchTerm": "검색어를 입력하세요.",
        "previousMail": "이전 메일",
        "nextMail": "다음 메일",
        "emailSearchResults": "이메일 검색 결과",
        "emailContent": "이메일 내용",
        "selectMail": "메일을 선택하세요.",
        "downloadEml": ".eml 다운로드",
        "openInNewWindow": "새 창에서 열기",
        "selectMailHelp": "메일을 선택하면 검색 결과의 본문 미리보기가 여기에 표시됩니다.",
        "passwordPrompt": "비밀번호 입력",
        "passwordPlaceholder": "비밀번호",
        "capsLockWarning": "Caps Lock이 켜져 있습니다.",
        "loginButton": "접속하기",
        "checking": "확인 중...",
        "invalidPassword": "비밀번호가 틀렸습니다.",
        "networkError": "네트워크 오류가 발생했습니다.",
        "loadingMail": "메일을 불러오는 중입니다.",
        "noSubject": "(제목 없음)",
        "noSender": "발신자 없음",
        "from": "보낸 사람",
        "to": "받는 사람",
        "cc": "참조",
        "date": "날짜",
        "attachment": "첨부",
        "attachments": "첨부파일 {count}개",
        "attachmentsShort": "첨부 {count}",
        "none": "없음",
        "mailHtmlBody": "메일 HTML 본문",
        "attachmentPreview": "첨부파일 미리보기",
        "unsupportedPreview": "이 형식은 브라우저에서 바로 미리보기 어렵습니다.",
        "openOrDownload": "열기 또는 다운로드",
        "noBodyInSearchResult": "검색 결과에 본문 텍스트가 없습니다. 전체 메일은 새 창에서 열어 확인하세요.",
        "noPreviewBody": "검색 결과 미리보기 본문이 없습니다.",
        "noResults": "검색 결과가 없습니다.",
        "searching": "검색 중",
        "mailDetailRequestFailed": "메일 상세 요청이 실패했습니다.",
        "searchRequestFailed": "검색 요청이 실패했습니다.",
        "searchFailed": "검색 실패",
        "shownResults": "{shown}개 표시 · 전체 {total}개 · {sortLabel}{looseLabel}",
        "sortRelevanceSummary": "관련성순",
        "sortDescSummary": "최신순",
        "sortAscSummary": "오래된순",
        "looseSummary": " · 느슨하게",
        "mailLoadFailed": "메일을 불러오지 못했습니다.",
        "noBody": "본문 텍스트가 없습니다.",
        "searchOpenArchiverFailed": "OpenArchiver 검색 요청이 실패했습니다.",
        "mailOpenArchiverFailed": "OpenArchiver 메일 상세 요청이 실패했습니다.",
        "openArchiverConnectionFailed": "OpenArchiver에 연결할 수 없습니다: {error}",
        "invalidSearchResponse": "OpenArchiver 검색 응답 형식이 올바르지 않습니다.",
        "invalidMailResponse": "OpenArchiver 메일 상세 응답 형식이 올바르지 않습니다.",
        "missingEmlData": "EML 데이터를 찾을 수 없습니다.",
        "noAttachments": "첨부파일이 없습니다.",
        "attachmentNotFound": "첨부파일을 찾을 수 없습니다.",
        "missingAttachmentStoragePath": "첨부파일 storagePath가 없습니다.",
        "attachmentDownloadFailed": "첨부파일 다운로드 요청이 실패했습니다.",
    },
    "en": {
        "appTitle": "Company Email Search",
        "indexPageTitle": "{title}",
        "loginPageTitle": "Sign in - {title}",
        "mailViewTitle": "Mail View",
        "searchResults": "Search results",
        "logout": "Log out",
        "searchPlaceholder": "Enter search terms.",
        "searchTermLabel": "Search terms",
        "search": "Search",
        "sortMode": "Sort order",
        "sortRelevance": "Best match",
        "sortDesc": "Newest",
        "sortAsc": "Oldest",
        "exactTitle": "Search only mail that contains exact word matches.",
        "exact": "Exact match",
        "limitTitle": "Increase this value to include more loosely matching mail.",
        "limit": "Results",
        "clear": "Clear",
        "clearTitle": "Clear the search term and all filters.",
        "advanced": "Advanced",
        "fromLabel": "From",
        "fromPlaceholder": "name@example.com",
        "dateFromLabel": "From date",
        "dateToLabel": "To date",
        "attachmentLabel": "Attachment",
        "attachmentAny": "Any",
        "attachmentYes": "Yes",
        "attachmentNo": "No",
        "enterSearchTerm": "Enter search terms.",
        "previousMail": "Previous mail",
        "nextMail": "Next mail",
        "emailSearchResults": "Email search results",
        "emailContent": "Email content",
        "selectMail": "Select a mail.",
        "downloadEml": "Download .eml",
        "openInNewWindow": "Open in new window",
        "selectMailHelp": "Select a mail to show a body preview from the search results here.",
        "passwordPrompt": "Enter password",
        "passwordPlaceholder": "Password",
        "capsLockWarning": "Caps Lock is on.",
        "loginButton": "Sign in",
        "checking": "Checking...",
        "invalidPassword": "The password is incorrect.",
        "networkError": "A network error occurred.",
        "loadingMail": "Loading mail.",
        "noSubject": "(No subject)",
        "noSender": "No sender",
        "from": "From",
        "to": "To",
        "cc": "Cc",
        "date": "Date",
        "attachment": "Attachments",
        "attachments": "{count} attachments",
        "attachmentsShort": "{count} attachments",
        "none": "None",
        "mailHtmlBody": "Mail HTML body",
        "attachmentPreview": "Attachment preview",
        "unsupportedPreview": "This file type is difficult to preview directly in the browser.",
        "openOrDownload": "Open or download",
        "noBodyInSearchResult": "No body text is available in the search result. Open the full mail in a new window.",
        "noPreviewBody": "No search result preview body is available.",
        "noResults": "No search results.",
        "searching": "Searching",
        "mailDetailRequestFailed": "The mail detail request failed.",
        "searchRequestFailed": "The search request failed.",
        "searchFailed": "Search failed",
        "shownResults": "{shown} shown · {total} total · {sortLabel}{looseLabel}",
        "sortRelevanceSummary": "Best match",
        "sortDescSummary": "Newest",
        "sortAscSummary": "Oldest",
        "looseSummary": " · loose",
        "mailLoadFailed": "Could not load the mail.",
        "noBody": "No body text.",
        "searchOpenArchiverFailed": "The OpenArchiver search request failed.",
        "mailOpenArchiverFailed": "The OpenArchiver mail detail request failed.",
        "openArchiverConnectionFailed": "Could not connect to OpenArchiver: {error}",
        "invalidSearchResponse": "The OpenArchiver search response format is invalid.",
        "invalidMailResponse": "The OpenArchiver mail detail response format is invalid.",
        "missingEmlData": "EML data was not found.",
        "noAttachments": "There are no attachments.",
        "attachmentNotFound": "The attachment was not found.",
        "missingAttachmentStoragePath": "The attachment storagePath is missing.",
        "attachmentDownloadFailed": "The attachment download request failed.",
    },
}


def required_config(section: str, name: str) -> Any:
    section_value = CONFIG.get(section)
    if not isinstance(section_value, dict):
        raise RuntimeError(f"Missing config section: {section}")
    value = section_value.get(name)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise RuntimeError(f"Missing config value: {section}.{name}")
    return value


def required_config_str(section: str, name: str) -> str:
    value = required_config(section, name)
    if not isinstance(value, str):
        raise RuntimeError(f"Invalid string config value: {section}.{name}")
    return value.strip()


def required_config_language(section: str, name: str) -> str:
    value = required_config_str(section, name).lower()
    if value not in SUPPORTED_LANGUAGES:
        supported = ", ".join(sorted(SUPPORTED_LANGUAGES))
        raise RuntimeError(f"{section}.{name} must be one of: {supported}")
    return value


def required_config_bool(section: str, name: str) -> bool:
    value = required_config(section, name)
    if not isinstance(value, bool):
        raise RuntimeError(f"Invalid boolean config value: {section}.{name}")
    return value


def optional_config_bool(section: str, name: str, default: bool) -> bool:
    section_value = CONFIG.get(section)
    if not isinstance(section_value, dict) or name not in section_value:
        return default
    value = section_value[name]
    if not isinstance(value, bool):
        raise RuntimeError(f"Invalid boolean config value: {section}.{name}")
    return value


def required_config_positive_int(section: str, name: str) -> int:
    value = required_config(section, name)
    if not isinstance(value, int):
        raise RuntimeError(f"Invalid integer config value: {section}.{name}")
    if value < 1:
        raise RuntimeError(f"{section}.{name} must be greater than 0")
    return value


NEED_LOG_IN = required_config_bool("app", "need_log_in")
REMEMBER_LOGIN = optional_config_bool("app", "remember_login", False)
SITE_PASSWORD = required_env("SITE_PASSWORD") if NEED_LOG_IN else ""
APP_LANGUAGE = required_config_language("app", "language")
APP_MESSAGES = I18N[APP_LANGUAGE]
APP_TITLE = required_config_str("app", "title")


app = FastAPI(title=APP_TITLE)


@app.middleware("http")
async def no_cache_headers(request: Request, call_next):
    # Starlette's StaticFiles sets ETag/Last-Modified but no Cache-Control,
    # so browsers fall back to heuristic caching and can keep serving an old
    # JS/CSS/HTML bundle after a deploy. iOS Safari has no accessible
    # hard-reload gesture, so a stale heuristic cache there can outlive any
    # "reload" the user can actually perform. Force revalidation on every
    # load instead; ETag/Last-Modified still make that a cheap 304.
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

VALID_SESSIONS: dict[str, float | None] = {}


def t(key: str, **values: Any) -> str:
    message = APP_MESSAGES.get(key, I18N["ko"].get(key, key))
    return message.format(**values) if values else message


def app_config_script() -> str:
    payload = {
        "language": APP_LANGUAGE,
        "locale": LOCALES[APP_LANGUAGE],
        "title": APP_TITLE,
        "messages": APP_MESSAGES,
    }
    config_json = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    return f"<script>window.APP_CONFIG = {config_json};</script>"


def render_static_template(filename: str, page_title: str) -> str:
    content = (STATIC_DIR / filename).read_text(encoding="utf-8")
    return (
        content
        .replace("__APP_LANG__", escape(APP_LANGUAGE))
        .replace("__APP_TITLE__", escape(APP_TITLE))
        .replace("__PAGE_TITLE__", escape(page_title))
        .replace("__APP_CONFIG_SCRIPT__", app_config_script())
    )

def current_timestamp() -> float:
    return datetime.now(timezone.utc).timestamp()


def remember_login_expires_at() -> float | None:
    if not REMEMBER_LOGIN:
        return None
    return current_timestamp() + LOGIN_REMEMBER_SECONDS


def prune_expired_sessions() -> None:
    now = current_timestamp()
    for token, expires_at in list(VALID_SESSIONS.items()):
        if expires_at is not None and expires_at <= now:
            VALID_SESSIONS.pop(token, None)


def is_valid_auth_token(auth_token: str | None) -> bool:
    if not NEED_LOG_IN:
        return True
    if not auth_token or auth_token not in VALID_SESSIONS:
        return False
    expires_at = VALID_SESSIONS[auth_token]
    if expires_at is not None and expires_at <= current_timestamp():
        VALID_SESSIONS.pop(auth_token, None)
        return False
    return True


def verify_api_auth(auth_token: str | None = Cookie(None)):
    if not is_valid_auth_token(auth_token):
        raise HTTPException(status_code=401, detail="Unauthorized")

class LoginRequest(BaseModel):
    password: str

@app.post("/api/login")
async def login(req: LoginRequest):
    if not NEED_LOG_IN:
        return JSONResponse({"success": True})

    if req.password != SITE_PASSWORD:
        await asyncio.sleep(random.uniform(1.0, 3.0))
        raise HTTPException(status_code=401, detail=t("invalidPassword"))
    
    prune_expired_sessions()
    token = uuid.uuid4().hex
    VALID_SESSIONS[token] = remember_login_expires_at()
    response = JSONResponse({"success": True})
    cookie_options: dict[str, Any] = {"httponly": True, "samesite": "lax"}
    if REMEMBER_LOGIN:
        cookie_options["max_age"] = LOGIN_REMEMBER_SECONDS
    response.set_cookie(key="auth_token", value=token, **cookie_options)
    return response

@app.post("/api/logout")
def logout(auth_token: str | None = Cookie(None)):
    if auth_token in VALID_SESSIONS:
        VALID_SESSIONS.pop(auth_token, None)
    response = JSONResponse({"success": True})
    response.delete_cookie("auth_token")
    return response

@app.get("/login")
def login_page() -> Response:
    if not NEED_LOG_IN:
        return RedirectResponse(url="/")
    content = render_static_template("login.html", t("loginPageTitle", title=APP_TITLE))
    return Response(content=content, media_type="text/html")


class Settings(BaseModel):
    search_url: str
    mail_get_url: str
    storage_download_url: str
    api_key: str
    search_matching_strategy: str
    search_max_limit: int
    enable_exact_filter: bool


def join_openarchiver_url(base_url: str, path_or_url: str) -> str:
    value = path_or_url.strip()
    parsed = urlsplit(value)
    if parsed.scheme and parsed.netloc:
        return value
    if not value:
        return base_url.rstrip("/")
    return f"{base_url.rstrip('/')}/{value.lstrip('/')}"


def openarchiver_url(path_config_name: str) -> str:
    return join_openarchiver_url(
        required_env("OPENARCHIVER_URL"),
        required_config_str("openarchiver", path_config_name),
    )


def get_settings() -> Settings:
    search_max_limit = required_config_positive_int("search", "max_limit")
    if search_max_limit < MIN_SEARCH_LIMIT:
        raise RuntimeError(f"search.max_limit must be at least {MIN_SEARCH_LIMIT}")

    return Settings(
        search_url=openarchiver_url("api_search"),
        mail_get_url=openarchiver_url("api_mail_get"),
        storage_download_url=openarchiver_url("api_storage_download"),
        api_key=required_env("OPENARCHIVER_API_KEY"),
        search_matching_strategy=required_config_str("search", "matching_strategy"),
        search_max_limit=search_max_limit,
        enable_exact_filter=required_config_bool("search", "enable_exact_filter"),
    )


SETTINGS = get_settings()


_QUOTE_CHARS = "\"“”„‟"


def phrase_query(query: str) -> str | None:
    """None unless the whole query is one quoted phrase ("be quite"), in which case
    returns the inner text unquoted. Handles straight quotes and the curly variants iOS
    autocorrect substitutes them with, on either side (open/close needn't match).
    A single-word "quoted" query isn't treated as a phrase -- Meilisearch's phrase syntax
    only means anything for 2+ words in a fixed order; for one word it's identical to a
    plain search, so falling through to the normal (typo-tolerant) path is strictly better.
    """
    stripped = query.strip()
    if len(stripped) < 2 or stripped[0] not in _QUOTE_CHARS or stripped[-1] not in _QUOTE_CHARS:
        return None
    inner = stripped[1:-1].strip()
    return inner if inner and len(inner.split()) > 1 else None


def quote_keywords(query: str) -> str:
    words = [word.strip().strip("\"'") for word in query.split()]
    return " ".join(f'"{word}"' for word in words if word)


def loose_keywords(query: str) -> str:
    return " ".join(word.strip().strip("\"'") for word in query.split() if word.strip().strip("\"'"))


def search_terms(query: str) -> list[str]:
    # Strips the same quote characters phrase_query() recognizes (straight and curly), so a
    # phrase query's highlight terms don't carry a stray leading/trailing quote glyph that
    # would never match anything in the rendered text and silently fail to highlight.
    strip_chars = "\"'" + _QUOTE_CHARS
    return [word.strip().strip(strip_chars).lower() for word in query.split() if word.strip().strip(strip_chars)]


def timestamp_to_iso(timestamp: Any) -> str | None:
    try:
        timestamp_int = int(timestamp)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(timestamp_int / 1000, tz=timezone.utc).isoformat()


def iso_to_timestamp(value: Any) -> int | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return int(parsed.timestamp() * 1000)


def normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def html_to_text(text: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p\s*>", "\n\n", text)
    text = re.sub(r"(?s)<.*?>", " ", text)
    return normalize_text(unescape(text))


def sanitize_mail_html(text: str) -> str:
    text = re.sub(r"(?is)<(script|iframe|object|embed).*?>.*?</\1>", " ", text)
    text = re.sub(r"(?is)\s+on[a-z]+\s*=\s*(\".*?\"|'.*?'|[^\s>]+)", "", text)
    text = re.sub(r"(?is)(href|src)\s*=\s*([\"'])\s*javascript:.*?\2", r"\1=\2#\2", text)
    return text


def strip_highlight(text: str) -> str:
    text = re.sub(r"</?em>", "", text)
    return unescape(text)


def safe_highlight_html(text: str) -> str:
    parts = re.split(r"(</?em>)", text)
    output: list[str] = []
    is_marked = False
    for part in parts:
        if part == "<em>":
            output.append("<mark>")
            is_marked = True
        elif part == "</em>":
            output.append("</mark>")
            is_marked = False
        elif part:
            output.append(escape(unescape(part)))
    if is_marked:
        output.append("</mark>")
    return "".join(output)


def text_snippet(text: str, terms: list[str], width: int = 180) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""

    lower_text = text.lower()
    positions = [lower_text.find(term) for term in terms if term]
    positions = [position for position in positions if position >= 0]
    if not positions:
        return text[:width]

    match_position = min(positions)
    start = max(match_position - 55, 0)
    end = min(start + width, len(text))
    if end - start < width:
        start = max(end - width, 0)

    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(text) else ""
    return f"{prefix}{text[start:end].strip()}{suffix}"


def highlighted_snippet(text: str, terms: list[str], width: int = 180) -> str:
    snippet = text_snippet(text, terms, width=width)
    if not snippet:
        return ""

    escaped = escape(snippet)
    for term in sorted(terms, key=len, reverse=True):
        if not term:
            continue
        escaped = re.sub(
            re.escape(escape(term)),
            lambda match: f"<mark>{match.group(0)}</mark>",
            escaped,
            flags=re.IGNORECASE,
        )
    return escaped


def format_address_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []

    addresses: list[str] = []
    for item in value:
        if isinstance(item, str):
            addresses.append(item)
            continue
        if not isinstance(item, dict):
            continue
        recipient = item.get("recipient")
        if isinstance(recipient, dict):
            item = recipient
        name = str(item.get("name") or "").strip()
        email = str(item.get("email") or item.get("emailAddress") or item.get("address") or "").strip()
        if name and email:
            addresses.append(f"{name} <{email}>")
        elif email:
            addresses.append(email)
        elif name:
            addresses.append(name)
    return addresses


def raw_bytes(raw: Any) -> bytes | None:
    if isinstance(raw, str):
        return raw.encode("utf-8", errors="ignore")
    if not isinstance(raw, dict):
        return None
    data = raw.get("data")
    if isinstance(data, list):
        try:
            return bytes(int(value) for value in data)
        except (TypeError, ValueError):
            return None
    return None


def parsed_raw_message(raw: Any) -> Any | None:
    payload = raw_bytes(raw)
    if not payload:
        return None

    try:
        return BytesParser(policy=policy.default).parsebytes(payload)
    except Exception:
        return None


def replace_cid_images(html: str, cid_images: dict[str, str]) -> str:
    if not cid_images:
        return html

    def replace_match(match: re.Match[str]) -> str:
        cid = unquote(match.group(1)).strip("<>")
        return cid_images.get(cid, match.group(0))

    return re.sub(r"cid:([^\"'>\s)]+)", replace_match, html, flags=re.IGNORECASE)


def extract_raw_content(raw: Any) -> dict[str, str]:
    message = parsed_raw_message(raw)
    if message is None:
        return {"text": "", "html": ""}

    plain_parts: list[str] = []
    html_parts: list[str] = []
    cid_images: dict[str, str] = {}
    for part in message.walk():
        content_type = part.get_content_type()
        content_id = str(part.get("Content-ID") or "").strip().strip("<>")
        if content_id and content_type.startswith("image/"):
            payload = part.get_payload(decode=True)
            if isinstance(payload, bytes):
                encoded = base64.b64encode(payload).decode("ascii")
                cid_images[content_id] = f"data:{content_type};base64,{encoded}"
            continue

        if part.get_content_disposition() == "attachment":
            continue

        try:
            content = part.get_content()
        except Exception:
            continue
        if not isinstance(content, str):
            continue
        if content_type == "text/plain":
            plain_parts.append(content)
        elif content_type == "text/html":
            html_parts.append(content)

    html_body = ""
    if html_parts:
        html_body = replace_cid_images("\n\n".join(html_parts), cid_images)
        html_body = sanitize_mail_html(html_body)

    if plain_parts:
        text_body = normalize_text("\n\n".join(plain_parts))
    elif html_body:
        text_body = html_to_text(html_body)
    else:
        text_body = ""

    return {"text": text_body, "html": html_body}


def extract_raw_body(raw: Any) -> str:
    return extract_raw_content(raw)["text"]


def raw_attachment(
    raw: Any,
    filename: str,
    mime_type: str,
    size_bytes: Any,
) -> tuple[bytes, str] | None:
    message = parsed_raw_message(raw)
    if message is None:
        return None

    candidates: list[tuple[bytes, str, str | None]] = []
    for part in message.walk():
        part_filename = part.get_filename()
        if not part_filename and part.get_content_disposition() != "attachment":
            continue
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            continue
        part_content_type = part.get_content_type()
        if part_filename == filename:
            return payload, part_content_type
        candidates.append((payload, part_content_type, part_filename))

    try:
        expected_size = int(size_bytes)
    except (TypeError, ValueError):
        expected_size = None

    for payload, part_content_type, _part_filename in candidates:
        if expected_size is not None and len(payload) != expected_size:
            continue
        if mime_type and part_content_type != mime_type:
            continue
        return payload, part_content_type

    if len(candidates) == 1:
        payload, part_content_type, _part_filename = candidates[0]
        return payload, part_content_type
    return None


def content_disposition(filename: str, disposition_type: str = "inline") -> str:
    ascii_filename = re.sub(r"[^A-Za-z0-9._-]+", "_", filename).strip("_") or "attachment"
    return f"{disposition_type}; filename=\"{ascii_filename}\"; filename*=UTF-8''{quote(filename.encode('utf-8'))}"


def is_active_attachment(filename: str, mime_type: str) -> bool:
    filename_lower = filename.lower()
    mime_type_lower = mime_type.split(";", 1)[0].strip().lower()
    return (
        Path(filename_lower).suffix in ACTIVE_ATTACHMENT_EXTENSIONS
        or mime_type_lower in ACTIVE_ATTACHMENT_MIME_TYPES
        or mime_type_lower.endswith("+xml")
    )


def normalized_mime_type(filename: str, declared_mime_type: str = "", content: bytes | None = None) -> str:
    filename_lower = filename.lower()
    declared = declared_mime_type.split(";", 1)[0].strip().lower()
    guessed = (mimetypes.guess_type(filename)[0] or "").lower()

    if content and content.lstrip().startswith(b"%PDF-"):
        return "application/pdf"
    if filename_lower.endswith(".pdf"):
        return "application/pdf"
    if guessed and declared in {"", "application/octet-stream", "binary/octet-stream", "text/plain"}:
        return guessed
    return declared or guessed or "application/octet-stream"


def normalize_hit(hit: dict[str, Any], terms: list[str]) -> dict[str, Any]:
    body = re.sub(r"\s+", " ", str(hit.get("body") or "")).strip()
    attachments = hit.get("attachments")
    formatted = hit.get("_formatted") if isinstance(hit.get("_formatted"), dict) else {}
    formatted_body = str(formatted.get("body") or "") if isinstance(formatted, dict) else ""
    formatted_subject = str(formatted.get("subject") or "") if isinstance(formatted, dict) else ""
    if formatted_body and "<em>" in formatted_body:
        snippet = text_snippet(strip_highlight(formatted_body), terms)
        snippet_html = safe_highlight_html(text_snippet(formatted_body, terms))
    elif formatted_subject:
        snippet = strip_highlight(formatted_subject)
        snippet_html = safe_highlight_html(formatted_subject)
    else:
        snippet = text_snippet(body, terms)
        snippet_html = highlighted_snippet(body, terms)
    return {
        "id": str(hit.get("id") or ""),
        "userEmail": str(hit.get("userEmail") or ""),
        "from": str(hit.get("from") or ""),
        "to": format_address_list(hit.get("to")),
        "cc": format_address_list(hit.get("cc")),
        "bcc": format_address_list(hit.get("bcc")),
        "subject": str(hit.get("subject") or t("noSubject")),
        "subjectHtml": safe_highlight_html(formatted_subject) if formatted_subject else "",
        "body": body,
        "bodyPreview": body[:320],
        "snippet": snippet,
        "snippetHtml": snippet_html,
        "attachmentCount": len(attachments) if isinstance(attachments, list) else 0,
        "timestamp": hit.get("timestamp"),
        "date": timestamp_to_iso(hit.get("timestamp")),
    }


def attachment_count(value: Any, has_attachments: Any = None) -> int:
    if isinstance(value, list):
        return len(value)
    return 1 if has_attachments is True else 0


def normalize_attachments(message_id: str, value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []

    attachments: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        attachment_id = str(item.get("id") or "").strip()
        filename = str(item.get("filename") or "attachment").strip()
        mime_type = normalized_mime_type(filename, str(item.get("mimeType") or ""))
        storage_path = str(item.get("storagePath") or "").strip()
        if not attachment_id or not storage_path:
            continue
        attachments.append(
            {
                "id": attachment_id,
                "filename": filename,
                "mimeType": mime_type,
                "sizeBytes": item.get("sizeBytes"),
                "viewUrl": f"/api/mail/{message_id}/attachments/{attachment_id}",
            }
        )
    return attachments


def normalize_mail_detail(message: dict[str, Any]) -> dict[str, Any]:
    sender_name = str(message.get("senderName") or "").strip()
    sender_email = str(message.get("senderEmail") or "").strip()
    if sender_name and sender_email:
        sender = f"{sender_name} <{sender_email}>"
    else:
        sender = sender_email or sender_name or str(message.get("from") or "")

    raw_content = extract_raw_content(message.get("raw"))
    body = raw_content["text"]
    sent_at = message.get("sentAt")
    timestamp = iso_to_timestamp(sent_at)
    message_id = str(message.get("id") or "")
    attachments = message.get("attachments")
    normalized_attachments = normalize_attachments(message_id, attachments)

    return {
        "id": message_id,
        "userEmail": str(message.get("userEmail") or ""),
        "from": sender,
        "to": format_address_list(message.get("recipients") or message.get("to")),
        "cc": format_address_list(message.get("cc")),
        "bcc": format_address_list(message.get("bcc")),
        "subject": str(message.get("subject") or t("noSubject")),
        "body": body,
        "bodyHtml": raw_content["html"],
        "bodyPreview": re.sub(r"\s+", " ", body).strip()[:320],
        "attachmentCount": attachment_count(attachments, message.get("hasAttachments")),
        "attachments": normalized_attachments,
        "timestamp": timestamp,
        "date": str(sent_at) if sent_at else None,
        "archivedAt": str(message.get("archivedAt") or ""),
        "messageIdHeader": str(message.get("messageIdHeader") or ""),
        "storagePath": str(message.get("storagePath") or ""),
    }


def openarchiver_headers(settings: Settings) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "X-API-KEY": settings.api_key,
    }


def openarchiver_session() -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    return session


def timestamp_sort_key(item: dict[str, Any]) -> tuple[int, int]:
    try:
        return (0, int(item.get("timestamp")))
    except (TypeError, ValueError):
        return (1, 0)


@app.get("/")
def index(request: Request) -> Response:
    auth_token = request.cookies.get("auth_token")
    if not is_valid_auth_token(auth_token):
        return RedirectResponse(url="/login")
    
    content = render_static_template("index.html", t("indexPageTitle", title=APP_TITLE))
    if not NEED_LOG_IN:
        content = re.sub(
            r'\s*<button id="logout-button" type="button" class="logout-button">.*?</button>',
            "",
            content,
            count=1,
            flags=re.DOTALL,
        )
    if not SETTINGS.enable_exact_filter:
        content = re.sub(
            r'\s*<label class="exact-control".*?</label>',
            "",
            content,
            count=1,
            flags=re.DOTALL,
        )
    search_max_limit = SETTINGS.search_max_limit
    default_limit = min(30, search_max_limit)
    content = content.replace(
        'id="limit-input" name="limit" type="range" min="10" max="100" value="30"',
        f'id="limit-input" name="limit" type="range" min="{MIN_SEARCH_LIMIT}" max="{search_max_limit}" value="{default_limit}"',
    )
    content = content.replace('id="limit-value">30', f'id="limit-value">{default_limit}')
    content = content.replace('id="limit-max-value" class="limit-bound">100', f'id="limit-max-value" class="limit-bound">{search_max_limit}')
    
    return Response(content=content, media_type="text/html")


@app.get("/mail/{message_id}")
def mail_view(request: Request, message_id: UUID) -> Response:
    auth_token = request.cookies.get("auth_token")
    if not is_valid_auth_token(auth_token):
        return RedirectResponse(url="/login")
    content = render_static_template("mail.html", t("mailViewTitle"))
    return Response(content=content, media_type="text/html")


@app.get("/api/search", dependencies=[Depends(verify_api_auth)])
def search_emails(
    q: str = Query(""),
    limit: int = Query(30, ge=MIN_SEARCH_LIMIT),
    sort: str = Query("relevance", pattern="^(relevance|asc|desc)$"),
    exact: bool = Query(False),
    sender: str | None = Query(None, max_length=320),
    date_from: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: str | None = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    has_attachment: bool | None = Query(None),
) -> dict[str, Any]:
    settings = SETTINGS
    limit = min(limit, settings.search_max_limit)
    exact = exact and settings.enable_exact_filter

    # A query that is itself one quoted phrase ("be quite") is sent to OpenArchiver's
    # Meilisearch as a single phrase token, which only matches the words adjacent and in
    # that exact order -- confirmed against the live index: "Rechnung Auftragsnummer" found
    # 2, the reversed word order found 0. Takes priority over the exact-match checkbox,
    # since a phrase is inherently more specific than per-word exact matching.
    phrase = phrase_query(q)
    if phrase:
        keywords = f'"{phrase}"'
    else:
        keywords = quote_keywords(q) if exact else loose_keywords(q)
    terms = search_terms(q)
    has_filter = bool(sender and sender.strip()) or bool(date_from) or bool(date_to) or has_attachment is not None
    if not keywords and not has_filter:
        # Outlook/eM Client allow a sender- or date-only browse with no search term at all --
        # OpenArchiver's own API already accepts an empty `keywords` param fine (confirmed
        # directly against it: from=sonja@... with keywords="" returned real hits). Only block
        # the genuinely empty case where neither a term nor any filter was given.
        raise HTTPException(status_code=400, detail=t("enterSearchTerm"))

    params = {
        "keywords": keywords,
        "page": "1",
        "limit": str(limit),
        "matchingStrategy": settings.search_matching_strategy,
    }
    # Erweiterte Filter (Outlook/eM-Client-Stil: Absender, Zeitraum, Anhang) -- reichen 1:1
    # an OpenArchivers eigene Parameter durch, die koennen das schon laenger, unsere UI
    # hat es bisher nur nie angeboten.
    if sender and sender.strip():
        params["from"] = sender.strip()
    if date_from:
        params["dateFrom"] = date_from
    if date_to:
        params["dateTo"] = date_to
    if has_attachment is not None:
        params["hasAttachments"] = "true" if has_attachment else "false"

    session = openarchiver_session()

    try:
        response = session.get(
            settings.search_url,
            params=params,
            headers=openarchiver_headers(settings),
            timeout=30,
        )
        response.raise_for_status()
    except requests.HTTPError as exc:
        status_code = exc.response.status_code if exc.response is not None else 502
        raise HTTPException(status_code=status_code, detail=t("searchOpenArchiverFailed")) from exc
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=t("openArchiverConnectionFailed", error=str(exc))) from exc

    payload = response.json()
    hits = payload.get("hits", [])
    if not isinstance(hits, list):
        raise HTTPException(status_code=502, detail=t("invalidSearchResponse"))

    items = [normalize_hit(hit, terms) for hit in hits if isinstance(hit, dict)]
    if sort in {"asc", "desc"}:
        items.sort(key=timestamp_sort_key, reverse=(sort == "desc"))

    return {
        "items": items,
        "total": payload.get("total", len(items)),
        "page": payload.get("page", 1),
        "limit": payload.get("limit", limit),
        "maxLimit": settings.search_max_limit,
        "totalPages": payload.get("totalPages"),
        "processingTimeMs": payload.get("processingTimeMs"),
        "query": q,
        "keywords": keywords,
        "sort": sort,
        "exact": exact,
    }


def fetch_mail_payload(settings: Settings, message_id: UUID) -> dict[str, Any]:
    session = openarchiver_session()
    url = f"{settings.mail_get_url}{message_id}"

    try:
        response = session.get(url, headers=openarchiver_headers(settings), timeout=30)
        response.raise_for_status()
    except requests.HTTPError as exc:
        status_code = exc.response.status_code if exc.response is not None else 502
        raise HTTPException(status_code=status_code, detail=t("mailOpenArchiverFailed")) from exc
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=t("openArchiverConnectionFailed", error=str(exc))) from exc

    payload = response.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=502, detail=t("invalidMailResponse"))
    return payload


from functools import lru_cache

@lru_cache(maxsize=200)
def _cached_get_mail_detail(message_id: UUID) -> dict[str, Any]:
    settings = SETTINGS
    payload = fetch_mail_payload(settings, message_id)
    return normalize_mail_detail(payload)

@app.get("/api/mail/{message_id}", dependencies=[Depends(verify_api_auth)])
def get_mail(message_id: UUID) -> dict[str, Any]:
    return {
        "item": _cached_get_mail_detail(message_id),
        "mailViewUrl": f"/mail/{message_id}",
    }


@app.get("/api/mail/{message_id}/eml", dependencies=[Depends(verify_api_auth)])
def download_eml(message_id: UUID) -> Response:
    settings = SETTINGS
    payload = fetch_mail_payload(settings, message_id)
    
    storage_path = str(payload.get("storagePath") or "").strip()
    response_content = None

    if storage_path:
        session = openarchiver_session()
        try:
            response = session.get(
                settings.storage_download_url,
                params={"path": storage_path},
                headers=openarchiver_headers(settings),
                timeout=60,
            )
            response.raise_for_status()
            response_content = response.content
        except requests.RequestException:
            pass

    if not response_content:
        response_content = raw_bytes(payload.get("raw"))
        
    if not response_content:
        raise HTTPException(status_code=404, detail=t("missingEmlData"))

    subject = str(payload.get("subject") or "").strip()
    safe_subject = re.sub(r'[\\/*?:"<>|]', "", subject).strip()
    filename = f"{safe_subject}.eml" if safe_subject else f"{message_id}.eml"
    
    ascii_filename = re.sub(r"[^A-Za-z0-9._-]+", "_", filename).strip("_") or "mail.eml"
    encoded_filename = quote(filename.encode("utf-8"))

    headers = {
        "Content-Disposition": f'attachment; filename="{ascii_filename}"; filename*=UTF-8\'\'{encoded_filename}',
        "X-Content-Type-Options": "nosniff",
    }
    return Response(content=response_content, media_type="message/rfc822", headers=headers)


@lru_cache(maxsize=200)
def _cached_get_attachment_data(message_id: UUID, attachment_id: str) -> tuple[bytes, str, str]:
    settings = SETTINGS
    payload = fetch_mail_payload(settings, message_id)
    attachments = payload.get("attachments")
    if not isinstance(attachments, list):
        raise HTTPException(status_code=404, detail=t("noAttachments"))

    attachment = next(
        (
            item
            for item in attachments
            if isinstance(item, dict) and str(item.get("id") or "") == attachment_id
        ),
        None,
    )
    if attachment is None:
        raise HTTPException(status_code=404, detail=t("attachmentNotFound"))

    storage_path = str(attachment.get("storagePath") or "").strip()
    if not storage_path:
        raise HTTPException(status_code=404, detail=t("missingAttachmentStoragePath"))

    session = openarchiver_session()
    try:
        response = session.get(
            settings.storage_download_url,
            params={"path": storage_path},
            headers=openarchiver_headers(settings),
            timeout=60,
        )
        response.raise_for_status()
    except requests.HTTPError as exc:
        fallback = raw_attachment(
            payload.get("raw"),
            str(attachment.get("filename") or ""),
            str(attachment.get("mimeType") or ""),
            attachment.get("sizeBytes"),
        )
        if fallback is None:
            status_code = exc.response.status_code if exc.response is not None else 502
            raise HTTPException(status_code=status_code, detail=t("attachmentDownloadFailed")) from exc
        content, fallback_mime_type = fallback
        response_content = content
        response_mime_type = fallback_mime_type
    except requests.RequestException as exc:
        fallback = raw_attachment(
            payload.get("raw"),
            str(attachment.get("filename") or ""),
            str(attachment.get("mimeType") or ""),
            attachment.get("sizeBytes"),
        )
        if fallback is None:
            raise HTTPException(status_code=502, detail=t("openArchiverConnectionFailed", error=str(exc))) from exc
        content, fallback_mime_type = fallback
        response_content = content
        response_mime_type = fallback_mime_type
    else:
        response_content = response.content
        response_mime_type = str(response.headers.get("content-type") or "")

    filename = str(attachment.get("filename") or "attachment").strip().replace('"', "'")
    mime_type = normalized_mime_type(
        filename,
        str(attachment.get("mimeType") or response_mime_type or ""),
        response_content,
    )
    return response_content, mime_type, filename


@app.get("/api/mail/{message_id}/attachments/{attachment_id}", dependencies=[Depends(verify_api_auth)])
def get_attachment(message_id: UUID, attachment_id: str) -> Response:
    response_content, mime_type, filename = _cached_get_attachment_data(message_id, attachment_id)
    is_active_content = is_active_attachment(filename, mime_type)
    headers = {
        "Content-Disposition": content_disposition(filename, "attachment" if is_active_content else "inline"),
        "X-Content-Type-Options": "nosniff",
    }
    safe_mime_type = "application/octet-stream" if is_active_content else mime_type
    return Response(content=response_content, media_type=safe_mime_type, headers=headers)
