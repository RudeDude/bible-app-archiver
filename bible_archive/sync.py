"""Download the signed-in account slowly, then rebuild the local catalog."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Callable

from bible_archive.client import ApiError, YouVersionClient
from bible_archive.normalize import (
    extract_items,
    label_map,
    moment_version_names,
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
        refresh_version_names(raw_root, catalog)
        for version_id in catalog.unnamed_version_ids():
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
        stats = catalog.replace_items(load_raw_items(raw_root, names))
        refresh_version_names(raw_root, catalog)
        return stats
    finally:
        catalog.close()


def refresh_version_names(raw_root: Path, catalog: Catalog) -> None:
    """Save names from version responses, then abbreviations from the saved moments.

    A version response keeps its title. When a saved moment carries a different
    abbreviation, that short name replaces the one stored before.
    """
    directory = raw_root / "versions"
    if directory.exists():
        for path in sorted(directory.glob("*.json")):
            if not path.stem.isdigit():
                continue
            try:
                payload = _read_json(path)
            except (OSError, json.JSONDecodeError):
                continue
            abbreviation, title = version_fields(payload)
            if not abbreviation and not title:
                continue
            catalog.save_version(
                int(path.stem),
                abbreviation,
                title,
                json.dumps(payload, ensure_ascii=False, default=str),
            )
    catalog.merge_version_names(_names_from_moments(raw_root))


def _names_from_moments(raw_root: Path) -> dict[int, str]:
    counts: dict[int, Counter[str]] = {}
    moments = raw_root / "moments"
    if not moments.exists():
        return {}
    for kind_dir in sorted(path for path in moments.iterdir() if path.is_dir()):
        if not kind_dir.is_dir():
            continue
        for page in _page_files(kind_dir):
            try:
                payload = _read_json(page)
            except (OSError, json.JSONDecodeError):
                continue
            for item in extract_items(payload):
                for version_id, abbreviation in moment_version_names(item).items():
                    counts.setdefault(version_id, Counter())[abbreviation] += 1
    return {
        version_id: counter.most_common(1)[0][0]
        for version_id, counter in counts.items()
        if counter
    }


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
    files = sorted(
        path for path in raw_root.rglob("*.json")
        if path.is_file() and path.name != "list-stamp.json"
    )
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
    """Fetch numbered pages and drop saved pages the account no longer has.

    A finished list is checked at the first and last saved page. When both still
    match, the middle pages are kept. When either differs, the list shifted, so
    every page is downloaded again and a removed highlight is not left behind.
    Pages saved before that check existed are downloaded again once.
    ``--max-pages`` only refreshes the pages it asks for and leaves the rest.
    """
    directory.mkdir(parents=True, exist_ok=True)
    if redownload or max_pages or _highest_page(directory) == 0 or not _list_is_complete(directory):
        return _walk_pages(
            directory, fetch_page, max_pages=max_pages, log=log, label=label, start=1, prefetched={}
        )

    last_cached = _highest_page(directory)
    first_status, first_payload = _probe_page(directory, fetch_page, 1)
    if first_status == "end":
        _delete_pages_after(directory, 0)
        _clear_list_stamp(directory)
        log(f"{label} page 1: end (not found)")
        return 0
    prefetched = {1: first_payload}
    if first_status == "changed":
        log(f"{label}: saved pages changed, downloading the list again")
        return _walk_pages(
            directory, fetch_page, max_pages=max_pages, log=log, label=label, start=1, prefetched=prefetched
        )
    if last_cached == 1:
        if not _has_more_pages(first_payload):
            return _count_saved_items(directory, 1)
        return _walk_pages(
            directory, fetch_page, max_pages=max_pages, log=log, label=label, start=2, prefetched={}
        )

    last_status, last_payload = _probe_page(directory, fetch_page, last_cached)
    if last_status != "same":
        log(f"{label}: saved pages changed, downloading the list again")
        if last_status == "changed":
            prefetched[last_cached] = last_payload
        return _walk_pages(
            directory, fetch_page, max_pages=max_pages, log=log, label=label, start=1, prefetched=prefetched
        )
    if not _has_more_pages(last_payload):
        _delete_pages_after(directory, last_cached)
        return _count_saved_items(directory, last_cached)
    return _walk_pages(
        directory, fetch_page, max_pages=max_pages, log=log, label=label, start=last_cached + 1, prefetched={}
    )


def _walk_pages(
    directory: Path,
    fetch_page,
    *,
    max_pages: int,
    log: Log,
    label: str,
    start: int,
    prefetched: dict[int, Any],
) -> int:
    _clear_list_stamp(directory)
    seen: set[tuple[str, ...]] = set()
    count = 0
    for page in range(1, start):
        payload = _read_json(directory / f"page-{page:04d}.json")
        items = extract_items(payload)
        seen.add(tuple(_identity(item) for item in items))
        count += len(items)
    page = start
    reached_end = False
    keep_through = start - 1
    while True:
        if max_pages and page > max_pages:
            log(f"{label}: stopped at --max-pages {max_pages}")
            break
        path = directory / f"page-{page:04d}.json"
        if page in prefetched:
            payload = prefetched[page]
            origin = "fetched"
        else:
            try:
                payload = fetch_page(page)
            except ApiError as error:
                if _is_end_of_pages(error):
                    log(f"{label} page {page}: end (not found)")
                    reached_end = True
                    break
                raise
            origin = "fetched"
        items = extract_items(payload)
        signature = tuple(_identity(item) for item in items)
        if items and signature in seen:
            log(f"{label} page {page}: end ({origin}, duplicate)")
            keep_through = page - 1
            reached_end = True
            break
        _save(path, payload)
        if not items or not _has_more_pages(payload):
            log(f"{label} page {page}: end ({origin}, {len(items)} items)")
            keep_through = page
            reached_end = True
            break
        seen.add(signature)
        count += len(items)
        keep_through = page
        log(f"{label} page {page}: {len(items)} items ({origin}, running total {count})")
        page += 1
    if reached_end:
        _delete_pages_after(directory, keep_through)
        _write_list_stamp(directory, keep_through)
    return count


def _probe_page(directory: Path, fetch_page, page: int) -> tuple[str, Any]:
    """Return ('same', payload), ('changed', payload), or ('end', None)."""
    try:
        payload = fetch_page(page)
    except ApiError as error:
        if _is_end_of_pages(error):
            return "end", None
        raise
    path = directory / f"page-{page:04d}.json"
    if path.exists() and _payload_matches(path, payload):
        return "same", payload
    return "changed", payload


def _payload_matches(path: Path, payload: Any) -> bool:
    try:
        saved = _read_json(path)
    except (OSError, json.JSONDecodeError):
        return False
    return _page_fingerprint(saved) == _page_fingerprint(payload)


def _page_fingerprint(payload: Any) -> str:
    """Identity of a list page, ignoring fields that change without an edit.

    ``buildtime`` is a clock on the response envelope. Like and comment counts
    change when someone else interacts with a moment. Neither means the saved
    highlight list shifted.
    """
    items = []
    for item in extract_items(payload):
        if isinstance(item, dict):
            items.append({key: value for key, value in item.items() if key not in ("commenting", "liking")})
        else:
            items.append(item)
    return json.dumps(
        {"items": items, "next_page": _next_page(payload)},
        sort_keys=True,
        default=str,
    )


def _has_more_pages(payload: Any) -> bool:
    """Whether another numbered page should be requested after this one."""
    if not extract_items(payload):
        return False
    if _next_page(payload) is None and _has_next_page_field(payload):
        return False
    return True


def _next_page(payload: Any) -> Any:
    node = _data_node(payload)
    if isinstance(node, dict):
        return node.get("next_page")
    return None


def _has_next_page_field(payload: Any) -> bool:
    node = _data_node(payload)
    return isinstance(node, dict) and "next_page" in node


def _data_node(payload: Any) -> Any:
    if not isinstance(payload, dict):
        return payload
    response = payload.get("response")
    if isinstance(response, dict) and "data" in response:
        return response["data"]
    if "data" in payload:
        return payload["data"]
    return payload


def _list_stamp_path(directory: Path) -> Path:
    return directory / "list-stamp.json"


def _clear_list_stamp(directory: Path) -> None:
    _list_stamp_path(directory).unlink(missing_ok=True)


def _write_list_stamp(directory: Path, last_page: int) -> None:
    digest = hashlib.sha256()
    for page in range(1, last_page + 1):
        path = directory / f"page-{page:04d}.json"
        if not path.exists():
            return
        digest.update(_page_fingerprint(_read_json(path)).encode())
        digest.update(b"\0")
    _save(_list_stamp_path(directory), {"pages": last_page, "sha256": digest.hexdigest()})


def _list_is_complete(directory: Path) -> bool:
    """True when these page files came from one finished download."""
    path = _list_stamp_path(directory)
    if not path.exists():
        return False
    try:
        stamp = _read_json(path)
    except (OSError, json.JSONDecodeError):
        return False
    last_page = _highest_page(directory)
    if stamp.get("pages") != last_page or not isinstance(stamp.get("sha256"), str):
        return False
    digest = hashlib.sha256()
    for page in range(1, last_page + 1):
        page_path = directory / f"page-{page:04d}.json"
        if not page_path.exists():
            return False
        try:
            digest.update(_page_fingerprint(_read_json(page_path)).encode())
        except (OSError, json.JSONDecodeError):
            return False
        digest.update(b"\0")
    return digest.hexdigest() == stamp["sha256"]


def _count_saved_items(directory: Path, last_page: int) -> int:
    count = 0
    for page in range(1, last_page + 1):
        path = directory / f"page-{page:04d}.json"
        if path.exists():
            count += len(extract_items(_read_json(path)))
    return count


def _delete_pages_after(directory: Path, page: int) -> None:
    for path in _page_files(directory):
        number = _page_number(path)
        if number is not None and number > page:
            path.unlink()


def _page_number(path: Path) -> int | None:
    try:
        return int(path.stem.split("-")[1])
    except (IndexError, ValueError):
        return None


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
