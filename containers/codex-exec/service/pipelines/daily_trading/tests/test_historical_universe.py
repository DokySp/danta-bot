import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from ..scripts import historical_universe as universe


def export(codes):
    return "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in
                   ("주권", "current market", "current name", code, "2026-01-01", "123")) + "</tr>" for code in codes)


def summary():
    return "".join(f"<tr><td>1</td><td onclick=\"detailView('{market}', 'ST', '1')\"></td></tr>" for market in universe.MARKETS)


class FakeClient(universe.KindClient):
    def fetch(self, path, params):
        self.request_count += 1
        body = summary() if path == universe.STATUS else export(["005930" if params["mktId"] == "STK" else "12345A"])
        return body, {"url": universe.BASE + path, "response_sha256": "a" * 64, "selDate": params["selDate"]}


class HistoricalUniverseTest(unittest.TestCase):
    def test_count_alphanumeric_and_mutable_attributes(self):
        self.assertEqual(universe.company_counts(summary()), {"STK": 1, "KSQ": 1})
        self.assertEqual(universe.membership_codes(export(["12345A"]), 1), ["12345A"])
        for codes, expected in ((["12345A"], 2), (["005930", "005930"], 2), (["12345a"], 1), (["00593"], 1)):
            with self.assertRaises(ValueError):
                universe.membership_codes(export(codes), expected)
        with self.assertRaises(ValueError):
            universe.company_counts(summary() + summary())

    def test_calendar_and_resumable_normalized_membership(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            calendar = root / "calendar.json"
            calendar.write_text(json.dumps({"trading_dates": ["2020-01-02", "2020-01-03"]}))
            days, digest = universe.load_calendar(calendar)
            path = root / "universe.sqlite3"
            universe.initialize(path, days, digest)
            with patch("builtins.print"):
                initial = universe.collect(path, days, FakeClient(), workers=1, limit_days=1)
                resumed = universe.collect(path, days, FakeClient(), workers=2)
                cached = universe.collect(path, days, FakeClient())
            self.assertEqual(initial["status"], "incomplete")
            self.assertEqual(resumed["status"], "complete")
            self.assertEqual(resumed["requests_this_run"], 3)
            self.assertEqual(cached["requests_this_run"], 0)
            universe.initialize(path, days, digest)
            with universe.open_database(path) as db:
                self.assertEqual([row[1] for row in db.execute("PRAGMA table_info(universe_membership)")],
                                 ["snapshot_date", "market", "code"])
                self.assertEqual(db.execute("SELECT COUNT(*) FROM universe_membership").fetchone()[0], 4)
                db.execute("DELETE FROM universe_membership WHERE code='005930'")
            with self.assertRaisesRegex(ValueError, "saved membership"):
                universe.initialize(path, days, digest)
            calendar.write_text('["2020-01-03", "2020-01-02"]')
            with self.assertRaises(ValueError):
                universe.load_calendar(calendar)
            calendar.write_text('["2020-01-02", "2020-01-02"]')
            with self.assertRaises(ValueError):
                universe.load_calendar(calendar)
            calendar.write_text('["2026-01-02"]')
            with self.assertRaises(ValueError):
                universe.load_calendar(calendar)

    def test_overlap_rolls_back_and_saved_first_market_survives_failure(self):
        class OverlapClient(FakeClient):
            def fetch(self, path, params):
                body, source = super().fetch(path, params)
                return (export(["005930"]) if path == universe.DETAIL else body), source
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "universe.sqlite3"
            days = ["2020-01-02"]
            universe.initialize(path, days, "hash")
            with patch("builtins.print"):
                failed = universe.collect(path, days, OverlapClient(), workers=1)
            self.assertEqual(failed["snapshot_status_counts"], {"complete": 1, "failed": 1})
            self.assertEqual(failed["membership_rows"], 1)
            with patch("builtins.print"):
                resumed = universe.collect(path, days, FakeClient(), workers=1)
            self.assertEqual(resumed["status"], "complete")
            self.assertEqual(resumed["requests_this_run"], 2)
            with self.assertRaisesRegex(ValueError, "calendar differs"):
                universe.initialize(path, days, "different")
            with universe.open_database(path) as db:
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute("INSERT INTO universe_membership VALUES ('2020-01-02','STK','12345a')")

    def test_rate_block_stops_without_retry(self):
        for status in (403, 429):
            client = universe.KindClient(interval=0)
            with patch.object(universe, "urlopen", side_effect=HTTPError("", status, "blocked", {}, None)) as request:
                with self.assertRaisesRegex(RuntimeError, "no retry"):
                    client.fetch(universe.STATUS, {})
                self.assertEqual(request.call_count, 1)
                self.assertTrue(client.stopped.is_set())
        client = universe.KindClient(interval=0)
        with patch.object(universe, "urlopen", side_effect=TimeoutError), patch.object(universe.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "3 attempts"):
                client.fetch(universe.STATUS, {})
            self.assertEqual(client.request_count, 3)


if __name__ == "__main__":
    unittest.main()
