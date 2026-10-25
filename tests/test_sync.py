import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from bible_archive.auth import (
    SIGN_IN_HELP,
    AuthError,
    access_token_needs_refresh,
    app_headers,
    exchange_session_cookie,
    parse_session_text,
    save_session,
)
from bible_archive.cli import acquire_login, check_token, refresh_saved_session
from bible_archive.client import ApiError, YouVersionClient
from bible_archive.store import Catalog
from bible_archive.sync import download_pages, sync_account


def fake_jwt(user_id="42"):
    return _jwt({"user_id": user_id})


def _jwt(payload):
    import base64

    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"header.{body}.sig"


class DownloadPagesTest(unittest.TestCase):
    def test_resume_refetches_only_the_last_page(self):
        root = Path(tempfile.mkdtemp())
        fetches = []

        def fetch(page):
            fetches.append(page)
            if page == 1:
                return {"moments": [{"id": 1, "kind_id": "highlight"}]}
            return {"moments": []}

        download_pages(root, fetch, max_pages=0, refresh=False, log=lambda _message: None, label="highlight")
        download_pages(root, fetch, max_pages=0, refresh=False, log=lambda _message: None, label="highlight")
        self.assertEqual(fetches, [1, 2, 2])

    def test_max_pages_stops(self):
        fetches = []
        root = Path(tempfile.mkdtemp())

        def fetch(page):
            fetches.append(page)
            return {"moments": [{"id": page, "kind_id": "note", "content": "x"}]}

        count = download_pages(root, fetch, max_pages=1, refresh=False, log=lambda _message: None, label="note")
        self.assertEqual(fetches, [1])
        self.assertEqual(count, 1)

    def test_not_found_ends_the_list_and_keeps_earlier_pages(self):
        root = Path(tempfile.mkdtemp())
        fetches = []

        def fetch(page):
            fetches.append(page)
            if page == 1:
                return {"moments": [{"id": 1, "kind_id": "highlight"}]}
            raise ApiError(
                404,
                '{"response": {"code": 404, "data": {"errors": [{"key": "errors.moments.not_found"}]}}}',
                "https://moments.youversionapi.com/3.1/items.json",
            )

        count = download_pages(root, fetch, max_pages=0, refresh=False, log=lambda _message: None, label="highlight")
        self.assertEqual(fetches, [1, 2])
        self.assertEqual(count, 1)
        self.assertTrue((root / "page-0001.json").exists())
        self.assertFalse((root / "page-0002.json").exists())

        def fail(page):
            raise ApiError(404, "missing route", "https://moments.youversionapi.com/3.1/items.json")

        with self.assertRaises(ApiError):
            download_pages(root / "other", fail, max_pages=0, refresh=False, log=lambda _message: None, label="note")


class ClientBehaviorTest(unittest.TestCase):
    def setUp(self):
        import os

        self._saved_env = {
            key: os.environ.get(key)
            for key in ("YOUVERSION_ACCESS_TOKEN", "YOUVERSION_SESSION_COOKIE")
        }
        # Keep load_env from filling these from the real .env during CLI tests.
        os.environ["YOUVERSION_ACCESS_TOKEN"] = ""
        os.environ["YOUVERSION_SESSION_COOKIE"] = ""

    def tearDown(self):
        import os

        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_session_json_keeps_token_and_user_id_only(self):
        pasted = json.dumps({
            "accessToken": fake_jwt("42"),
            "user": {"id": 42, "email": "secret@example.com", "name": "Secret Name"},
        })
        token, user_id = parse_session_text(pasted)
        root = Path(tempfile.mkdtemp())
        save_session(root / "session.json", token, user_id)
        saved = (root / "session.json").read_text(encoding="utf-8")
        self.assertEqual(user_id, "42")
        self.assertNotIn("secret@example.com", saved)
        self.assertNotIn("Secret Name", saved)
        self.assertNotIn("password", saved.lower())
        self.assertIn(token, saved)

    def test_cookie_and_error_payloads_are_refused(self):
        with self.assertRaises(AuthError) as cookie:
            parse_session_text("yva_session=not-the-api-token-value")
        self.assertIn("/api/authenticate", str(cookie.exception))
        with self.assertRaises(AuthError) as missing:
            parse_session_text('{"error":"Unauthenticated","reason":"No session cookie"}')
        self.assertIn("accessToken", str(missing.exception))
        self.assertNotIn("add a password", SIGN_IN_HELP)
        self.assertIn("/api/authenticate", SIGN_IN_HELP)
        self.assertIn("yva_session", SIGN_IN_HELP)

    def test_cookie_refreshes_an_expired_token_without_sending_it_as_a_bearer(self):
        import time

        root = Path(tempfile.mkdtemp())
        old = _jwt({"user_id": "7", "exp": int(time.time()) - 10})
        fresh = _jwt({"user_id": "7", "exp": int(time.time()) + 3600})
        save_session(root / "session.json", old, "7", session_cookie="browser-cookie-value")
        seen = {}

        def transport(method, url, headers, body):
            seen["method"] = method
            seen["url"] = url
            seen["cookie"] = headers.get("Cookie")
            seen["authorization"] = headers.get("Authorization")
            self.assertIsNone(body)
            payload = {"accessToken": fresh, "user": {"id": 7, "email": "secret@example.com"}}
            rotated = "yva_session=rotated-cookie-value; Path=/; HttpOnly"
            return 200, {"set-cookie": rotated}, json.dumps(payload).encode()

        from contextlib import redirect_stderr
        from io import StringIO

        with redirect_stderr(StringIO()):
            token, user_id, cookie = acquire_login(root / "session.json", transport, prompt=False)
        self.assertEqual((token, user_id, cookie), (fresh, "7", "rotated-cookie-value"))
        self.assertEqual(seen["method"], "GET")
        self.assertIn("/api/authenticate", seen["url"])
        self.assertEqual(seen["cookie"], "yva_session=browser-cookie-value")
        self.assertIsNone(seen["authorization"])
        saved = (root / "session.json").read_text(encoding="utf-8")
        self.assertIn("rotated-cookie-value", saved)
        self.assertNotIn("browser-cookie-value", saved)
        self.assertNotIn("secret@example.com", saved)
        self.assertFalse(access_token_needs_refresh(fresh, now=time.time()))

    def test_a_fresh_token_does_not_call_bible_com(self):
        import time

        root = Path(tempfile.mkdtemp())
        fresh = _jwt({"user_id": "7", "exp": int(time.time()) + 10 * 3600})
        save_session(root / "session.json", fresh, "7", session_cookie="stay-cookie-value")

        def transport(method, url, headers, body):
            raise AssertionError("refresh was not needed")

        token, user_id, cookie = acquire_login(root / "session.json", transport, prompt=False)
        self.assertEqual(token, fresh)
        self.assertEqual(user_id, "7")
        self.assertEqual(cookie, "stay-cookie-value")

    def test_refresh_auth_uses_the_cookie_and_does_not_download(self):
        import time

        root = Path(tempfile.mkdtemp())
        fresh = _jwt({"user_id": "7", "exp": int(time.time()) + 96 * 3600})
        save_session(root / "session.json", fake_jwt("7"), "7", session_cookie="saved-cookie-value")
        calls = []

        def transport(method, url, headers, body):
            calls.append((method, url.split("?", 1)[0], body, headers.get("Cookie")))
            payload = {"accessToken": fresh, "user": {"id": 7, "email": "secret@example.com"}}
            return 200, {}, json.dumps(payload).encode()

        token, user_id, cookie = refresh_saved_session(root / "session.json", transport)
        self.assertEqual(token, fresh)
        self.assertEqual(user_id, "7")
        self.assertEqual(cookie, "saved-cookie-value")
        self.assertEqual(calls, [("GET", "https://www.bible.com/api/authenticate", None, "yva_session=saved-cookie-value")])
        saved = (root / "session.json").read_text(encoding="utf-8")
        self.assertNotIn("secret@example.com", saved)
        self.assertIn("saved-cookie-value", saved)

        empty = Path(tempfile.mkdtemp())
        save_session(empty / "session.json", fresh, "7")
        with self.assertRaises(AuthError) as caught:
            refresh_saved_session(empty / "session.json", transport)
        self.assertIn("cannot be refreshed", str(caught.exception))
        self.assertEqual(len(calls), 1)

    def test_rejected_cookie_is_not_included_in_the_error(self):
        secret = "super-secret-cookie-value"

        def transport(method, url, headers, body):
            self.assertEqual(method, "GET")
            return 401, {}, b'{"error":"Unauthenticated","reason":"No session cookie"}'

        with self.assertRaises(AuthError) as caught:
            exchange_session_cookie(secret, transport)
        self.assertNotIn(secret, str(caught.exception))
        self.assertIn("yva_session", str(caught.exception))

    def test_web_headers_are_the_default(self):
        import os

        saved = os.environ.pop("YOUVERSION_APP_PLATFORM", None)
        try:
            headers = app_headers()
        finally:
            if saved is not None:
                os.environ["YOUVERSION_APP_PLATFORM"] = saved
        self.assertEqual(headers["X-YouVersion-App-Platform"], "web")
        self.assertEqual(headers["X-YouVersion-Client"], "youversion")
        self.assertNotIn("android", headers["Referer"])

    def test_rejected_token_is_not_called_a_password_problem(self):
        def transport(method, url, headers, body):
            self.assertIsNone(body)
            self.assertTrue(headers["Authorization"].startswith("Bearer "))
            return 401, {}, b'{"error":"access_denied"}'

        with self.assertRaises(AuthError) as caught:
            check_token("x" * 30, "1", transport=transport)
        message = str(caught.exception).lower()
        self.assertNotIn("password", message)
        self.assertNotIn("google", message)
        self.assertIn("/api/authenticate", str(caught.exception))

    def test_login_command_stores_the_token_without_the_password_or_email(self):
        import os

        from bible_archive.cli import main

        root = Path(tempfile.mkdtemp())
        token = fake_jwt("7")
        previous = {
            key: os.environ.get(key) for key in ("YOUVERSION_ACCESS_TOKEN", "YOUVERSION_PASSWORD")
        }
        os.environ["YOUVERSION_ACCESS_TOKEN"] = json.dumps({
            "accessToken": token,
            "user": {"id": 7, "email": "secret@example.com"},
        })
        os.environ["YOUVERSION_PASSWORD"] = "super-secret-password"
        try:
            from io import StringIO
            from contextlib import redirect_stdout

            with redirect_stdout(StringIO()):
                code = main(["--data", str(root), "login", "--no-check"])
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        self.assertEqual(code, 0)
        saved = (root / "session.json").read_text(encoding="utf-8")
        self.assertEqual(json.loads(saved)["user_id"], "7")
        self.assertIn(token, saved)
        self.assertNotIn("secret@example.com", saved)
        self.assertNotIn("super-secret-password", saved)
        mode = (root / "session.json").stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_retries_429_then_reads(self):
        sleeps = []
        calls = {"n": 0}

        def transport(method, url, headers, body):
            calls["n"] += 1
            if calls["n"] == 1:
                return 429, {"retry-after": "2"}, b""
            return 200, {}, b'{"moments":[{"id":7,"kind_id":"highlight"}]}'

        client = YouVersionClient(transport, token="token", user_id="42", delay=0, sleep=sleeps.append)
        payload = client.moments_page("highlight", 1)
        self.assertEqual(payload["moments"][0]["id"], 7)
        self.assertEqual(sleeps, [2.0])

    def test_expired_token_asks_for_a_new_session(self):
        calls = []
        fresh = fake_jwt()

        def transport(method, url, headers, body):
            calls.append((method, url.split("?", 1)[0], body))
            if headers.get("Authorization") != f"Bearer {fresh}":
                return 401, {}, b"{}"
            return 200, {}, b'{"moments":[]}'

        def sign_in():
            return fresh, "42"

        client = YouVersionClient(transport, token="stale", user_id="1", delay=0, sleep=lambda _seconds: None, login=sign_in)
        self.assertEqual(client.moments_page("note", 1), {"moments": []})
        self.assertTrue(all(method == "GET" and body is None for method, _url, body in calls))
        self.assertTrue(all("delete" not in url and "create" not in url for _method, url, _body in calls))


class SyncAccountTest(unittest.TestCase):
    def test_sync_is_read_only_resumable_and_searchable(self):
        root = Path(tempfile.mkdtemp())
        requests = []

        def transport(method, url, headers, body):
            parsed = urlparse(url)
            requests.append((method, parsed.path, parse_qs(parsed.query)))
            self.assertNotIn("delete", parsed.path)
            self.assertNotIn("create", parsed.path)
            if parsed.path.endswith("/labels.json"):
                return 200, {}, b'{"labels":[{"id":3,"name":"Sunday"}]}'
            if parsed.path.endswith("/saved_items.json"):
                return 404, {}, b'{"error":"missing"}'
            if parsed.path.endswith("/version.json"):
                return 200, {}, b'{"abbreviation":"NIV","title":"New International Version"}'
            kind = parse_qs(parsed.query).get("kind", [""])[0]
            page = parse_qs(parsed.query).get("page", ["1"])[0]
            if page != "1":
                return 200, {}, b'{"moments":[]}'
            if kind == "highlight":
                body = {"moments": [{
                    "id": "9",
                    "kind_id": "highlight",
                    "color": "ffff00",
                    "created_dt": "2020-01-02T00:00:00Z",
                    "references": [{"human": "Romans 8:1", "usfm": "ROM.8.1", "version_id": 111}],
                }]}
            elif kind == "note":
                body = {"moments": [{
                    "kind_id": "note.v1",
                    "object": {
                        "id": 55,
                        "title": "Abide",
                        "content": "Sermon: the vine and the branches",
                        "created_dt": "2016-05-02T18:30:00+00:00",
                        "references": [{"human": "John 15:1", "usfm": ["JHN.15.1"], "version_id": 111}],
                        "labels": [3],
                        "status": "private",
                    },
                }]}
            else:
                body = {"moments": []}
            return 200, {}, json.dumps(body).encode()

        client = YouVersionClient(transport, token=fake_jwt(), user_id="42", delay=0, sleep=lambda _seconds: None)
        logs = []
        first = sync_account(client, root, kinds=["highlight", "note", "event"], log=logs.append)
        second = sync_account(client, root, kinds=["highlight", "note", "event"], log=logs.append)
        highlight_page_one = [
            query for method, path, query in requests
            if method == "GET" and path.endswith("/items.json") and query.get("kind") == ["highlight"] and query.get("page") == ["1"]
        ]
        self.assertEqual(len(highlight_page_one), 1)
        self.assertTrue(all(method == "GET" for method, _path, _query in requests))
        self.assertEqual(first["counts"]["note"], 1)
        self.assertEqual(second["counts"]["highlight"], 1)
        self.assertTrue(any("events skipped" in line for line in logs))

        catalog = Catalog(root / "catalog.sqlite")
        try:
            found = catalog.search(terms=["vine"], start="2016-05-01", end="2016-06-01")
            self.assertEqual(found["total"], 1)
            self.assertEqual(found["items"][0]["labels"], "Sunday")
            self.assertEqual(found["items"][0]["versions"][0]["abbreviation"], "NIV")
            self.assertNotIn("super-secret-password", (root / "catalog.sqlite").read_bytes().decode("utf-8", "ignore"))
        finally:
            catalog.close()

    def test_saved_event_notes_are_indexed(self):
        root = Path(tempfile.mkdtemp())

        class Stub:
            def __init__(self):
                self.views = []
                self.pages = []

            def saved_events_page(self, page):
                self.pages.append(page)
                if page == 1:
                    return {"events": [{"id": 9, "title": "Sunday gathering", "user_note": "list note"}]}
                return {"events": []}

            def event_view(self, event_id):
                self.views.append(event_id)
                return {
                    "id": event_id,
                    "title": "Sunday gathering",
                    "start_dt": "2020-03-01T15:00:00+00:00",
                    "content": "Church outline",
                    "modules": [{"user_note": "Remember the vine"}],
                }

            def bible_version(self, version_id):
                return {"abbreviation": "NIV", "title": "New International Version"}

        stub = Stub()
        sync_account(stub, root, kinds=["event"], log=lambda _message: None)
        sync_account(stub, root, kinds=["event"], log=lambda _message: None)
        self.assertEqual(stub.views, ["9"])
        self.assertEqual(stub.pages.count(1), 1)
        catalog = Catalog(root / "catalog.sqlite")
        try:
            found = catalog.search(terms=["Remember the vine"])
            self.assertEqual(found["total"], 1)
            self.assertNotIn("Church outline", found["items"][0]["content"])
            self.assertNotIn("list note", found["items"][0]["content"])
        finally:
            catalog.close()


if __name__ == "__main__":
    unittest.main()
