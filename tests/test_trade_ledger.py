import json
import tempfile
import unittest
from pathlib import Path

from trade_ledger import TradeLedger


class TradeLedgerTest(unittest.TestCase):
    def test_same_day_buy_is_not_sellable(self):
        ledger = TradeLedger()
        ledger.record_trade("buy", "sz000737", "北方铜业", "2026-10-20", 100, 12.13)

        self.assertEqual(ledger.sellable_quantity("sz000737", "2026-10-20"), 0)
        self.assertEqual(ledger.sellable_quantity("sz000737", "2026-10-21"), 100)

    def test_sell_cannot_exceed_same_day_sellable(self):
        ledger = TradeLedger()
        ledger.record_trade("buy", "sz000737", "北方铜业", "2026-10-19", 500, 12.50)
        ledger.record_trade("buy", "sz000737", "北方铜业", "2026-10-20", 100, 12.13)
        ledger.record_trade("sell", "sz000737", "北方铜业", "2026-10-20", 500, 12.20)

        with self.assertRaisesRegex(ValueError, "当日可卖"):
            ledger.record_trade("sell", "sz000737", "北方铜业", "2026-10-20", 100, 12.20)

    def test_summary_separates_today_locked_from_sellable(self):
        ledger = TradeLedger()
        ledger.record_trade("buy", "sz000737", "北方铜业", "2026-10-19", 500, 12.50)
        ledger.record_trade("buy", "sz000737", "北方铜业", "2026-10-20", 100, 12.13)

        summary = ledger.position_summary("sz000737", "2026-10-20")

        self.assertEqual(summary["quantity"], 600)
        self.assertEqual(summary["sellable_quantity"], 500)
        self.assertEqual(summary["today_locked_quantity"], 100)
        self.assertEqual(summary["next_sellable_quantity"], 600)

    def test_legacy_positions_are_sellable_immediately(self):
        ledger = TradeLedger()
        ledger.sync_legacy_positions([
            {"code": "sz000737", "name": "北方铜业", "quantity": 500, "cost": 16.71}
        ])

        summary = ledger.position_summary("sz000737", "2026-10-20")

        self.assertEqual(summary["quantity"], 500)
        self.assertEqual(summary["sellable_quantity"], 500)
        self.assertEqual(summary["today_locked_quantity"], 0)

    def test_json_round_trip_preserves_lots_and_transactions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trade_ledger.json"
            ledger = TradeLedger(path)
            ledger.record_trade("buy", "sz000737", "北方铜业", "2026-10-19", 500, 12.50)
            ledger.record_trade("buy", "sz000737", "北方铜业", "2026-10-20", 100, 12.13)

            loaded = TradeLedger(path)

            self.assertEqual(loaded.sellable_quantity("sz000737", "2026-10-20"), 500)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["version"], 1)


if __name__ == "__main__":
    unittest.main()
