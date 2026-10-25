"""Download the signed-in account slowly, then rebuild the local catalog."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable

from bible_archive.client import ApiError, YouVersionClient
from bible_archive.normalize import (
    extract_items,
    label_map,
    normalize_event,
    normalize_moment,
    payload_keys,
    version_fields,
)
from bible_archive.store import Catalog

MOMENT_KINDS = ("highlight", "note", "bookmark", "client_side")
REQUIRED_KINDS = {"highlight", "note"}
Log = Callable[[str], None]


def sync_account(
    client: YouVersionClient,
    root: Path,
    *,
    kinds: list[str],
    max_pages: int = 0,
    redownload: bool = False,
    log: Log = print,
) -> dict[str, Any]:
    """Save raw responses under root/raw, then rebuild root/catalog.sqlite."""
    raw_root = root / "raw"
    raw_root.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    downloaded: dict[str, int] = {}

    try:
        if any(kind in kinds for kind in MOMENT_KINDS):
            _best_effort(
                "labels",
                errors,
                log,
                lambda: _save(raw_root / "labels.json", client.labels()),
            )
        for kind in kinds:
            if kind in MOMENT_KINDS:
                _run_kind(kind, client, raw_root, max_pages, redownload, errors, downloaded, log)
            elif kind == "event":
                _best_effort(
                    "events",
                    errors,
                    log,
                    lambda: downloaded.__setitem__(
                        "event",
                        _sync_events(client, raw_root / "events", max_pages, redownload, log),
                    ),
                )
            else:
                errors.append(f"unknown kind {kind}")
    except Exception:
        rebuild_catalog(root)
        raise

    names = _load_labels(raw_root / "labels.json")
    catalog = Catalog(root / "catalog.sqlite")
    try:
        stats = catalog.replace_items(load_raw_items(raw_root, names))
        missing = [
            version_id
            for version_id in catalog.version_ids()
            if version_id not in catalog.known_version_ids()
        ]
        for version_id in missing:
            _best_effort(
                f"version {version_id}",
                errors,
                log,
                lambda version_id=version_id: _save_version(client, catalog, raw_root, version_id),
            )
        counts = catalog.counts()
    finally:
        catalog.close()
    log(
        "catalog: "
        + (", ".join(f"{count} {kind}" for kind, count in sorted(counts.items())) or "empty")
    )
    return {"downloaded": downloaded, "catalog": stats, "counts": counts, "errors": errors}


def rebuild_catalog(root: Path) -> dict[str, int]:
    raw_root = root / "raw"
    names = _load_labels(raw_root / "labels.json")
    catalog = Catalog(root / "catalog.sqlite")
    try:
        return catalog.replace_items(load_raw_items(raw_root, names))
    finally:
        catalog.close()


def load_raw_items(raw_root: Path, label_names: dict[str, str] | None = None):
    names = label_names or {}
    moments = raw_root / "moments"
    if moments.exists():
        for kind_dir in sorted(path for path in moments.iterdir() if path.is_dir()):
            for page in _page_files(kind_dir):
                for item in extract_items(_read_json(page)):
                    yield normalize_moment(item, kind_dir.name, names)
    event_views: dict[str, dict[str, Any]] = {}
    events = raw_root / "events"
    if events.exists():
        for view in sorted(events.glob("view-*.json")):
            record = _event_record(_read_json(view))
            if record is None:
                continue
            row = normalize_event(record)
            event_views[row["youversion_id"]] = row
        for page in _page_files(events):
            for item in extract_items(_read_json(page)):
                row = normalize_event(item)
                event_views.setdefault(row["youversion_id"], row)
    yield from event_views.values()


def peek(raw_root: Path, log: Log = print) -> None:
    """Print payload shapes. Values such as note text are not printed."""
    if not raw_root.exists():
        log(f"No raw archive at {raw_root}")
        return
    files = sorted(path for path in raw_root.rglob("*.json") if path.is_file())
    if not files:
        log("No raw JSON files yet.")
        return
    for path in files:
        payload = _read_json(path)
        items = extract_items(payload)
        item_keys = list(items[0].keys())[:12] if items else []
        log(
            f"{path.relative_to(raw_root)} items={len(items)} "
            f"keys={payload_keys(payload)[:12]} item_keys={item_keys}"
        )


def _run_kind(kind, client, raw_root, max_pages, redownload, errors, downloaded, log) -> None:
    directory = raw_root / "moments" / kind

    def fetch(page: int):
        return client.moments_page(kind, page)

    action = lambda: downloaded.__setitem__(
        kind, download_pages(directory, fetch, max_pages=max_pages, redownload=redownload, log=log, label=kind)
    )
    if kind in REQUIRED_KINDS:
        action()
    else:
        _best_effort(kind, errors, log, action)


def _sync_events(client, directory: Path, max_pages: int, redownload: bool, log: Log) -> int:
    count = download_pages(
        directory,
        client.saved_events_page,
        max_pages=max_pages,
        redownload=redownload,
        log=log,
        label="events",
    )
    seen: set[str] = set()
    for page in _page_files(directory):
        for item in extract_items(_read_json(page)):
            event_id = _event_id(item)
            if not event_id or event_id in seen:
                continue
            seen.add(event_id)
            path = directory / f"view-{_safe_filename(event_id)}.json"
            if path.exists() and not redownload:
                continue
            try:
                _save(path, client.event_view(event_id))
                log(f"events view {event_id}")
            except ApiError as error:
                _save(path, {"_error": public_event_error(error), "id": event_id})
                log(f"events view {event_id} skipped: {error}")
    return count


def download_pages(
    directory: Path,
    fetch_page,
    *,
    max_pages: int,
    redownload: bool,
    log: Log,
    label: str,
) -> int:
    """Fetch numbered pages. Cached pages are kept so a run can resume."""
    directory.mkdir(parents=True, exist_ok=True)
    last_cached = _highest_page(directory)
    seen: set[tuple[str, ...]] = set()
    page = 1
    count = 0
    while True:
        if max_pages and page > max_pages:
            log(f"{label}: stopped at --max-pages {max_pages}")
            break
        path = directory / f"page-{page:04d}.json"
        use_cache = path.exists() and not redownload and page < last_cached
        if use_cache:
            payload = _read_json(path)
            origin = "cached"
        else:
            try:
                payload = fetch_page(page)
            except ApiError as error:
                # A page past the last one is 404 not_found, not an empty list.
                if _is_end_of_pages(error):
                    log(f"{label} page {page}: end (not found)")
                    break
                raise
            _save(path, payload)
            origin = "fetched"
        items = extract_items(payload)
        signature = tuple(_identity(item) for item in items)
        if not items or signature in seen:
            log(f"{label} page {page}: end ({origin}, {len(items)} items)")
            break
        seen.add(signature)
        count += len(items)
        log(f"{label} page {page}: {len(items)} items ({origin}, running total {count})")
        page += 1
    return count


def _is_end_of_pages(error: ApiError) -> bool:
    """YouVersion uses 404 not_found when a list page does not exist."""
    return error.status == 404 and "not_found" in str(error)


def _save_version(client: YouVersionClient, catalog: Catalog, raw_root: Path, version_id: int) -> None:
    payload = client.bible_version(version_id)
    path = raw_root / "versions" / f"{version_id}.json"
    _save(path, payload)
    abbreviation, title = version_fields(payload)
    catalog.save_version(version_id, abbreviation, title, json.dumps(payload, ensure_ascii=False, default=str))


def _load_labels(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        return label_map(_read_json(path))
    except (OSError, json.JSONDecodeError):
        return {}


def _best_effort(label: str, errors: list[str], log: Log, action) -> None:
    try:
        action()
    except ApiError as error:
        errors.append(f"{label}: {error}")
        log(f"{label} skipped: {error}")


def _page_files(directory: Path) -> list[Path]:
    return sorted(directory.glob("page-*.json"))


def _highest_page(directory: Path) -> int:
    numbers = []
    for path in _page_files(directory):
        try:
            numbers.append(int(path.stem.split("-")[1]))
        except (IndexError, ValueError):
            continue
    return max(numbers) if numbers else 0


def _identity(item: dict[str, Any]) -> str:
    obj = item.get("object") if isinstance(item.get("object"), dict) else item
    for node in (obj, item):
        if isinstance(node, dict):
            for key in ("id", "moment_id", "event_id"):
                if node.get(key) is not None:
                    return f"{key}:{node[key]}"
    return json.dumps(item, sort_keys=True, default=str)


def _event_record(payload: Any) -> dict[str, Any] | None:
    """Use the event object itself. Do not treat its module list as more events."""
    if not isinstance(payload, dict) or payload.get("_error"):
        return None
    node: Any = payload
    response = payload.get("response")
    if isinstance(response, dict) and isinstance(response.get("data"), dict):
        node = response["data"]
    if isinstance(node, dict):
        for key in ("event", "item"):
            if isinstance(node.get(key), dict):
                return node[key]
        if any(key in node for key in ("id", "event_id", "title", "name", "modules")):
            return node
    return None


def _safe_filename(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", value).strip("._")
    return (cleaned or "unknown")[:80]


def public_event_error(error: ApiError) -> str:
    return str(error)[:400]


def _event_id(item: dict[str, Any]) -> str:
    for node in (item, item.get("event"), item.get("object")):
        if isinstance(node, dict):
            for key in ("id", "event_id"):
                if node.get(key) is not None:
                    return str(node[key])
    return ""


def _save(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    temporary.replace(path)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))
