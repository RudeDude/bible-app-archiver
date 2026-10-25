import tempfile
import unittest
from pathlib import Path

from bible_archive.normalize import normalize_moment
from bible_archive.store import Catalog


def note(content, created, reference="John 15:1", version=111):
    return normalize_moment(
        {
            "id": content,
            "kind_id": "note",
            "content": content,
            "created_dt": created,
            "references": [{"human": reference, "usfm": ["JHN.15.1"], "version_id": version}],
        },
        "note",
    )


class StoreSearchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.catalog = Catalog(Path(self.tmp.name) / "catalog.sqlite")
        self.catalog.replace_items(
            [
                note("Sermon: the vine and the branches", "2016-05-02T18:30:00+00:00"),
                note("100% grace_alone", "2020-01-02T00:00:00Z", "Romans 8:1", 59),
                normalize_moment(
                    {
                        "id": "old",
                        "kind_id": "highlight",
                        "color": "ffff00",
                        "created_dt": "2014-08-01T12:00:00+00:00",
                        "references": [{"human": "Psalm 23:1", "usfm": ["PSA.23.1"], "version_id": 111}],
                    },
                    "highlight",
                ),
            ]
        )
        self.catalog.save_version(111, "NIV", "New International Version", "{}")

    def tearDown(self):
        self.catalog.close()
        self.tmp.cleanup()

    def test_substring_keyword_phrase_and_wildcards(self):
        vine = self.catalog.search(terms=["SERMON"])
        self.assertEqual(vine["total"], 1)
        both = self.catalog.search(terms=["vine", "branches"])
        self.assertEqual(both["total"], 1)
        missing = self.catalog.search(terms=["vine", "missing"])
        self.assertEqual(missing["total"], 0)
        phrase = self.catalog.search(terms=["the vine"])
        self.assertEqual(phrase["total"], 1)
        not_a_phrase = self.catalog.search(terms=["vine branches"])
        self.assertEqual(not_a_phrase["total"], 0)
        percent = self.catalog.search(terms=["%"])
        self.assertEqual([item["content"] for item in percent["items"]], ["100% grace_alone"])
        underscore = self.catalog.search(terms=["grace_alone"])
        self.assertEqual(underscore["total"], 1)
        not_wildcard = self.catalog.search(terms=["graceXalone"])
        self.assertEqual(not_wildcard["total"], 0)

    def test_date_kind_and_version_filters(self):
        may = self.catalog.search(terms=[], start="2016-05-01", end="2016-06-01")
        self.assertEqual([item["id"] for item in may["items"]], ["note:Sermon: the vine and the branches"])
        highlights = self.catalog.search(terms=[], kinds=["highlight"])
        self.assertEqual(highlights["total"], 1)
        self.assertEqual(highlights["items"][0]["versions"][0]["abbreviation"], "NIV")
        niv = self.catalog.search(terms=[], version_id=111)
        self.assertEqual(niv["total"], 2)
        oldest = self.catalog.search(terms=[], sort="oldest")
        self.assertTrue(oldest["items"][0]["created_at"].startswith("2014"))
        months = self.catalog.facets(terms=[])["months"]
        self.assertEqual(months[0]["month"], "2020-01")
        self.assertIn("note", self.catalog.facets(terms=["vine"])["kinds"])


if __name__ == "__main__":
    unittest.main()
