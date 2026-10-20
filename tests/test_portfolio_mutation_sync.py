import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from stock_menubar import StockMenuBar
from trade_ledger import TradeLedger


class PortfolioMutationSyncTest(unittest.TestCase):
    @staticmethod
    def app(ledger):
        app = object.__new__(StockMenuBar)
        app._trade_ledger = ledger
        app.portfolio = {"positions": [{
            "code": "sz000737",
            "name": "北方铜业",
            "quantity": 600,
            "cost": 12.2,
        }]}
        app._save_portfolio = Mock(return_value=True)
        app._refresh_portfolio_update_menu = Mock()
        app._last_portfolio_analysis_slot = None
        app._maybe_refresh_portfolio_analysis = Mock()
        return app

    def test_manual_quantity_increase_today_is_t_plus_1(self):
        ledger = TradeLedger()
        ledger.record_trade("buy", "sz000737", "北方铜业", "2026-10-19", 500, 12.5)
        app = self.app(ledger)

        app._apply_portfolio_quantity_change(
            "sz000737", 600, 12.2, "2026-10-20", "manual_correction",
        )

        summary = ledger.position_summary("sz000737", "2026-10-20")
        self.assertEqual(summary["quantity"], 600)
        self.assertEqual(summary["sellable_quantity"], 500)
        self.assertEqual(summary["today_locked_quantity"], 100)

    def test_manual_sell_cannot_exceed_sellable(self):
        ledger = TradeLedger()
        ledger.record_trade("buy", "sz000737", "北方铜业", "2026-10-19", 500, 12.5)
        ledger.record_trade("buy", "sz000737", "北方铜业", "2026-10-20", 100, 12.1)
        app = self.app(ledger)

        with self.assertRaisesRegex(ValueError, "当日可卖"):
            app._apply_portfolio_quantity_change(
                "sz000737", 0, 12.2, "2026-10-20", "manual_sell",
            )

    def test_cost_only_correction_does_not_create_trade_or_lock_quantity(self):
        ledger = TradeLedger()
        ledger.record_trade("buy", "sz000737", "北方铜业", "2026-10-19", 500, 12.5)
        before_trade_count = len(ledger.trades)

        adjustment = ledger.record_adjustment(
            "sz000737", "北方铜业", "2026-10-20", 0, 12.2, "cost_correction",
        )

        self.assertEqual(adjustment.quantity_delta, 0)
        self.assertEqual(len(ledger.trades), before_trade_count)
        summary = ledger.position_summary("sz000737", "2026-10-20")
        self.assertEqual(summary["today_locked_quantity"], 0)
        self.assertEqual(summary["average_cost"], 12.2)

    def test_adjustments_survive_json_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "trade_ledger.json"
            ledger = TradeLedger(path)
            ledger.record_trade("buy", "sz000737", "北方铜业", "2026-10-19", 500, 12.5)
            ledger.record_adjustment(
                "sz000737", "北方铜业", "2026-10-20", 100, 12.1, "manual_buy",
            )

            loaded = TradeLedger(path)

            summary = loaded.position_summary("sz000737", "2026-10-20")
            self.assertEqual(summary["quantity"], 600)
            self.assertEqual(summary["sellable_quantity"], 500)
            self.assertEqual(summary["today_locked_quantity"], 100)
            self.assertTrue(json.loads(path.read_text(encoding="utf-8"))["adjustments"])

    def test_unmonitor_removes_ui_item_but_keeps_ledger_position(self):
        ledger = TradeLedger()
        ledger.record_trade("buy", "sz000737", "北方铜业", "2026-10-19", 500, 12.5)
        app = self.app(ledger)
        app._resolve_portfolio_stock = Mock(
            return_value=("sz000737", "北方铜业"),
        )
        app._window = Mock(side_effect=[
            SimpleNamespace(run=lambda: SimpleNamespace(clicked=True, text="北方铜业")),
            SimpleNamespace(run=lambda: SimpleNamespace(clicked=True, text="确认")),
        ])
        app._alert = Mock()

        app.show_portfolio_unmonitor(None)

        self.assertEqual(app.portfolio["positions"], [])
        summary = ledger.position_summary("sz000737", "2026-10-20")
        self.assertEqual(summary["quantity"], 500)


if __name__ == "__main__":
    unittest.main()
