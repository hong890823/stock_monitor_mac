import json
import tempfile
import unittest
from pathlib import Path

from trade_ledger import LedgerAdjustment, Trade, TradeLedger


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

    def test_sync_does_not_duplicate_adjustment_only_position(self):
        ledger = TradeLedger()
        ledger.record_adjustment(
            "sz002466", "天齐锂业", "2026-10-21", 300, 36.82, "manual_edit"
        )

        ledger.sync_legacy_positions([
            {"code": "sz002466", "name": "天齐锂业", "quantity": 300, "cost": 36.82}
        ])

        summary = ledger.position_summary("sz002466", "2026-10-23")
        self.assertEqual(summary["quantity"], 300)
        self.assertEqual(summary["sellable_quantity"], 300)

    def test_sync_repairs_existing_duplicate_adjustment_and_legacy_lots(self):
        ledger = TradeLedger()
        ledger.adjustments.append(LedgerAdjustment(
            adjustment_id="adjust-2026-10-21-sz002466-7",
            code="sz002466",
            name="天齐锂业",
            effective_date="2026-10-21",
            quantity_delta=300,
            cost=36.82,
            reason="manual_edit",
        ))
        ledger._apply_adjustment(ledger.adjustments[-1], save=False)
        legacy_trade = Trade(
            trade_id="legacy-sz002466",
            side="buy",
            code="sz002466",
            name="天齐锂业",
            trade_date="0001-01-01",
            quantity=300,
            price=36.82,
            source="legacy",
        )
        ledger._append_trade(legacy_trade, save=False)

        ledger.sync_legacy_positions([
            {"code": "sz002466", "name": "天齐锂业", "quantity": 300, "cost": 36.82}
        ])

        summary = ledger.position_summary("sz002466", "2026-10-23")
        self.assertEqual(summary["quantity"], 300)
        self.assertEqual(summary["sellable_quantity"], 300)
        self.assertTrue(summary["consistent"])

    def test_unexplained_quantity_mismatch_blocks_sellable_action(self):
        ledger = TradeLedger()
        ledger.record_trade(
            "buy", "sz002466", "天齐锂业", "2026-10-21", 600, 36.82
        )

        ledger.sync_legacy_positions([
            {"code": "sz002466", "name": "天齐锂业", "quantity": 300, "cost": 36.82}
        ])

        summary = ledger.position_summary("sz002466", "2026-10-23")
        self.assertFalse(summary["consistent"])
        self.assertEqual(summary["actionable_sellable_quantity"], 0)

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
