"""SQLite catalog of highlights, notes, bookmarks, and sermon notes."""

from __future__ import annotations

import sqlite3
from collections.abc import Collection, Iterable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id TEXT PRIMARY KEY,
    youversion_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    color TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    reference_human TEXT NOT NULL DEFAULT '',
    usfm TEXT NOT NULL DEFAULT '',
    version_ids TEXT NOT NULL DEFAULT ',',
    primary_version_id INTEGER,
    labels TEXT NOT NULL DEFAULT '',
    search_text TEXT NOT NULL DEFAULT '',
    raw_json TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS items_created ON items(created_at);
CREATE INDEX IF NOT EXISTS items_kind_created ON items(kind, created_at);
CREATE TABLE IF NOT EXISTS versions (
    version_id INTEGER PRIMARY KEY,
    abbreviation TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    raw_json TEXT NOT NULL DEFAULT ''
);
"""


class Catalog:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def replace_items(self, items: Iterable[dict[str, Any]]) -> dict[str, int]:
        """Upsert every item, then drop rows that were not in this set."""
        stats = {"inserted": 0, "updated": 0, "unchanged": 0, "removed": 0}
        seen: list[str] = []
        with self.conn:
            for item in items:
                stats[_upsert(self.conn, item)] += 1
                seen.append(item["id"])
            self.conn.execute("DROP TABLE IF EXISTS seen")
            self.conn.execute("CREATE TEMP TABLE seen(id TEXT PRIMARY KEY)")
            self.conn.executemany(
                "INSERT OR IGNORE INTO seen(id) VALUES (?)",
                [(item_id,) for item_id in seen],
            )
            removed = self.conn.execute(
                "DELETE FROM items WHERE id NOT IN (SELECT id FROM seen)"
            ).rowcount
            self.conn.execute("DROP TABLE seen")
            stats["removed"] = removed if removed is not None else 0
        return stats

    def version_ids(self) -> list[int]:
        rows = self.conn.execute(
            """
            SELECT DISTINCT primary_version_id AS version_id FROM items
            WHERE primary_version_id IS NOT NULL
            """
        ).fetchall()
        return [int(row["version_id"]) for row in rows]

    def unnamed_version_ids(self) -> list[int]:
        """Version ids on items that still have no abbreviation and no title."""
        rows = self.conn.execute(
            "SELECT version_id FROM versions WHERE abbreviation != '' OR title != ''"
        ).fetchall()
        named = {int(row["version_id"]) for row in rows}
        return [version_id for version_id in self.version_ids() if version_id not in named]

    def merge_version_names(self, names: dict[int, str]) -> None:
        """Store abbreviations found on saved moments. Keep a title the version API already filled in."""
        with self.conn:
            for version_id, abbreviation in names.items():
                abbreviation = abbreviation.strip()
                if not abbreviation:
                    continue
                row = self.conn.execute(
                    "SELECT abbreviation FROM versions WHERE version_id = ?",
                    (version_id,),
                ).fetchone()
                if row is None:
                    self.conn.execute(
                        """
                        INSERT INTO versions(version_id, abbreviation, title, raw_json)
                        VALUES (?, ?, '', '')
                        """,
                        (version_id, abbreviation),
                    )
                elif row["abbreviation"] != abbreviation:
                    self.conn.execute(
                        "UPDATE versions SET abbreviation = ? WHERE version_id = ?",
                        (abbreviation, version_id),
                    )

    def save_version(self, version_id: int, abbreviation: str, title: str, raw_json: str) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO versions(version_id, abbreviation, title, raw_json)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(version_id) DO UPDATE SET
                    abbreviation = excluded.abbreviation,
                    title = excluded.title,
                    raw_json = excluded.raw_json
                """,
                (version_id, abbreviation, title, raw_json),
            )

    def search(
        self,
        *,
        terms: list[str],
        kinds: list[str] | None = None,
        start: str = "",
        end: str = "",
        version_id: int | None = None,
        sort: str = "newest",
        limit: int = 40,
        offset: int = 0,
        exclude_kinds: Collection[str] | None = None,
    ) -> dict[str, Any]:
        clause, params = _filters(terms, kinds, start, end, version_id, exclude_kinds)
        total = self.conn.execute(f"SELECT COUNT(*) FROM items{clause}", params).fetchone()[0]
        order = "created_at DESC, id DESC" if sort != "oldest" else "created_at ASC, id ASC"
        rows = self.conn.execute(
            f"""
            SELECT * FROM items{clause}
            ORDER BY (created_at = '') ASC, {order}
            LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        ).fetchall()
        return {"total": total, "items": [self._public_row(row) for row in rows]}

    def facets(
        self,
        *,
        terms: list[str],
        kinds: list[str] | None = None,
        start: str = "",
        end: str = "",
        version_id: int | None = None,
        sort: str = "newest",
        exclude_kinds: Collection[str] | None = None,
    ) -> dict[str, Any]:
        kind_clause, kind_params = _filters(terms, None, start, end, version_id, exclude_kinds)
        kind_rows = self.conn.execute(
            f"SELECT kind, COUNT(*) AS count FROM items{kind_clause} GROUP BY kind ORDER BY kind",
            kind_params,
        ).fetchall()
        month_clause, month_params = _filters(terms, kinds, "", "", version_id, exclude_kinds)
        month_order = "ASC" if sort == "oldest" else "DESC"
        month_sql = (
            "SELECT substr(created_at, 1, 7) AS month, COUNT(*) AS count "
            f"FROM items{month_clause + (' AND ' if month_clause else ' WHERE ')}created_at != '' "
            f"GROUP BY month ORDER BY month {month_order}"
        )
        month_rows = self.conn.execute(month_sql, month_params).fetchall()
        version_clause, version_params = _filters(terms, kinds, start, end, None, exclude_kinds)
        version_rows = self.conn.execute(
            f"""
            SELECT primary_version_id AS version_id, COUNT(*) AS count
            FROM items{version_clause + (' AND ' if version_clause else ' WHERE ')}
                primary_version_id IS NOT NULL
            GROUP BY primary_version_id
            ORDER BY count DESC, primary_version_id
            """,
            version_params,
        ).fetchall()
        versions = self._version_map([int(row["version_id"]) for row in version_rows])
        return {
            "kinds": {row["kind"]: row["count"] for row in kind_rows},
            "months": [{"month": row["month"], "count": row["count"]} for row in month_rows],
            "versions": [
                {
                    "version_id": int(row["version_id"]),
                    "count": row["count"],
                    "abbreviation": versions.get(int(row["version_id"]), {}).get("abbreviation", ""),
                    "title": versions.get(int(row["version_id"]), {}).get("title", ""),
                }
                for row in version_rows
            ],
        }

    def export_rows(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM items ORDER BY (created_at = '') ASC, created_at DESC, id DESC"
        ).fetchall()
        return [self._public_row(row) for row in rows]

    def counts(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT kind, COUNT(*) AS count FROM items GROUP BY kind").fetchall()
        return {row["kind"]: row["count"] for row in rows}

    def _public_row(self, row: sqlite3.Row) -> dict[str, Any]:
        version_ids = [int(part) for part in str(row["version_ids"]).split(",") if part.isdigit()]
        versions = self._version_map(version_ids)
        return {
            "id": row["id"],
            "youversion_id": row["youversion_id"],
            "kind": row["kind"],
            "title": row["title"],
            "content": row["content"],
            "color": row["color"],
            "status": row["status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "reference_human": row["reference_human"],
            "usfm": row["usfm"],
            "labels": row["labels"],
            "versions": [
                {
                    "version_id": version_id,
                    "abbreviation": versions.get(version_id, {}).get("abbreviation", ""),
                    "title": versions.get(version_id, {}).get("title", ""),
                }
                for version_id in version_ids
            ],
        }

    def _version_map(self, version_ids: list[int]) -> dict[int, dict[str, str]]:
        if not version_ids:
            return {}
        marks = ",".join("?" * len(version_ids))
        rows = self.conn.execute(
            f"SELECT version_id, abbreviation, title FROM versions WHERE version_id IN ({marks})",
            version_ids,
        ).fetchall()
        return {
            int(row["version_id"]): {"abbreviation": row["abbreviation"], "title": row["title"]}
            for row in rows
        }


def like_pattern(term: str) -> str:
    escaped = term.casefold().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def date_window(start: str, end: str, month: str) -> tuple[str, str]:
    """Return an inclusive start date and an exclusive end date, as YYYY-MM-DD."""
    if month:
        parsed = datetime.strptime(month, "%Y-%m")
        if parsed.month == 12:
            following = parsed.replace(year=parsed.year + 1, month=1)
        else:
            following = parsed.replace(month=parsed.month + 1)
        return parsed.strftime("%Y-%m-%d"), following.strftime("%Y-%m-%d")
    start_day = start or ""
    end_day = ""
    if end:
        end_day = (datetime.strptime(end, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    return start_day, end_day


def _filters(
    terms: list[str],
    kinds: list[str] | None,
    start: str,
    end: str,
    version_id: int | None,
    exclude_kinds: Collection[str] | None = None,
) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    for term in terms:
        clauses.append("search_text LIKE ? ESCAPE '\\'")
        params.append(like_pattern(term))
    if kinds is not None:
        if kinds:
            clauses.append("kind IN (%s)" % ",".join("?" * len(kinds)))
            params.extend(kinds)
        else:
            clauses.append("0")
    hidden = [kind for kind in (exclude_kinds or ()) if kind]
    if hidden:
        clauses.append("kind NOT IN (%s)" % ",".join("?" * len(hidden)))
        params.extend(hidden)
    if start:
        clauses.append("created_at >= ?")
        params.append(start)
    if end:
        clauses.append("created_at != '' AND created_at < ?")
        params.append(end)
    if version_id is not None:
        clauses.append("version_ids LIKE ?")
        params.append(f"%,{int(version_id)},%")
    if not clauses:
        return "", params
    return " WHERE " + " AND ".join(clauses), params


def _upsert(conn: sqlite3.Connection, item: dict[str, Any]) -> str:
    existing = conn.execute("SELECT raw_json FROM items WHERE id = ?", (item["id"],)).fetchone()
    conn.execute(
        """
        INSERT INTO items (
            id, youversion_id, kind, title, content, color, status, created_at, updated_at,
            reference_human, usfm, version_ids, primary_version_id, labels, search_text,
            raw_json, source
        ) VALUES (
            :id, :youversion_id, :kind, :title, :content, :color, :status, :created_at, :updated_at,
            :reference_human, :usfm, :version_ids, :primary_version_id, :labels, :search_text,
            :raw_json, :source
        )
        ON CONFLICT(id) DO UPDATE SET
            youversion_id = excluded.youversion_id,
            kind = excluded.kind,
            title = excluded.title,
            content = excluded.content,
            color = excluded.color,
            status = excluded.status,
            created_at = excluded.created_at,
            updated_at = excluded.updated_at,
            reference_human = excluded.reference_human,
            usfm = excluded.usfm,
            version_ids = excluded.version_ids,
            primary_version_id = excluded.primary_version_id,
            labels = excluded.labels,
            search_text = excluded.search_text,
            raw_json = excluded.raw_json,
            source = excluded.source
        """,
        item,
    )
    if existing is None:
        return "inserted"
    if existing["raw_json"] == item["raw_json"]:
        return "unchanged"
    return "updated"
