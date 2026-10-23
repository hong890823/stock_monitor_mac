import os
import sys
import unittest
from datetime import datetime
from unittest.mock import Mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from intraday_tactics import IntradaySignal, IntradaySignalType
from stock_menubar import StockMenuBar


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
    def __init__(self, sellable=500, locked=100, total=600, consistent=True):
        self.sellable = sellable
        self.locked = locked
        self.total = total
        self.consistent = consistent

    def position_summary(self, code, as_of_date):
        return {
            "code": code,
            "quantity": self.total,
            "sellable_quantity": self.sellable,
            "today_locked_quantity": self.locked,
            "consistent": self.consistent,
        }


def subject(bars15, bars30, ledger):
    app = object.__new__(StockMenuBar)
    app._trade_ledger = ledger
    app._intraday_tactics = Mock()
    app._intraday_bar_fetcher = Mock(side_effect=lambda code, scale, datalen, now: (
        bars15 if scale == 15 else bars30
    ))
    app.log = Mock()
    return app


class IntradayTradeIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 20, 10, 46)

    def test_unconfirmed_buy_becomes_observation_only(self):
        app = subject(
            [bar("2026-10-20 10:45:00", 12.1)],
            [bar("2026-10-20 10:30:00", 12.1)],
            FakeLedger(),
        )
        signal = IntradaySignal(
            IntradaySignalType.T_BUY,
            "📗 北方铜业 回踩接回",
            "站回支撑后可小仓补。",
            "小仓补",
        )

        result = app._apply_intraday_trade_policy(
            signal, "sz000737", "北方铜业", 12.1, 12.0, 13.0,
            now=self.now, buy_capacity_quantity=100,
            trade_date="2026-10-20",
        )

        self.assertEqual(result.signal_type, IntradaySignalType.TRADE_WAIT)
        self.assertIn("仅观察", result.notification_body)
        self.assertNotIn("小仓买入", result.notification_body)
        app._intraday_tactics.defer_signal.assert_called_once_with(
            "sz000737", IntradaySignalType.T_BUY, retry_after_seconds=600
        )

    def test_confirmed_buy_forwards_volume_context_to_action_guard(self):
        bars15 = [
            bar("2026-10-20 10:30:00", 12.1),
            bar("2026-10-20 10:45:00", 12.1),
        ]
        bars30 = [
            bar("2026-10-20 10:00:00", 12.1),
            bar("2026-10-20 10:30:00", 12.1),
        ]
        app = subject(bars15, bars30, FakeLedger())
        signal = IntradaySignal(
            IntradaySignalType.T_BUY,
            "📗 北方铜业 回踩接回",
            "站回支撑后可小仓补。",
            "小仓补",
        )

        result = app._apply_intraday_trade_policy(
            signal, "sz000737", "北方铜业", 12.1, 12.0, 13.0,
            now=self.now, buy_capacity_quantity=100,
            trade_date="2026-10-20",
            volume_inputs={
                "market_turnover": 130,
                "recent_market_turnovers": [100, 100, 100],
                "stock_volume_ratio": 1.5,
                "price_change_pct": 2.0,
            },
        )

        self.assertNotIn("仅观察", result.body)
        self.assertIn("小仓买", result.notification_body)

    def test_today_bought_only_risk_signal_cannot_sell_today(self):
        app = subject(
            [
                bar("2026-10-20 10:30:00", 11.8),
                bar("2026-10-20 10:45:00", 11.75),
            ],
            [bar("2026-10-20 10:30:00", 11.8)],
            FakeLedger(sellable=0, locked=100, total=100),
        )
        signal = IntradaySignal(
            IntradaySignalType.BREAK_SUPPORT,
            "⚠️ 北方铜业 跌破支撑",
            "风险条件触发。",
            "跌破12.03｜卖出",
        )

        result = app._apply_intraday_trade_policy(
            signal, "sz000737", "北方铜业", 11.8, 12.0, 13.0,
            now=self.now, buy_capacity_quantity=0,
            trade_date="2026-10-20",
            historical_context={
                "price_location": "LOW",
                "closes_below_ma20": 10,
                "invalidation_price": 10.5,
            },
        )

        self.assertEqual(result.signal_type, IntradaySignalType.NEXT_DAY_REVIEW)
        self.assertIn("T+1", result.notification_body)
        self.assertIn("下一交易日", result.notification_body)
        self.assertNotIn("今日卖出", result.notification_body)

    def test_two_confirmed_risk_bars_reduce_one_lot_and_do_not_wait_for_30m(self):
        app = subject(
            [
                bar("2026-10-20 10:30:00", 11.8),
                bar("2026-10-20 10:45:00", 11.75),
            ],
            [bar("2026-10-20 10:30:00", 11.7)],
            FakeLedger(sellable=500, locked=100, total=600),
        )
        signal = IntradaySignal(
            IntradaySignalType.EXIT_RISK,
            "🔴 北方铜业 高位离场风险",
            "确认跌破支撑后离场。",
            "支撑12.03｜风险离场",
        )

        result = app._apply_intraday_trade_policy(
            signal, "sz000737", "北方铜业", 11.8, 12.0, 13.0,
            now=self.now, buy_capacity_quantity=0,
            trade_date="2026-10-20",
            historical_context={
                "price_location": "LOW",
                "closes_below_ma20": 10,
                "range_low": 10.5,
                "invalidation_price": 10.5,
                "major_trend": "MISSING",
            },
        )

        self.assertEqual(result.signal_type, IntradaySignalType.EXIT_WINDOW)
        self.assertIn("最多减100股", result.notification_body)
        self.assertIn("不纳入", result.notification_body)
        self.assertEqual(app._intraday_bar_fetcher.call_count, 1)
        self.assertEqual(app._intraday_bar_fetcher.call_args.args[1], 15)

    def test_ledger_mismatch_blocks_risk_action(self):
        app = subject(
            [
                bar("2026-10-20 10:30:00", 11.8),
                bar("2026-10-20 10:45:00", 11.75),
            ],
            [],
            FakeLedger(sellable=500, locked=0, total=600, consistent=False),
        )
        signal = IntradaySignal(
            IntradaySignalType.EXIT_RISK,
            "🔴 北方铜业 风险离场",
            "确认跌破支撑后离场。",
            "支撑12.03｜风险离场",
        )

        result = app._apply_intraday_trade_policy(
            signal, "sz000737", "北方铜业", 11.8, 12.0, 13.0,
            now=self.now, trade_date="2026-10-20",
        )

        self.assertEqual(result.signal_type, IntradaySignalType.TRADE_WAIT)
        self.assertIn("持仓账本数量不一致", result.notification_body)

    def test_t_sell_without_dual_confirmation_is_suppressed(self):
        app = subject(
            [bar("2026-10-20 10:45:00", 12.4)],
            [bar("2026-10-20 10:30:00", 12.4)],
            FakeLedger(),
        )
        signal = IntradaySignal(
            IntradaySignalType.T_SELL,
            "🔥 北方铜业 冲高做T",
            "乖离扩大，可做T。",
            "冲高做T｜减仓",
        )

        result = app._apply_intraday_trade_policy(
            signal, "sz000737", "北方铜业", 12.4, 12.0, 13.0,
            now=self.now, buy_capacity_quantity=0,
            trade_date="2026-10-20",
        )

        self.assertEqual(result.signal_type, IntradaySignalType.TRADE_WAIT)
        self.assertIn("不做T", result.notification_body)


if __name__ == "__main__":
    unittest.main()
