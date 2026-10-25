"""Command line for syncing and searching a local YouVersion archive."""

from __future__ import annotations

import argparse
import csv
import getpass
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from bible_archive import __version__
from bible_archive.auth import (
    SIGN_IN_HELP,
    AuthError,
    access_token_needs_refresh,
    exchange_session_cookie,
    is_session_cookie_text,
    jwt_expiry,
    load_session,
    parse_session_cookie,
    parse_session_text,
    save_session,
)
from bible_archive.client import ApiError, YouVersionClient, urllib_transport
from bible_archive.normalize import parse_terms
from bible_archive.server import serve
from bible_archive.store import Catalog, date_window
from bible_archive.sync import peek, rebuild_catalog, sync_account

DEFAULT_KINDS = ["highlight", "note", "bookmark", "client_side", "event"]
MIN_DELAY = 0.5


def main(argv: list[str] | None = None) -> int:
    load_env(Path.cwd() / ".env")
    parser = argparse.ArgumentParser(
        prog="bible-archive",
        description="Save your YouVersion highlights and notes into a local searchable catalog.",
    )
    parser.add_argument("--data", help="Directory for the archive. Default: ./data")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)

    login = commands.add_parser("login", help="Save a bible.com session token or yva_session cookie")
    login.add_argument("--no-check", action="store_true", help="Save the token without calling YouVersion")
    login.set_defaults(func=command_login)

    auth_refresh = commands.add_parser("auth-refresh", help="Renew the access token from the saved cookie")
    auth_refresh.set_defaults(func=command_auth_refresh)

    sync = commands.add_parser("sync", help="Download highlights and notes, slowly")
    sync.add_argument("--delay", type=float, default=None, help="Seconds between requests (minimum 0.5)")
    sync.add_argument("--max-pages", type=int, default=0, help="Stop each list after this many pages")
    sync.add_argument("--kinds", default=",".join(DEFAULT_KINDS), help="Comma-separated list to download")
    sync.add_argument("--redownload", action="store_true", help="Download pages again instead of using saved pages")
    sync.set_defaults(func=command_sync)

    search = commands.add_parser("search", help="Search the local catalog")
    search.add_argument("query", nargs="?", default="")
    search.add_argument("--kind", action="append", default=[])
    search.add_argument("--from", dest="start")
    search.add_argument("--to", dest="end")
    search.add_argument("--month", help="YYYY-MM")
    search.add_argument("--version", type=int)
    search.add_argument("--oldest", action="store_true")
    search.add_argument("--limit", type=int, default=20)
    search.set_defaults(func=command_search)

    web = commands.add_parser("serve", help="Open the local search page")
    web.add_argument("--host", default="127.0.0.1")
    web.add_argument("--port", type=int, default=8765)
    web.set_defaults(func=command_serve)

    commands.add_parser("rebuild", help="Rebuild the catalog from saved raw JSON").set_defaults(func=command_rebuild)
    commands.add_parser("peek", help="Show the shape of saved responses, without note text").set_defaults(func=command_peek)

    export = commands.add_parser("export", help="Write JSON and CSV copies of the catalog")
    export.add_argument("--dest", help="Directory for the files. Default: <data>/export")
    export.set_defaults(func=command_export)

    imported = commands.add_parser("import-json", help="Add one saved API response to the raw archive")
    imported.add_argument("file", type=Path)
    imported.add_argument("--kind", required=True, choices=["highlight", "note", "bookmark", "client_side", "event"])
    imported.set_defaults(func=command_import)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except AuthError as error:
        print(error, file=sys.stderr)
        return 2
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nStopped.")
        return 130


def command_login(args: argparse.Namespace) -> int:
    root = data_root(args)
    session_path = root / "session.json"
    transport = urllib_transport()
    if cookie_from_env():
        token, user_id, cookie = exchange_session_cookie(cookie_from_env(), transport)
    elif os.environ.get("YOUVERSION_ACCESS_TOKEN", "").strip():
        token, user_id = token_from_env()
        cookie = None
    elif sys.stdin.isatty():
        token, user_id, cookie = prompt_for_session(session_path, transport, save=False)
    else:
        token, user_id, cookie = acquire_login(session_path, transport, prompt=False)
    if not args.no_check:
        try:
            check_token(token, user_id, transport)
        except ApiError as error:
            save_session(session_path, token, user_id, cookie)
            print(f"Stored the session, but the check failed: {error}", file=sys.stderr)
            return 0
    save_session(session_path, token, user_id, cookie)
    who = user_id or "unknown"
    print(f"Signed in. User id: {who}. Session saved in {session_path}")
    if cookie or (load_session(session_path) or {}).get("session_cookie"):
        print("Later syncs refresh the access token with the saved yva_session cookie.")
    return 0


def command_auth_refresh(args: argparse.Namespace) -> int:
    session_path = data_root(args) / "session.json"
    token, user_id, cookie = refresh_saved_session(session_path, urllib_transport())
    who = user_id or "unknown"
    print(f"Refreshed the access token. User id: {who}. Expires {_format_expiry(jwt_expiry(token))}.")
    if cookie:
        print(f"Cookie saved in {session_path}.")
    return 0


def refresh_saved_session(session_path: Path, transport) -> tuple[str, str | None, str]:
    """Exchange the saved yva_session cookie. This does not download highlights."""
    saved = load_session(session_path) or {}
    cookie = cookie_from_env() or str(saved.get("session_cookie") or "")
    if not cookie:
        raise AuthError(
            "No yva_session cookie is saved. An access token cannot be refreshed by itself. "
            "Run login and paste yva_session=VALUE from https://www.bible.com."
        )
    token, user_id, cookie = exchange_session_cookie(cookie, transport)
    save_session(session_path, token, user_id, cookie)
    return token, user_id, cookie


def command_sync(args: argparse.Namespace) -> int:
    root = data_root(args)
    delay = args.delay if args.delay is not None else _float_env("YOUVERSION_DELAY_SECONDS", 1.5)
    if delay < MIN_DELAY:
        print(f"Using a {MIN_DELAY}s delay. Faster polling is more likely to trip rate limits.")
        delay = MIN_DELAY
    kinds = [kind.strip() for kind in args.kinds.split(",") if kind.strip()]
    client = signed_in_client(root, delay)
    print(f"Signed in. Waiting {delay:.1f}s between requests. Archive: {root}")
    if args.max_pages:
        print(f"Stopping each list after {args.max_pages} page(s). Run sync again with no --max-pages to continue.")
    result = sync_account(
        client,
        root,
        kinds=kinds,
        max_pages=args.max_pages,
        redownload=args.redownload,
    )
    for error in result["errors"]:
        print(f"Warning: {error}", file=sys.stderr)
    print("Search it with: python -m bible_archive serve")
    return 0


def command_search(args: argparse.Namespace) -> int:
    root = data_root(args)
    database = root / "catalog.sqlite"
    if not database.exists():
        print("No catalog yet. Run: python -m bible_archive sync", file=sys.stderr)
        return 1
    start, end = date_window(args.start or "", args.end or "", args.month or "")
    catalog = Catalog(database)
    try:
        result = catalog.search(
            terms=parse_terms(args.query),
            kinds=args.kind or None,
            start=start,
            end=end,
            version_id=args.version,
            sort="oldest" if args.oldest else "newest",
            limit=max(1, args.limit),
            offset=0,
        )
    finally:
        catalog.close()
    print(f"{result['total']} match(es)")
    for item in result["items"]:
        print(format_item(item))
        print()
    return 0


def command_serve(args: argparse.Namespace) -> int:
    root = data_root(args)
    database = root / "catalog.sqlite"
    if not database.exists():
        Catalog(database).close()
    serve(database, args.host, args.port)
    return 0


def command_rebuild(args: argparse.Namespace) -> int:
    stats = rebuild_catalog(data_root(args))
    print(
        f"Inserted {stats['inserted']}, updated {stats['updated']}, "
        f"unchanged {stats['unchanged']}, removed {stats['removed']}."
    )
    return 0


def command_peek(args: argparse.Namespace) -> int:
    peek(data_root(args) / "raw")
    return 0


def command_export(args: argparse.Namespace) -> int:
    root = data_root(args)
    database = root / "catalog.sqlite"
    if not database.exists():
        print("No catalog yet. Run: python -m bible_archive sync", file=sys.stderr)
        return 1
    dest = Path(args.dest) if args.dest else root / "export"
    dest.mkdir(parents=True, exist_ok=True)
    catalog = Catalog(database)
    try:
        rows = catalog.export_rows()
    finally:
        catalog.close()
    json_path = dest / "catalog.json"
    csv_path = dest / "catalog.csv"
    json_path.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    fields = [
        "kind",
        "created_at",
        "updated_at",
        "reference_human",
        "usfm",
        "labels",
        "title",
        "content",
        "color",
        "status",
        "id",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    print(f"Wrote {len(rows)} rows to {json_path} and {csv_path}")
    return 0


def command_import(args: argparse.Namespace) -> int:
    root = data_root(args)
    payload = json.loads(args.file.read_text(encoding="utf-8"))
    if args.kind == "event":
        dest = root / "raw" / "events" / "page-import.json"
    else:
        dest = root / "raw" / "moments" / args.kind / "page-import.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    stats = rebuild_catalog(root)
    print(f"Imported {args.file} as {args.kind}. Catalog inserted {stats['inserted']}, updated {stats['updated']}.")
    return 0


def signed_in_client(root: Path, delay: float) -> YouVersionClient:
    transport = urllib_transport()
    session_path = root / "session.json"
    token, user_id, _cookie = acquire_login(session_path, transport, prompt=True)

    def sign_in() -> tuple[str, str | None]:
        fresh, fresh_user, cookie = acquire_login(session_path, transport, prompt=True, force_refresh=True)
        save_session(session_path, fresh, fresh_user, cookie)
        return fresh, fresh_user

    return YouVersionClient(
        transport,
        token=token,
        user_id=str(user_id) if user_id else None,
        delay=delay,
        login=sign_in,
    )


def acquire_login(
    session_path: Path,
    transport,
    *,
    prompt: bool,
    force_refresh: bool = False,
) -> tuple[str, str | None, str | None]:
    """Return a token, user id, and yva_session cookie. The cookie may be absent."""
    saved = load_session(session_path) or {}
    cookie = cookie_from_env() or str(saved.get("session_cookie") or "")
    found = token_from_env()
    if found is None and saved.get("access_token") and not force_refresh:
        found = (str(saved["access_token"]), saved.get("user_id"))
    if cookie and (force_refresh or found is None or access_token_needs_refresh(found[0])):
        try:
            token, user_id, cookie = exchange_session_cookie(cookie, transport)
        except AuthError as error:
            if found is not None and not force_refresh:
                print(
                    "Could not refresh the bible.com session. The saved token is still in use.",
                    file=sys.stderr,
                )
                return found[0], found[1], cookie or None
            if not prompt:
                raise
            print(error, file=sys.stderr)
        else:
            save_session(session_path, token, user_id, cookie)
            print("Refreshed the bible.com session.", file=sys.stderr)
            return token, user_id, cookie
    if found is not None and not force_refresh:
        return found[0], found[1], cookie or None
    if not prompt:
        raise AuthError("Set YOUVERSION_SESSION_COOKIE or YOUVERSION_ACCESS_TOKEN, then run this again.")
    return prompt_for_session(session_path, transport)


def token_from_env() -> tuple[str, str | None] | None:
    raw = os.environ.get("YOUVERSION_ACCESS_TOKEN", "").strip()
    if not raw:
        return None
    return parse_session_text(raw)


def cookie_from_env() -> str:
    raw = os.environ.get("YOUVERSION_SESSION_COOKIE", "").strip()
    if not raw:
        return ""
    if is_session_cookie_text(raw):
        return parse_session_cookie(raw)
    return raw


def prompt_for_session(
    session_path: Path,
    transport,
    *,
    save: bool = True,
) -> tuple[str, str | None, str | None]:
    print(SIGN_IN_HELP, file=sys.stderr)
    if os.environ.get("YOUVERSION_PASSWORD"):
        print("The password in the environment was not sent.", file=sys.stderr)
    if not sys.stdin.isatty():
        raise AuthError(
            "Set YOUVERSION_SESSION_COOKIE or YOUVERSION_ACCESS_TOKEN, then run this again."
        )
    pasted = getpass.getpass("Paste yva_session=VALUE, session JSON, or an access token (input stays hidden): ")
    if is_session_cookie_text(pasted):
        token, user_id, cookie = exchange_session_cookie(parse_session_cookie(pasted), transport)
    else:
        token, user_id = parse_session_text(pasted)
        cookie = None
    if save:
        save_session(session_path, token, user_id, cookie)
    return token, user_id, cookie


def check_token(token: str, user_id: str | None, transport=None) -> None:
    """One read of the account labels. A rejected token raises AuthError."""
    client = YouVersionClient(
        transport or urllib_transport(),
        token=token,
        user_id=str(user_id) if user_id else None,
        delay=0,
    )
    try:
        client.labels()
    except ApiError as error:
        if error.status in {401, 403}:
            raise AuthError(
                "YouVersion rejected that session token. Sign in at https://www.bible.com "
                "and copy a new /api/authenticate response."
            ) from error
        raise


def _format_expiry(expires_at: int | None) -> str:
    if expires_at is None:
        return "unknown"
    return datetime.fromtimestamp(expires_at, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def data_root(args: argparse.Namespace) -> Path:
    raw = args.data or os.environ.get("BIBLE_ARCHIVE_DIR") or "data"
    path = Path(raw)
    path.mkdir(parents=True, exist_ok=True)
    return path


def format_item(item: dict) -> str:
    versions = ", ".join(
        version["abbreviation"] or f"version {version['version_id']}" for version in item["versions"]
    )
    when = (item["created_at"] or "undated")[:10]
    heading = item["reference_human"] or item["usfm"] or item["title"] or item["id"]
    pieces = [when, item["kind"], heading]
    if versions:
        pieces.append(versions)
    if item["labels"]:
        pieces.append(item["labels"])
    lines = ["  ".join(pieces)]
    if item["title"] and item["title"] not in heading:
        lines.append(f"  {item['title']}")
    text = " ".join(item["content"].split())
    if text:
        lines.append(f"  {text[:400]}")
    return "\n".join(lines)


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default
