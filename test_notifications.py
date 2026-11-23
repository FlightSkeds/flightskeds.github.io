"""Regression checks with archived CNATRA data and an in-memory push transport."""
import csv
import gzip
import io
import tempfile
import unittest
from collections import Counter
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

import fetch_schedules as fetch
import notify_schedules as push
from notification_schedule import personal, read_schedule


class Notifications(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.day = "2026-11-19"
        cls.pdf = (push.ROOT / "schedules/VT-3" / cls.day / "original_schedule.pdf").read_bytes()
        cls.schedule = read_schedule(cls.pdf)
        cls.bailey = personal(cls.schedule, "LOGAN", "BAILEY", False)
        cls.event = next(e for e in cls.bailey if e.label == "FAM4204")

    def test_archive_extraction(self):
        parsed, text = fetch.extract_pdf(self.pdf)
        self.assertIn("Ground Events", text)
        self.assertTrue(parsed["tables"])
        self.assertEqual(Counter(e.section for e in self.schedule[0]),
                         {"Flight": 38, "Simulator": 40, "Duty": 11, "Ground": 26})
        self.assertEqual(len(self.bailey), 4)
        self.assertIn(("SCHNEIDER", "ETHAN"), self.event.crew)
        singles = Counter(person for e in self.schedule[0] for person in set(e.crew))
        last, first = next(k for k, n in singles.items() if n == 1 and len(k[1]) > 1)
        one = personal(self.schedule, first, last, False)
        self.assertIn("1 event:", push.message(self.day, None, one, False)[1])
        self.assertIn("4 events:", push.message(self.day, None, self.bailey, False)[1])

    def test_absent_and_unmatched(self):
        for known, expected in [(True, "No Events"), (False, "Schedule Posted")]:
            after = personal(self.schedule, "TEST", "ABSENT", known)
            self.assertEqual(push.message(self.day, None, after, False)[0], expected)
        self.assertIsNone(personal(self.schedule, "", "BAILEY", True))
        unreadable = read_schedule((push.ROOT / "schedules/VT-3/2026-11-24/original_schedule.pdf").read_bytes())
        self.assertIsNone(personal(unreadable, "LOGAN", "BAILEY", True))

    def test_revisions(self):
        e = self.event
        for before, after, expected in [
            ([], [e], "added"), ([e], [], "removed"), ([e], [replace(e, start="1430")], "to 1430"),
            ([e], [replace(e, crew=e.crew[:1])], "crew changed"),
            ([e], [replace(e, label="F4202")], "changed to F 4202"),
            ([e], [replace(e, formation="LD VS WHEELER")], "formation changed"),
        ]:
            self.assertIn(expected, push.message(self.day, before, after, True)[1])
        self.assertIsNone(push.message(self.day, self.bailey, self.bailey[::-1], True))
        self.assertIsNone(push.message(self.day, [], [], True))
        self.assertIsNone(push.message(self.day, None, None, True))

    def test_private_targets_and_retry(self):
        path = f"schedules/VT-3/{self.day}/full_schedule.json"
        tags = {"squadron": "VT-3", "first": "LOGAN", "last": "BAILEY"}
        devices = {"a": tags, "b": tags, "c": dict(tags, squadron="VT-6")}
        with patch.object(push, "git", side_effect=[b"changed", self.pdf, b""]*2), \
             patch.object(push, "api", return_value={"id": "accepted"}) as send:
            for _ in range(2):
                push.dispatch("revision", path, devices)
        bodies = [call.args[1] for call in send.call_args_list]
        self.assertEqual([b["include_subscription_ids"] for b in bodies], [["a"], ["b"]]*2)
        self.assertEqual(bodies[:2], bodies[2:])
        self.assertNotEqual(bodies[0]["idempotency_key"], bodies[1]["idempotency_key"])
        self.assertEqual(bodies[0]["url"], "https://flightskeds.github.io/?squadron=VT-3&date="+self.day)

    def test_export_filters(self):
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["id", "tags", "device_type", "invalid_identifier", "notification_types"])
        for row in [("active", 5, "f", 1), ("expired", 5, "t", 1), ("off", 5, "f", -2), ("email", 11, "f", 1)]:
            writer.writerow([row[0], '{"squadron":"VT-3"}', *row[1:]])
        with patch.object(push, "api", return_value={"csv_file_url": "https://example.test/export"}), \
             patch.object(push.requests, "get", return_value=Mock(status_code=200,
                          content=gzip.compress(buf.getvalue().encode()))):
            self.assertEqual(list(push.subscriptions()), ["active"])

    def test_checkpoint(self):
        with tempfile.TemporaryDirectory() as temp:
            cursor = Path(temp) / "cursor"
            cursor.write_text("old")
            with patch.object(push, "CURSOR", cursor), patch.object(push, "subscriptions", return_value={}), \
                 patch.object(push, "git", side_effect=[b"new", b"path", b"new", b"path", b""]), \
                 patch.object(push, "dispatch", side_effect=[RuntimeError(), None]) as deliver:
                with self.assertRaises(RuntimeError):
                    push.main()
                self.assertEqual(cursor.read_text(), "old")
                push.main()
                push.main()
                self.assertEqual(cursor.read_text().strip(), "new")
                self.assertEqual(deliver.call_count, 2)

    def test_friday_and_holiday_window(self):
        now = datetime(2026, 11, 20, 13, 8, tzinfo=fetch.TZ)
        days = fetch.target_dates(now, "")
        self.assertEqual(len(days), 8)
        self.assertTrue(all(fetch.should_check_date(now, day, "") for day in days))
        self.assertIn("2026-11-26", days)
        self.assertTrue(fetch.should_check_date(now.replace(minute=38), "2026-11-23", ""))


if __name__ == "__main__":
    unittest.main()
