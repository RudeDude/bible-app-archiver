import unittest

from bible_archive.normalize import (
    extract_items,
    normalize_datetime,
    normalize_event,
    normalize_moment,
    parse_terms,
    version_fields,
)


class ParseTermsTest(unittest.TestCase):
    def test_words_and_phrases(self):
        self.assertEqual(parse_terms('  vine "the branches" vine  '), ["the branches", "vine"])
        self.assertEqual(parse_terms("   "), [])


class NormalizeTest(unittest.TestCase):
    def test_nested_note(self):
        row = normalize_moment(
            {
                "kind_id": "note.v1",
                "object": {
                    "id": 55,
                    "title": "Abide",
                    "content": "Sermon: the vine and the branches",
                    "created_dt": "2016-05-02T18:30:00.000000+00:00",
                    "updated_dt": "2016-05-03T01:00:00+00:00",
                    "references": [
                        {"human": "John 15:1", "usfm": ["JHN.15.1", "JHN.15.2"], "version_id": "111"}
                    ],
                    "labels": [{"id": 3, "name": "Sunday"}],
                    "color": "FFEE88",
                    "status": "private",
                },
            },
            "note",
            {"3": "Sunday"},
        )
        self.assertEqual(row["id"], "note:55")
        self.assertEqual(row["kind"], "note")
        self.assertEqual(row["title"], "Abide")
        self.assertIn("the vine", row["content"])
        self.assertEqual(row["created_at"], "2016-05-02T18:30:00")
        self.assertEqual(row["reference_human"], "John 15:1")
        self.assertEqual(row["usfm"], "JHN.15.1 JHN.15.2")
        self.assertEqual(row["version_ids"], ",111,")
        self.assertEqual(row["labels"], "Sunday")
        self.assertEqual(row["color"], "ffee88")
        self.assertIn("john 15:1", row["search_text"])

    def test_flat_highlight_and_wrapped_list(self):
        payload = {
            "response": {
                "data": {
                    "moments": [
                        {
                            "id": "9",
                            "kind_id": "highlight",
                            "color": "#FFFF00",
                            "created_dt": "2020-01-02T00:00:00Z",
                            "references": [{"human": "Romans 8:1", "usfm": "ROM.8.1", "version_id": 111}],
                        }
                    ]
                }
            }
        }
        items = extract_items(payload)
        self.assertEqual(len(items), 1)
        row = normalize_moment(items[0], "highlight")
        self.assertEqual(row["color"], "ffff00")
        self.assertEqual(row["created_at"], "2020-01-02T00:00:00")
        self.assertEqual(row["usfm"], "ROM.8.1")

    def test_event_keeps_typed_notes_only(self):
        row = normalize_event(
            {
                "id": 9,
                "title": "Sunday gathering",
                "start_dt": "2020-03-01T15:00:00+00:00",
                "content": "Church outline that is not my note",
                "modules": [
                    {"title": "Point 1", "body": "Pastor text", "user_note": "Remember the vine"},
                    {
                        "references": [{"human": "John 15:5", "usfm": ["JHN.15.5"], "version_id": 111}],
                        "notes": "abide",
                    },
                ],
                "comments": [
                    {"user_id": 5, "text": "Friend said hello"},
                    "my private margin",
                ],
            }
        )
        self.assertEqual(row["kind"], "event")
        self.assertIn("Remember the vine", row["content"])
        self.assertIn("abide", row["content"])
        self.assertIn("my private margin", row["content"])
        self.assertNotIn("Church outline", row["content"])
        self.assertNotIn("Pastor text", row["content"])
        self.assertNotIn("Friend said hello", row["content"])
        self.assertEqual(row["reference_human"], "John 15:5")
        self.assertEqual(row["created_at"], "2020-03-01T15:00:00")

    def test_version_fields_and_dates(self):
        abbreviation, title = version_fields(
            {"response": {"data": {"version": {"abbreviation": "NIV", "local_title": "New International Version"}}}}
        )
        self.assertEqual(abbreviation, "NIV")
        self.assertEqual(title, "New International Version")
        self.assertEqual(normalize_datetime(""), "")
        self.assertEqual(normalize_datetime(None), "")


if __name__ == "__main__":
    unittest.main()
