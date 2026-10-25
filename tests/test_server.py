import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from io import StringIO
from pathlib import Path
from contextlib import redirect_stdout

from bible_archive.cli import main
from bible_archive.normalize import normalize_moment
from bible_archive.server import CatalogServer
from bible_archive.store import Catalog


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        catalog = Catalog(self.root / "catalog.sqlite")
        catalog.replace_items(
            [
                normalize_moment(
                    {
                        "id": 55,
                        "kind_id": "note",
                        "title": "Abide",
                        "content": "Sermon: the vine and the branches",
                        "created_dt": "2016-05-02T18:30:00+00:00",
                        "labels": ["Sunday"],
                        "references": [{"human": "John 15:1", "usfm": ["JHN.15.1"], "version_id": 111}],
                    },
                    "note",
                )
            ]
        )
        catalog.save_version(111, "NIV", "New International Version", "{}")
        catalog.close()
        self.httpd = CatalogServer(("127.0.0.1", 0), self.root / "catalog.sqlite")
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=3)
        self.tmp.cleanup()

    def fetch(self, path):
        last_error = None
        for _ in range(20):
            try:
                with urllib.request.urlopen(self.base + path, timeout=2) as response:
                    return response.status, response.read()
            except urllib.error.HTTPError as error:
                return error.code, error.read()
            except urllib.error.URLError as error:
                last_error = error
                time.sleep(0.05)
        raise AssertionError(last_error)

    def test_page_search_and_month(self):
        status, html = self.fetch("/")
        self.assertEqual(status, 200)
        self.assertIn(b"Search notes", html)
        self.assertIn(b'id="clear"', html)
        status, body = self.fetch("/api/search?q=VINE")
        payload = json.loads(body)
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["items"][0]["reference_human"], "John 15:1")
        self.assertEqual(payload["terms"], ["VINE"])
        status, body = self.fetch("/api/search?month=2016-05&kind=note")
        self.assertEqual(json.loads(body)["total"], 1)
        status, body = self.fetch("/api/search?month=2017-01")
        self.assertEqual(json.loads(body)["total"], 0)
        status, body = self.fetch("/api/facets?q=vine")
        facets = json.loads(body)
        self.assertEqual(facets["kinds"]["note"], 1)
        self.assertEqual(facets["versions"][0]["abbreviation"], "NIV")
        self.assertEqual(facets["months"][0]["month"], "2016-05")
        status, body = self.fetch("/api/search?month=2016-13")
        self.assertEqual(status, 400)

    def test_feed_cards_are_left_out_of_the_web_ui(self):
        catalog = Catalog(self.root / "catalog.sqlite")
        catalog.replace_items(
            [
                normalize_moment(
                    {
                        "id": 9,
                        "kind_id": "highlight",
                        "created_dt": "2020-01-02T00:00:00Z",
                        "references": [{"human": "Romans 8:1", "usfm": ["ROM.8.1"], "version_id": 111}],
                    },
                    "highlight",
                ),
                normalize_moment(
                    {
                        "id": 343,
                        "kind_id": "reading_plan_carousel.v1",
                        "title": "A reading plan",
                        "created_dt": "2024-02-01T00:00:00Z",
                        "references": [{"human": "John 1:1", "usfm": ["JHN.1.1"], "version_id": 999}],
                    },
                    "client_side",
                ),
                normalize_moment(
                    {
                        "id": 395,
                        "kind_id": "friend_suggestions.v1",
                        "title": "People to follow",
                        "created_dt": "2024-02-02T00:00:00Z",
                    },
                    "client_side",
                ),
            ]
        )
        catalog.close()
        status, body = self.fetch("/api/search")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["items"][0]["kind"], "highlight")
        status, body = self.fetch("/api/search?q=follow")
        self.assertEqual(json.loads(body)["total"], 0)
        status, body = self.fetch("/api/search?kind=friend_suggestions")
        self.assertEqual(json.loads(body)["total"], 0)
        status, body = self.fetch("/api/search?kind=reading_plan_carousel")
        self.assertEqual(json.loads(body)["total"], 0)
        status, body = self.fetch("/api/facets")
        facets = json.loads(body)
        self.assertEqual(list(facets["kinds"]), ["highlight"])
        self.assertEqual([entry["month"] for entry in facets["months"]], ["2020-01"])
        self.assertEqual([row["version_id"] for row in facets["versions"]], [111])

    def test_oldest_sort_reverses_the_month_list(self):
        catalog = Catalog(self.root / "catalog.sqlite")
        catalog.replace_items(
            [
                normalize_moment(
                    {
                        "id": 55,
                        "kind_id": "note",
                        "content": "Sermon: the vine and the branches",
                        "created_dt": "2016-05-02T18:30:00+00:00",
                        "references": [{"human": "John 15:1", "usfm": ["JHN.15.1"], "version_id": 111}],
                    },
                    "note",
                ),
                normalize_moment(
                    {
                        "id": 80,
                        "kind_id": "highlight",
                        "created_dt": "2020-01-02T00:00:00Z",
                        "references": [{"human": "Romans 8:1", "usfm": ["ROM.8.1"], "version_id": 111}],
                    },
                    "highlight",
                ),
            ]
        )
        catalog.close()
        status, body = self.fetch("/api/facets?sort=oldest")
        self.assertEqual(status, 200)
        self.assertEqual([entry["month"] for entry in json.loads(body)["months"]], ["2016-05", "2020-01"])
        status, body = self.fetch("/api/facets?sort=newest")
        self.assertEqual([entry["month"] for entry in json.loads(body)["months"]], ["2020-01", "2016-05"])

    def test_cli_search_export_and_import(self):
        stdout = StringIO()
        with redirect_stdout(stdout):
            code = main(["--data", str(self.root), "search", "vine", "--month", "2016-05"])
        self.assertEqual(code, 0)
        self.assertIn("John 15:1", stdout.getvalue())
        self.assertIn("the vine", stdout.getvalue())

        export_dir = self.root / "out"
        with redirect_stdout(StringIO()):
            code = main(["--data", str(self.root), "export", "--dest", str(export_dir)])
        self.assertEqual(code, 0)
        exported = json.loads((export_dir / "catalog.json").read_text(encoding="utf-8"))
        self.assertEqual(exported[0]["content"], "Sermon: the vine and the branches")
        self.assertIn("Sunday", (export_dir / "catalog.csv").read_text(encoding="utf-8"))

        other = self.root / "imported"
        source = self.root / "extra.json"
        source.write_text(
            json.dumps({"moments": [{"id": "2", "kind_id": "highlight", "content": "marked mercy", "created_dt": "2018-02-02T00:00:00Z", "references": [{"human": "Micah 6:8", "usfm": ["MIC.6.8"], "version_id": 111}]}]}),
            encoding="utf-8",
        )
        with redirect_stdout(StringIO()):
            code = main(["--data", str(other), "import-json", str(source), "--kind", "highlight"])
        self.assertEqual(code, 0)
        stdout = StringIO()
        with redirect_stdout(stdout):
            main(["--data", str(other), "search", "mercy"])
        self.assertIn("Micah 6:8", stdout.getvalue())
        peek = StringIO()
        with redirect_stdout(peek):
            main(["--data", str(other), "peek"])
        self.assertIn("items=1", peek.getvalue())
        self.assertNotIn("marked mercy", peek.getvalue())


if __name__ == "__main__":
    unittest.main()
