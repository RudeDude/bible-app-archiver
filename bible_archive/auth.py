"""Use the session www.bible.com already created. Read only.

YouVersion rejects the old password grant for this app. The password is never
sent. A signed-in browser session is read from
https://www.bible.com/api/authenticate and reused as a bearer token.
"""

from __future__ import annotations

import base64
import json
import os
import stat
import time
from pathlib import Path
from typing import Any

# Kept so a bearer token is not attached if a request is aimed at the old token URL.
AUTH_URL = "https://auth.youversionapi.com/token"
AUTHENTICATE_URL = "https://www.bible.com/api/authenticate"

SIGN_IN_HELP = """\
YouVersion refused this app's password sign-in. The server said the client is
not authorized to request a token that way. That is not a wrong password.

Sign in at https://www.bible.com. Then copy the yva_session cookie so later
syncs can refresh the access token:

1. Open Developer Tools, then Application (Chrome) or Storage (Firefox).
2. Choose Cookies, then https://www.bible.com.
3. Copy the value of yva_session.

Paste it as yva_session=VALUE. The cookie stays in data/session.json. Sync
calls /api/authenticate with it when the access token is near expiry. You can
also set YOUVERSION_SESSION_COOKIE. You can remove YOUVERSION_PASSWORD.

A one-time token also works. In the Console on bible.com, paste:

copy(JSON.stringify(await (await fetch("/api/authenticate", {cache:"no-store"})).json()))
"""

# Renew a little early so a sync does not start with a token that expires mid-run.
REFRESH_SKEW_SECONDS = 600
Transport = Any


class AuthError(Exception):
    """The account session could not be read, or the token was rejected."""


def app_headers() -> dict[str, str]:
    """Match the headers bible.com sends with a session from /api/authenticate."""
    return {
        "Accept": "application/json",
        "Referer": "https://web.youversionapi.com",
        "X-YouVersion-App-Platform": os.environ.get("YOUVERSION_APP_PLATFORM", "web"),
        "X-YouVersion-App-Version": os.environ.get("YOUVERSION_APP_VERSION", "3"),
        "X-YouVersion-Client": "youversion",
        "User-Agent": os.environ.get("YOUVERSION_USER_AGENT", "bible-app-archiver/0.1"),
    }


def jwt_user_id(token: str) -> str | None:
    """Read the user id from a token payload. The signature is not checked."""
    try:
        part = token.split(".")[1]
        padding = "=" * (-len(part) % 4)
        payload = json.loads(base64.urlsafe_b64decode(part + padding))
    except (IndexError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return _first_id(payload)


def load_session(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(data, dict) and data.get("access_token"):
        return data
    return None


def save_session(
    path: Path,
    token: str,
    user_id: str | None,
    session_cookie: str | None = None,
) -> None:
    existing = load_session(path) or {}
    cookie = session_cookie or existing.get("session_cookie") or ""
    payload: dict[str, Any] = {"access_token": token, "user_id": user_id}
    expires_at = jwt_expiry(token)
    if expires_at is not None:
        payload["expires_at"] = expires_at
    if cookie:
        payload["session_cookie"] = cookie
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def parse_session_text(text: str) -> tuple[str, str | None]:
    """Accept a raw token or the JSON from bible.com /api/authenticate.

    The saved session keeps the token and user id only.
    """
    raw = text.strip().strip("'\"")
    if not raw:
        raise AuthError("No session token was provided.")
    explicit_id: str | None = None
    if raw.startswith("{"):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as error:
            raise AuthError("That text looks like JSON, but it could not be read.") from error
        if not isinstance(payload, dict):
            raise AuthError("The session JSON must be an object.")
        token = payload.get("accessToken") or payload.get("access_token")
        user = payload.get("user")
        if isinstance(user, dict):
            explicit_id = _first_id(user)
        if not token:
            raise AuthError(
                "That response has no accessToken. Sign in at https://www.bible.com, "
                "then fetch /api/authenticate again."
            )
        raw = str(token).strip()
    if raw.lower().startswith("bearer "):
        raw = raw[7:].strip()
    if "=" in raw and " " not in raw.split("=", 1)[0]:
        name, value = raw.split("=", 1)
        if name.strip() == "yva_session":
            raise AuthError(
                "That is the yva_session cookie, not the access token. "
                "Login exchanges it at https://www.bible.com/api/authenticate."
            )
        if name.strip() == "yva":
            raw = value.strip().strip("\"'")
    if len(raw) < 20 or any(character.isspace() for character in raw):
        raise AuthError("That does not look like a session token.")
    return raw, _prefer_user_id(explicit_id, raw)


def jwt_expiry(token: str) -> int | None:
    """Return the token exp claim in unix seconds, without checking the signature."""
    payload = _jwt_payload(token)
    if not isinstance(payload, dict):
        return None
    expiry = payload.get("exp")
    if isinstance(expiry, (int, float)):
        return int(expiry)
    return None


def access_token_needs_refresh(token: str, now: float | None = None) -> bool:
    """True when the token is missing, expired, or inside the renewal window."""
    if not token:
        return True
    expiry = jwt_expiry(token)
    if expiry is None:
        return False
    current = time.time() if now is None else now
    return expiry <= current + REFRESH_SKEW_SECONDS


def is_session_cookie_text(text: str) -> bool:
    raw = text.strip()
    lowered = raw.lower()
    return "yva_session=" in lowered or lowered.startswith("cookie:")


def parse_session_cookie(text: str) -> str:
    """Return the yva_session value from a cookie pair or a Cookie header."""
    raw = text.strip().strip("'\"")
    if raw.lower().startswith("cookie:"):
        raw = raw.split(":", 1)[1].strip()
    if "yva_session=" not in raw:
        raise AuthError("Paste the bible.com cookie as yva_session=VALUE.")
    for part in raw.split(";"):
        if "=" not in part:
            continue
        name, value = part.strip().split("=", 1)
        if name.strip() != "yva_session":
            continue
        return _clean_cookie_value(value)
    raise AuthError("The yva_session cookie value is empty.")


def exchange_session_cookie(cookie: str, transport: Transport) -> tuple[str, str | None, str]:
    """Ask bible.com for a new access token. GET only; PATCH on this URL signs out."""
    cookie = _clean_cookie_value(cookie)
    url = f"{AUTHENTICATE_URL}?t={int(time.time() * 1000)}"
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Cookie": f"yva_session={cookie}",
        "User-Agent": os.environ.get("YOUVERSION_USER_AGENT", "bible-app-archiver/0.1"),
    }
    try:
        status, response_headers, raw = transport("GET", url, headers, None)
    except OSError as error:
        raise AuthError("Could not reach bible.com to refresh the session.") from error
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
        payload = {}
    has_token = isinstance(payload, dict) and (payload.get("accessToken") or payload.get("access_token"))
    if status != 200 or not has_token:
        message = ""
        if isinstance(payload, dict):
            message = str(payload.get("reason") or payload.get("error") or "")
        if not message:
            message = f"HTTP {status}"
        raise AuthError(
            "bible.com rejected the yva_session cookie "
            f"({_redact(message, cookie)}). Sign in at https://www.bible.com and copy the cookie again."
        )
    token, user_id = parse_session_text(json.dumps(payload))
    rotated = _cookie_from_set_cookie(response_headers) or cookie
    return token, user_id, rotated


def _jwt_payload(token: str) -> dict[str, Any] | None:
    try:
        part = token.split(".")[1]
        padding = "=" * (-len(part) % 4)
        payload = json.loads(base64.urlsafe_b64decode(part + padding))
    except (IndexError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _clean_cookie_value(value: str) -> str:
    cleaned = value.strip().strip("'\"")
    if not cleaned or any(character in cleaned for character in "\r\n; "):
        raise AuthError("The yva_session cookie value is not usable.")
    return cleaned


def _cookie_from_set_cookie(headers: dict[str, str] | None) -> str | None:
    if not headers:
        return None
    raw = ""
    for key, value in headers.items():
        if key.lower() == "set-cookie":
            raw = value
            break
    marker = "yva_session="
    start = raw.find(marker)
    if start < 0:
        return None
    value = raw[start + len(marker):].split(";", 1)[0].strip()
    try:
        return _clean_cookie_value(value)
    except AuthError:
        return None


def _redact(text: str, secret: str) -> str:
    cleaned = text.replace(secret, "[redacted]") if secret else text
    return " ".join(cleaned.split())[:300]


def _prefer_user_id(explicit: str | None, token: str) -> str | None:
    from_token = jwt_user_id(token)
    if explicit and explicit.isdigit():
        return explicit
    if from_token and from_token.isdigit():
        return from_token
    return explicit or from_token


def _first_id(payload: dict[str, Any]) -> str | None:
    for key in ("id", "user_id", "userId", "sub"):
        value = payload.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None
