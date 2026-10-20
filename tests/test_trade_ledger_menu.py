import unittest

from trade_ledger import TradeDraft, TradeLedger, parse_trade_text


class TradeLedgerMenuTest(unittest.TestCase):
    def test_trade_text_records_buy_and_updates_sellable(self):
        resolver = lambda keyword: ("sz000737", "北方铜业")
        trades = parse_trade_text(
            "买入,北方铜业,2026-10-20,100,12.13",
            resolver=resolver,
        )

        self.assertEqual(trades[0].side, "buy")
        self.assertEqual(trades[0].code, "sz000737")
        self.assertEqual(trades[0].quantity, 100)

    def test_trade_text_accepts_sell_and_semicolon_rows(self):
        ledger = TradeLedger()
        ledger.record_trade("buy", "sz000737", "北方铜业", "2026-10-19", 500, 12.50)
        drafts = parse_trade_text(
            "卖出,sz000737,2026-10-20,100,12.20；买入,sz000737,2026-10-20,100,12.13",
            resolver=lambda keyword: (keyword, "北方铜业"),
        )

        for draft in drafts:
            ledger.record_trade(
                draft.side, draft.code, draft.name, draft.trade_date,
                draft.quantity, draft.price,
            )

        summary = ledger.position_summary("sz000737", "2026-10-20")
        self.assertEqual(summary["quantity"], 500)
        self.assertEqual(summary["sellable_quantity"], 400)
        self.assertEqual(summary["today_locked_quantity"], 100)

    def test_invalid_trade_text_is_rejected_before_writing(self):
        with self.assertRaisesRegex(ValueError, "数量"):
            parse_trade_text(
                "买入,sz000737,2026-10-20,abc,12.13",
                resolver=lambda keyword: (keyword, "北方铜业"),
            )

    def test_record_many_rolls_back_partial_batch(self):
        ledger = TradeLedger()
        batch = [
            TradeDraft("buy", "sz000737", "北方铜业", "2026-10-20", 100, 12.13),
            TradeDraft("sell", "sz000737", "北方铜业", "2026-10-20", 100, 12.20),
        ]

        with self.assertRaisesRegex(ValueError, "当日可卖"):
            ledger.record_many(batch)

        self.assertEqual(ledger.position_summary("sz000737", "2026-10-20")["quantity"], 0)


if __name__ == "__main__":
    unittest.main()
