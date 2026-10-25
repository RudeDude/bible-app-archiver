"""Local search page for the catalog. It listens on localhost only by default."""

from __future__ import annotations

import json
import re
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from bible_archive.normalize import parse_terms
from bible_archive.store import Catalog, date_window

INDEX_PATH = Path(__file__).resolve().parent / "web" / "index.html"
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MONTH = re.compile(r"^\d{4}-\d{2}$")
_KIND = re.compile(r"^[a-z0-9_]{1,40}$")


class CatalogServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], database: Path):
        self.database = database
        super().__init__(address, CatalogHandler)


class CatalogHandler(BaseHTTPRequestHandler):
    server: CatalogServer

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/":
            self._send(200, "text/html; charset=utf-8", INDEX_PATH.read_bytes())
            return
        if parsed.path == "/api/search":
            self._search(parse_qs(parsed.query))
            return
        if parsed.path == "/api/facets":
            self._facets(parse_qs(parsed.query))
            return
        self._send(404, "application/json; charset=utf-8", _json({"error": "not found"}))

    def log_message(self, fmt: str, *args) -> None:
        return

    def _search(self, query: dict[str, list[str]]) -> None:
        try:
            filters = _filters(query)
            limit = _int(query, "limit", 40, maximum=100)
            offset = _int(query, "offset", 0, maximum=100_000)
        except ValueError as error:
            self._send(400, "application/json; charset=utf-8", _json({"error": str(error)}))
            return
        catalog = Catalog(self.server.database)
        try:
            result = catalog.search(limit=limit, offset=offset, **filters)
        finally:
            catalog.close()
        result["terms"] = filters["terms"]
        result["limit"] = limit
        result["offset"] = offset
        self._send(200, "application/json; charset=utf-8", _json(result))

    def _facets(self, query: dict[str, list[str]]) -> None:
        try:
            filters = _filters(query)
        except ValueError as error:
            self._send(400, "application/json; charset=utf-8", _json({"error": str(error)}))
            return
        catalog = Catalog(self.server.database)
        try:
            result = catalog.facets(
                terms=filters["terms"],
                kinds=filters["kinds"],
                start=filters["start"],
                end=filters["end"],
                version_id=filters["version_id"],
            )
        finally:
            catalog.close()
        self._send(200, "application/json; charset=utf-8", _json(result))

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def serve(database: Path, host: str = "127.0.0.1", port: int = 8765) -> None:
    server = CatalogServer((host, port), database)
    if host not in {"127.0.0.1", "localhost", "::1"}:
        print(f"Warning: {host} is not only this computer. The catalog contains private notes.")
    print(f"Catalog at http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()


def _filters(query: dict[str, list[str]]) -> dict:
    text = _one(query, "q")
    month = _one(query, "month")
    start = _one(query, "from")
    end = _one(query, "to")
    if month and not _MONTH.match(month):
        raise ValueError("month must be YYYY-MM")
    for label, value in (("from", start), ("to", end)):
        if value and not _valid_day(value):
            raise ValueError(f"{label} must be YYYY-MM-DD")
    if month:
        try:
            start, end = date_window("", "", month)
        except ValueError as error:
            raise ValueError("month must be YYYY-MM") from error
    else:
        start, end = date_window(start, end, "")
    kinds = [kind for kind in _many(query, "kind") if kind and _KIND.match(kind)]
    version = _one(query, "version")
    version_id = int(version) if version.isdigit() else None
    sort = _one(query, "sort") or "newest"
    if sort not in {"newest", "oldest"}:
        raise ValueError("sort must be newest or oldest")
    return {
        "terms": parse_terms(text),
        "kinds": kinds or None,
        "start": start,
        "end": end,
        "version_id": version_id,
        "sort": sort,
    }


def _valid_day(value: str) -> bool:
    if not _DAY.match(value):
        return False
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def _one(query: dict[str, list[str]], name: str) -> str:
    values = query.get(name) or []
    return values[0].strip() if values else ""


def _many(query: dict[str, list[str]], name: str) -> list[str]:
    values: list[str] = []
    for value in query.get(name) or []:
        values.extend(part.strip() for part in value.split(","))
    return values


def _int(query: dict[str, list[str]], name: str, default: int, maximum: int) -> int:
    raw = _one(query, name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be a number") from error
    if value < 0 or value > maximum:
        raise ValueError(f"{name} is out of range")
    return value


def _json(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")
