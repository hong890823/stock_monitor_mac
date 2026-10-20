from copy import deepcopy
from datetime import datetime
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from entry_review_store import EntryReviewStore, event_key
from tests.test_entry_review import sample_event, histories


class EntryReviewStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "review.sqlite3"
        self.store = EntryReviewStore(self.path)
        self.addCleanup(self.store.close)
        self.event = sample_event()

    def review(self, stock=None, benchmark=None):
        defaults = histories()
        return self.store.review(event_key(self.event), stock or defaults[0],
                                 benchmark or defaults[1], datetime(2026, 11, 2, 18))

    def test_repeated_event_keeps_first_evidence_and_one_position(self):
        self.store.record_event(self.event)
        duplicate = deepcopy(self.event)
        duplicate["price"] = 10.2
        self.store.record_event(duplicate)
        rows = self.store.events()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event"]["price"], 10)
        self.review()
        self.review()
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM reviews").fetchone()[0], 1)
        self.assertEqual(len(self.store.events()[0]["review"]["simulation"]["trades"]), 2)

    def test_round_and_day_are_independent_events(self):
        self.store.record_event(self.event)
        next_round = deepcopy(self.event)
        next_round["first_observed_date"] = "2026-10-13"
        self.store.record_event(next_round)
        next_day = deepcopy(self.event)
        next_day.update(date="2026-10-15", at="2026-10-15T10:00:00")
        self.store.record_event(next_day)
        self.assertEqual(len(self.store.events()), 3)

    def test_failed_delivery_is_stored_and_later_success_is_not_downgraded(self):
        key = self.store.record_event(self.event)
        self.store.record_delivery(key, False, self.event["at"])
        self.assertEqual(self.store.events()[0]["delivery"], "failed")
        self.store.record_delivery(key, True, "2026-10-14T10:03:00")
        self.store.record_delivery(key, False, "2026-10-14T10:04:00")
        self.assertEqual(self.store.events()[0]["delivery"], "sent")

    def test_restart_restores_events_and_policy(self):
        self.store.record_event(self.event)
        self.review()
        with EntryReviewStore(self.path) as reopened:
            self.assertEqual(reopened.events()[0]["event"], self.event)
            self.assertEqual(reopened.events()[0]["review"]["status"], "complete")
            self.assertEqual(reopened.pending_events(), [])

    def test_eliminated_pool_record_is_not_required_for_review(self):
        self.store.record_event(self.event)
        self.assertEqual(len(self.store.pending_events()), 1)
        self.review()
        self.assertEqual(self.store.events()[0]["review"]["simulation"]["status"], "closed")

    def test_changed_historical_bars_do_not_rewrite_prior_result(self):
        self.store.record_event(self.event)
        self.review()
        before = self.store.events()[0]["review"]
        stock, benchmark = histories()
        stock[1]["close"] = 10.3
        with self.assertRaisesRegex(ValueError, "日线变更"):
            self.review(stock, benchmark)
        self.assertEqual(self.store.events()[0]["review"], before)

    def test_truncated_new_fetch_keeps_previously_saved_sessions(self):
        self.store.record_event(self.event)
        stock, benchmark = histories()
        self.store.review(event_key(self.event), stock[:3], benchmark[:3], datetime(2026, 10, 16, 18))
        result = self.store.review(event_key(self.event), stock[3:], benchmark[3:], datetime(2026, 11, 2, 18))
        self.assertEqual(result["horizons"]["1"]["date"], "2026-10-15")
        self.assertEqual(result["status"], "complete")
        self.assertEqual(self.store.connection.execute("SELECT count(*) FROM reviews").fetchone()[0], 2)

    def test_corrupt_db_is_not_replaced(self):
        path = Path(self.directory.name) / "broken.sqlite3"
        path.write_bytes(b"broken ledger")
        with self.assertRaises(sqlite3.DatabaseError):
            EntryReviewStore(path)
        self.assertEqual(path.read_bytes(), b"broken ledger")

    def test_nonfinite_event_is_not_written(self):
        self.event["evidence"]["net_yi"] = float("nan")
        with self.assertRaises(ValueError):
            self.store.record_event(self.event)
        self.assertEqual(self.store.events(), [])

    def test_report_excludes_discontinuous_prices_from_valid_samples(self):
        self.store.record_event(self.event)
        stock, benchmark = histories()
        for daily in stock[2:]:
            daily.update(open=5, high=5.5, low=4.5, close=5)
        self.review(stock, benchmark)
        report = self.store.report()
        self.assertIn("10日有效0/1", report)
        self.assertIn("价格口径待核对", report)
        self.assertNotIn("-50.00%", report)

    def test_report_includes_denominators_and_never_calls_independent_events_a_portfolio(self):
        self.store.record_event(self.event)
        self.review()
        event = deepcopy(self.event)
        event.update(code="sh600002", sources=["strict", "relaxed"])
        self.store.record_event(event)
        report = self.store.report()
        self.assertIn("总事件2", report)
        self.assertIn("10日有效1/2", report)
        self.assertIn("独立样本，不是共享资金组合收益", report)
        self.assertIn("日线假设成交", report)
        self.assertIn("严格+宽松", report)
        self.assertIn("待复盘", report)
        self.assertIn("上证指数", report)


if __name__ == "__main__":
    unittest.main()
