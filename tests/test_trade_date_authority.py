import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from intraday_tactics import IntradaySignal, IntradaySignalType
from stock_menubar import StockMenuBar
from trade_ledger import TradeLedger


def bar(timestamp, close, support=12.0):
    return {
        "timestamp": timestamp,
        "open": close - 0.05,
        "high": max(close, support) + 0.1,
        "low": min(close, support) - 0.1,
        "close": close,
        "volume": 100,
    }


class FakeLedger:
    def position_summary(self, code, as_of_date):
        return {
            "code": code,
            "quantity": 600,
            "sellable_quantity": 500,
            "today_locked_quantity": 100,
        }


class TradeDateAuthorityTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 21, 9, 46)

    @staticmethod
    def app(bars15=None, bars30=None):
        app = object.__new__(StockMenuBar)
        app._trade_ledger = FakeLedger()
        app._intraday_tactics = Mock()
        app._intraday_bar_fetcher = Mock(side_effect=lambda code, scale, datalen, now: (
            bars15 if scale == 15 else bars30
        ))
        app.log = Mock()
        return app

    def test_sellable_is_compared_to_quote_date_not_wall_clock(self):
        ledger = TradeLedger()
        ledger.record_trade("buy", "sz000737", "北方铜业", "2026-10-20", 100, 12.13)

        self.assertEqual(ledger.sellable_quantity("sz000737", "2026-10-20"), 0)
        self.assertEqual(ledger.sellable_quantity("sz000737", "2026-10-21"), 100)

    def test_current_trade_date_uses_quote_date_field(self):
        app = self.app()

        self.assertEqual(
            app._current_trade_date("sz000737", {"date": "2026-10-21 09:45:00"}),
            "2026-10-21",
        )
        self.assertIsNone(app._current_trade_date("sz000737", {"date": ""}))

    def test_missing_quote_date_blocks_confirmed_buy(self):
        app = self.app(
            [
                bar("2026-10-21 09:15:00", 12.12),
                bar("2026-10-21 09:30:00", 12.13),
            ],
            [
                bar("2026-10-21 09:00:00", 12.10),
                bar("2026-10-21 09:30:00", 12.13),
            ],
        )
        signal = IntradaySignal(
            IntradaySignalType.T_BUY,
            "📗 北方铜业 回踩接回",
            "站回支撑后可小仓补。",
            "小仓补",
        )

        result = app._apply_intraday_trade_policy(
            signal,
            "sz000737",
            "北方铜业",
            12.13,
            12.03,
            13.0,
            now=self.now,
            buy_capacity_quantity=100,
            trade_date=None,
        )

        self.assertEqual(result.signal_type, IntradaySignalType.TRADE_WAIT)
        self.assertIn("行情日期缺失", result.notification_body)
        self.assertNotIn("小仓买入", result.notification_body)

    def test_missing_quote_date_risk_returns_fact_and_next_day_plan(self):
        app = self.app(
            [bar("2026-10-21 09:30:00", 11.75)],
            [],
        )
        signal = IntradaySignal(
            IntradaySignalType.EXIT_RISK,
            "🔴 北方铜业 高位离场风险",
            "确认跌破支撑后离场。",
            "支撑12.03｜风险离场",
        )

        result = app._apply_intraday_trade_policy(
            signal,
            "sz000737",
            "北方铜业",
            11.75,
            12.03,
            13.0,
            now=self.now,
            buy_capacity_quantity=0,
            trade_date=None,
        )

        self.assertEqual(result.signal_type, IntradaySignalType.NEXT_DAY_REVIEW)
        self.assertIn("行情日期缺失", result.notification_body)
        self.assertIn("风险事实", result.notification_body)
        self.assertIn("次一交易日", result.notification_body)


if __name__ == "__main__":
    unittest.main()
