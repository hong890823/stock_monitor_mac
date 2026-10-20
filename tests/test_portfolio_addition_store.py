from datetime import datetime
from pathlib import Path
import json
import tempfile
import unittest

from portfolio_addition import FundamentalSnapshot
from portfolio_addition_store import PortfolioAdditionStore


class PortfolioAdditionStoreTests(unittest.TestCase):
    def test_store_round_trip_and_expiry(self):
        with tempfile.TemporaryDirectory() as directory:
            store = PortfolioAdditionStore(Path(directory) / "addition-cache.json")
            snapshot = FundamentalSnapshot(
                "2026-10-15", 80, "SOUND", 10, 12, .9, 30, 15, 1, 12,
                15, 2, 25, 25, 8, 12,
            )
            store.save("sh600001", snapshot)
            self.assertEqual(store.load("sh600001"), snapshot)
            self.assertTrue(store.is_fresh(snapshot, datetime(2026, 10, 20, 15)))
            self.assertFalse(store.is_fresh(snapshot, datetime(2026, 11, 16, 15)))

    def test_invalid_record_returns_none_and_sets_last_error(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "addition-cache.json"
            path.write_text(json.dumps({
                "version": 1,
                "records": {"sh600001": {"analyzed_at": "not-a-date"}},
            }), encoding="utf-8")
            store = PortfolioAdditionStore(path)
            self.assertIsNone(store.load("sh600001"))
            self.assertIn("缺少字段", store.last_error)

    def test_future_snapshot_is_not_fresh(self):
        snapshot = FundamentalSnapshot(
            "2026-10-16", 80, "SOUND", 10, 12, .9, 30, 15, 1, 12,
            15, 2, 25, 25, 8, 12,
        )
        store = PortfolioAdditionStore(Path("unused.json"))
        self.assertFalse(store.is_fresh(snapshot, datetime(2026, 10, 15, 15)))


if __name__ == "__main__":
    unittest.main()
