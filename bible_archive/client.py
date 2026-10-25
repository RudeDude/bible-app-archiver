"""Read-only client for the signed-in user's highlights, notes, and saved events."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlencode

from bible_archive.auth import AUTH_URL, AuthError, app_headers

BIBLE_API = "https://bible.youversionapi.com"
EVENTS_API = "https://events.youversionapi.com"
MOMENTS_API = "https://moments.youversionapi.com"

Transport = Callable[[str, str, dict[str, str], bytes | None], tuple[int, dict[str, str], bytes]]
Sleeper = Callable[[float], None]

_TOKEN_PATTERN = re.compile(r"(access_token[\"']?\s*[:=]\s*[\"']?)[A-Za-z0-9._\-]+", re.I)


class ApiError(Exception):
    def __init__(self, status: int, message: str, url: str):
        super().__init__(f"HTTP {status} for {url}: {message}")
        self.status = status
        self.url = url


class YouVersionClient:
    """Sequential, throttled reader. It has no methods that change the account."""

    def __init__(
        self,
        transport: Transport,
        *,
        token: str,
        user_id: str | None = None,
        delay: float = 1.5,
        sleep: Sleeper = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        login: Callable[[], tuple[str, str | None]] | None = None,
    ):
        self.transport = transport
        self.token = token
        self.user_id = user_id
        self.delay = max(0.0, delay)
        self.sleep = sleep
        self.clock = clock
        self.login = login
        self._next_request = 0.0
        self.requests = 0

    def moments_page(self, kind: str, page: int) -> Any:
        params: dict[str, Any] = {"page": page}
        if kind and kind != "client_side":
            params["kind"] = kind
        if self.user_id:
            params["user_id"] = self.user_id
        path = "/3.1/client_side_items.json" if kind == "client_side" else "/3.1/items.json"
        return self.get(MOMENTS_API + path, params)

    def labels(self) -> Any:
        return self.get(MOMENTS_API + "/3.1/labels.json", {})

    def saved_events_page(self, page: int) -> Any:
        return self.get(EVENTS_API + "/3.2/saved_items.json", {"page": page})

    def event_view(self, event_id: str) -> Any:
        return self.get(EVENTS_API + "/3.2/view.json", {"id": event_id})

    def bible_version(self, version_id: int) -> Any:
        return self.get(BIBLE_API + "/3.1/version.json", {"id": version_id})

    def get(self, url: str, params: dict[str, Any]) -> Any:
        query = urlencode({key: value for key, value in params.items() if value is not None})
        full = f"{url}?{query}" if query else url
        return self._request("GET", full, None)

    def _request(self, method: str, url: str, body: bytes | None, auth_retry: bool = True) -> Any:
        attempt = 0
        while True:
            self._wait()
            headers = app_headers()
            if self.token and url != AUTH_URL:
                headers["Authorization"] = f"Bearer {self.token}"
            try:
                status, response_headers, raw = self.transport(method, url, headers, body)
            except OSError as error:
                attempt += 1
                if attempt >= 5:
                    raise ApiError(0, public_message(str(error)), _safe_url(url)) from error
                self._pause(min(60.0, 2.0 ** attempt))
                continue
            self.requests += 1
            if status == 401 and auth_retry and self.login and url != AUTH_URL:
                token, user_id = self.login()
                self.token = token
                self.user_id = user_id or self.user_id
                return self._request(method, url, body, auth_retry=False)
            if status == 429 or status in {500, 502, 503, 504}:
                attempt += 1
                if attempt >= 6:
                    raise ApiError(status, public_message(_body_text(raw)), _safe_url(url))
                self._pause(_retry_delay(status, response_headers, attempt))
                continue
            if status < 200 or status >= 300:
                raise ApiError(status, public_message(_body_text(raw)), _safe_url(url))
            if not raw:
                return {}
            try:
                return json.loads(raw.decode("utf-8-sig"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ApiError(status, "response was not JSON", _safe_url(url)) from error

    def _wait(self) -> None:
        now = self.clock()
        if now < self._next_request:
            self.sleep(self._next_request - now)
        self._next_request = self.clock() + self.delay

    def _pause(self, seconds: float) -> None:
        if seconds > 0:
            self.sleep(seconds)
        self._next_request = self.clock() + self.delay


def public_message(text: str) -> str:
    cleaned = _TOKEN_PATTERN.sub(r"\1[redacted]", text or "")
    cleaned = " ".join(cleaned.split())
    return cleaned[:400]


def _body_text(raw: bytes) -> str:
    try:
        return raw.decode("utf-8", errors="replace")
    except Exception:
        return ""


def _safe_url(url: str) -> str:
    return url.split("?", 1)[0]


def _retry_delay(status: int, headers: dict[str, str], attempt: int) -> float:
    if status == 429:
        retry_after = headers.get("retry-after") or headers.get("Retry-After") or ""
        try:
            return min(120.0, max(1.0, float(retry_after)))
        except ValueError:
            return min(120.0, 5.0 * attempt)
    return min(60.0, 2.0 ** attempt)


def urllib_transport(timeout: float = 45.0) -> Transport:
    import urllib.error
    import urllib.request

    def send(method: str, url: str, headers: dict[str, str], body: bytes | None):
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                response_headers = {key.lower(): value for key, value in response.headers.items()}
                return response.status, response_headers, response.read()
        except urllib.error.HTTPError as error:
            response_headers = {key.lower(): value for key, value in error.headers.items()}
            return error.code, response_headers, error.read()

    return send


__all__ = ["ApiError", "AuthError", "YouVersionClient", "urllib_transport"]
