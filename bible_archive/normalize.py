"""Turn YouVersion moment and event payloads into catalog rows."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any

_QUOTED = re.compile(r'"([^"]+)"')
_HEX_COLOR = re.compile(r"^[0-9a-fA-F]{6}$")
_DATE_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}")

NOTE_FIELD_NAMES = {
    "user_note",
    "user_notes",
    "my_note",
    "note",
    "notes",
    "annotation",
    "annotations",
}
MOMENT_TEXT_FIELDS = ("content", "text", "body")
ITEM_LIST_KEYS = (
    "moments",
    "items",
    "events",
    "saved_events",
    "saved",
    "labels",
    "versions",
)


def parse_terms(query: str) -> list[str]:
    """Split a search into quoted phrases and words. Every term must match."""
    if not query or not query.strip():
        return []
    phrases = [part.strip() for part in _QUOTED.findall(query) if part.strip()]
    rest = _QUOTED.sub(" ", query)
    words = [part for part in rest.split() if part.strip()]
    seen: set[str] = set()
    terms: list[str] = []
    for term in phrases + words:
        key = term.casefold()
        if key in seen:
            continue
        seen.add(key)
        terms.append(term)
    return terms


def extract_items(payload: Any) -> list[dict[str, Any]]:
    """Pull the list of records out of a moments or events response."""
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    unwrapped = _unwrap(payload)
    found = _find_item_list(unwrapped)
    if found:
        return found
    if unwrapped is not payload:
        return _find_item_list(payload)
    return []


def payload_keys(payload: Any) -> list[str]:
    if isinstance(payload, dict):
        return [str(key) for key in payload.keys()]
    if isinstance(payload, list):
        return ["<list>"]
    return [type(payload).__name__]


def normalize_moment(
    item: dict[str, Any],
    fallback_kind: str,
    label_names: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Normalize one highlight, note, bookmark, or client-side item."""
    obj, extras = _moment_layers(item)
    kind = _kind_name(item.get("kind") or item.get("kind_id") or obj.get("kind") or obj.get("kind_id") or fallback_kind)
    youversion_id = _first_id(obj, item)
    references = _collect_references(obj)
    labels = _label_names(
        extras.get("labels", obj.get("labels", item.get("labels"))),
        label_names or {},
    )
    title = _plain_title(extras.get("title")) or _plain_title(obj.get("title")) or _plain_title(item.get("title"))
    content = _moment_content(extras) or _moment_content(obj)
    color = _colors(extras, references) or _colors(obj, references)
    created_at = normalize_datetime(
        obj.get("created_dt")
        or obj.get("created_at")
        or obj.get("create_time")
        or item.get("created_dt")
        or item.get("created_at")
    )
    updated_at = normalize_datetime(
        obj.get("updated_dt") or obj.get("updated_at") or item.get("updated_dt") or item.get("updated_at")
    )
    return _row(
        kind=kind,
        youversion_id=youversion_id,
        title=title,
        content=content,
        color=color,
        status=_as_text(obj.get("status") or item.get("status")),
        created_at=created_at,
        updated_at=updated_at,
        references=references,
        labels=labels,
        raw=item,
        source=fallback_kind,
    )


def normalize_event(item: dict[str, Any]) -> dict[str, Any]:
    """Normalize a saved event, keeping the notes typed on its modules."""
    obj = item.get("event") if isinstance(item.get("event"), dict) else item
    if isinstance(item.get("object"), dict) and "event" not in item:
        obj = item["object"]
    youversion_id = _first_id(obj, item)
    references = _collect_references(obj)
    notes = _event_notes(obj)
    title = _as_text(obj.get("title") or obj.get("name") or item.get("title") or item.get("name"))
    created_at = normalize_datetime(
        obj.get("saved_dt")
        or obj.get("created_dt")
        or obj.get("start_dt")
        or obj.get("starts_at")
        or item.get("saved_dt")
        or item.get("created_dt")
        or item.get("start_dt")
    )
    updated_at = normalize_datetime(obj.get("updated_dt") or item.get("updated_dt"))
    return _row(
        kind="event",
        youversion_id=youversion_id,
        title=title,
        content="\n\n".join(notes),
        color="",
        status=_as_text(obj.get("status") or item.get("status")),
        created_at=created_at,
        updated_at=updated_at,
        references=references,
        labels=_label_names(obj.get("labels"), {}),
        raw=item,
        source="event",
    )


def label_map(payload: Any) -> dict[str, str]:
    """Map label ids to names from a labels response."""
    names: dict[str, str] = {}
    items = extract_items(payload)
    if not items and isinstance(payload, dict):
        unwrapped = _unwrap(payload)
        if isinstance(unwrapped, dict):
            items = [value for value in unwrapped.values() if isinstance(value, dict)]
    for item in items:
        name = _as_text(item.get("name") or item.get("label") or item.get("title"))
        item_id = item.get("id", item.get("label_id"))
        if item_id is not None and name:
            names[str(item_id)] = name
        if name:
            names[name] = name
    return names


def moment_version_names(item: dict[str, Any]) -> dict[int, str]:
    """Map a moment's version id to the abbreviation stored on that moment.

    bible.com puts the abbreviation on the card, in base.title.l_args, and the
    id on each reference. A card with one abbreviation and one version id is
    the name for that id. Notes often omit the abbreviation; highlights that
    share the id supply it.
    """
    abbreviation = _version_abbreviation(item)
    if not abbreviation:
        return {}
    version_ids: list[int] = []
    for reference in _collect_references(item):
        version_id = reference.get("version_id")
        if version_id is not None and str(version_id).isdigit():
            token = int(version_id)
            if token not in version_ids:
                version_ids.append(token)
    if len(version_ids) != 1:
        return {}
    return {version_ids[0]: abbreviation}


def version_fields(payload: Any) -> tuple[str, str]:
    """Read an abbreviation and title from a version response."""
    nodes: list[dict[str, Any]] = []
    if isinstance(payload, dict):
        unwrapped = _unwrap(payload)
        if isinstance(unwrapped, dict):
            for key in ("version", "bible"):
                if isinstance(unwrapped.get(key), dict):
                    nodes.append(unwrapped[key])
            nodes.append(unwrapped)
        for key in ("version", "bible"):
            if isinstance(payload.get(key), dict):
                nodes.append(payload[key])
        nodes.append(payload)
    abbreviation = ""
    title = ""
    for node in nodes:
        abbreviation = abbreviation or _as_text(
            node.get("abbreviation") or node.get("local_abbreviation") or node.get("short_name")
        )
        title = title or _as_text(node.get("title") or node.get("local_title") or node.get("name"))
    return abbreviation, title


def normalize_datetime(value: Any) -> str:
    """Return a sortable UTC timestamp, or a blank string when there is no date."""
    if isinstance(value, (int, float)) and value > 10_000_000:
        moment = datetime.fromtimestamp(float(value), timezone.utc)
        return moment.strftime("%Y-%m-%dT%H:%M:%S")
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text:
        return ""
    iso = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(iso)
    except ValueError:
        if _DATE_PREFIX.match(text):
            return text[:19]
        return ""
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed.strftime("%Y-%m-%dT%H:%M:%S")


def _row(
    *,
    kind: str,
    youversion_id: str,
    title: str,
    content: str,
    color: str,
    status: str,
    created_at: str,
    updated_at: str,
    references: list[dict[str, Any]],
    labels: list[str],
    raw: dict[str, Any],
    source: str,
) -> dict[str, Any]:
    humans: list[str] = []
    usfms: list[str] = []
    version_ids: list[str] = []
    for reference in references:
        human = _as_text(reference.get("human"))
        if human and human not in humans:
            humans.append(human)
        for usfm in _usfm_values(reference.get("usfm")):
            if usfm not in usfms:
                usfms.append(usfm)
        version_id = reference.get("version_id")
        if version_id is not None and str(version_id).isdigit():
            token = str(int(version_id))
            if token not in version_ids:
                version_ids.append(token)
    if not youversion_id:
        digest = hashlib.sha256(
            json.dumps(raw, sort_keys=True, default=str, ensure_ascii=False).encode()
        ).hexdigest()[:16]
        youversion_id = digest
    clean_labels = [label for label in labels if label]
    reference_human = "; ".join(humans)
    usfm_text = " ".join(usfms)
    search_text = "\n".join(
        part.casefold()
        for part in (title, content, reference_human, usfm_text, " ".join(clean_labels), kind)
        if part
    )
    primary_version = int(version_ids[0]) if version_ids else None
    return {
        "id": f"{kind}:{youversion_id}",
        "youversion_id": youversion_id,
        "kind": kind,
        "title": title,
        "content": content,
        "color": color,
        "status": status,
        "created_at": created_at,
        "updated_at": updated_at,
        "reference_human": reference_human,
        "usfm": usfm_text,
        "version_ids": f",{','.join(version_ids)}," if version_ids else ",",
        "primary_version_id": primary_version,
        "labels": ", ".join(clean_labels),
        "search_text": search_text,
        "raw_json": json.dumps(raw, ensure_ascii=False, sort_keys=True, default=str),
        "source": source,
    }


def _unwrap(payload: dict[str, Any]) -> Any:
    response = payload.get("response")
    if isinstance(response, dict) and "data" in response:
        data = response.get("data")
        return data if data is not None else {}
    return payload


def _find_item_list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ITEM_LIST_KEYS:
        value = payload.get(key)
        found = _list_of_dicts(value)
        if found:
            return found
        if isinstance(value, dict):
            nested = _find_item_list(value)
            if nested:
                return nested
    best: list[dict[str, Any]] = []
    for value in payload.values():
        found = _list_of_dicts(value)
        if len(found) > len(best):
            best = found
    return best


def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
        return value
    return []


def _kind_name(value: Any) -> str:
    text = _as_text(value).split(".", 1)[0].strip().lower().replace(" ", "_")
    return text or "item"


def _first_id(*nodes: Any) -> str:
    for node in nodes:
        if not isinstance(node, dict):
            continue
        for key in ("id", "moment_id", "event_id"):
            if node.get(key) is not None:
                return str(node[key])
    return ""


def _version_abbreviation(item: dict[str, Any]) -> str:
    nodes = [item]
    obj = item.get("object")
    if isinstance(obj, dict):
        nodes.append(obj)
    for node in nodes:
        base = node.get("base")
        if not isinstance(base, dict):
            continue
        title = base.get("title")
        if not isinstance(title, dict):
            continue
        l_args = title.get("l_args")
        if not isinstance(l_args, dict):
            continue
        abbreviation = l_args.get("version_abbreviation")
        if isinstance(abbreviation, str) and abbreviation.strip():
            return abbreviation.strip()
    return ""


def _moment_layers(item: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the legacy object and the bible.com extras block, when present."""
    obj = item.get("object") if isinstance(item.get("object"), dict) else item
    extras = item.get("extras") if isinstance(item.get("extras"), dict) else {}
    if not extras and isinstance(obj.get("extras"), dict):
        extras = obj["extras"]
    return obj, extras


def _plain_title(value: Any) -> str:
    """Use a title only when YouVersion sent one string. Card chrome is a nested object."""
    if isinstance(value, str):
        return value.strip()
    return ""


def _moment_content(obj: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in MOMENT_TEXT_FIELDS:
        text = _as_text(obj.get(key))
        if text and text not in parts:
            parts.append(text)
    return "\n\n".join(parts)


def _colors(obj: dict[str, Any], references: list[dict[str, Any]]) -> str:
    found: list[str] = []
    for value in [obj.get("color"), *[reference.get("color") for reference in references]]:
        color = _clean_color(value)
        if color and color not in found:
            found.append(color)
    return found[0] if found else ""


def _clean_color(value: Any) -> str:
    text = _as_text(value).removeprefix("#")
    if _HEX_COLOR.match(text):
        return text.lower()
    return ""


def _collect_references(node: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []

    def walk(value: Any) -> None:
        if isinstance(value, list):
            for entry in value:
                walk(entry)
            return
        if not isinstance(value, dict):
            return
        if "usfm" in value or ("human" in value and "version_id" in value):
            found.append(value)
            return
        for child in value.values():
            walk(child)

    walk(node)
    return found


def _usfm_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        return [str(entry) for entry in value if isinstance(entry, str) and entry]
    return []


def _label_names(value: Any, names: dict[str, str]) -> list[str]:
    if value is None:
        return []
    entries = [value] if isinstance(value, (str, int)) else value
    if not isinstance(entries, list):
        return []
    result: list[str] = []
    for entry in entries:
        if isinstance(entry, dict):
            name = _as_text(entry.get("name") or entry.get("label") or entry.get("title"))
            if not name and entry.get("id") is not None:
                name = names.get(str(entry["id"]), "")
            label = name
        else:
            label = names.get(str(entry), _as_text(entry))
        if label and label not in result:
            result.append(label)
    return result


def _event_notes(node: Any) -> list[str]:
    notes: list[str] = []

    def add(value: Any) -> None:
        for piece in _stringify_note(value):
            if piece not in notes:
                notes.append(piece)

    def walk(value: Any) -> None:
        if isinstance(value, list):
            for entry in value:
                walk(entry)
            return
        if not isinstance(value, dict):
            return
        for key, child in value.items():
            name = str(key).lower()
            if name in NOTE_FIELD_NAMES:
                add(child)
            elif name == "comments":
                add(_user_comments(child))
            else:
                walk(child)

    walk(node)
    return notes


def _user_comments(value: Any) -> Any:
    """Keep module notes. Skip social comment threads, which carry a user."""
    if isinstance(value, list):
        kept = []
        for entry in value:
            if isinstance(entry, dict) and any(key in entry for key in ("user", "username", "user_id")):
                continue
            kept.append(entry)
        return kept
    if isinstance(value, dict) and any(key in value for key in ("user", "username", "user_id")):
        return ""
    return value


def _stringify_note(value: Any) -> list[str]:
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, list):
        parts: list[str] = []
        for entry in value:
            parts.extend(_stringify_note(entry))
        return parts
    if isinstance(value, dict):
        for key in ("text", "content", "body", "note"):
            if key in value and not any(name in value for name in ("user", "username")):
                return _stringify_note(value.get(key))
        parts = []
        for child in value.values():
            if isinstance(child, str):
                parts.extend(_stringify_note(child))
        return parts
    return []


def _as_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, dict):
        for key in ("default", "en", "name", "text", "title"):
            if isinstance(value.get(key), str):
                return value[key].strip()
        for child in value.values():
            if isinstance(child, str) and child.strip():
                return child.strip()
    return ""
