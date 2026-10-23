import unittest

from intraday_volume_context import build_intraday_volume_context


def bar(timestamp, open_, high, low, close):
    return {
        "timestamp": timestamp,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": 100,
    }


class IntradayVolumeContextTest(unittest.TestCase):
    def test_market_and_stock_volume_confirm_expansion(self):
        context = build_intraday_volume_context(
            market_turnover=130,
            recent_market_turnovers=[100, 100, 100],
            stock_volume_ratio=1.4,
            price_change_pct=2.0,
        )

        self.assertTrue(context.market_expanding)
        self.assertTrue(context.stock_volume_confirming)

    def test_absorption_requires_recovery_and_volume(self):
        context = build_intraday_volume_context(
            market_turnover=130,
            recent_market_turnovers=[100, 100, 100],
            stock_volume_ratio=1.4,
            bars15=[
                bar("2026-10-23 09:30:00", 16.0, 16.1, 15.2, 15.3),
                {**bar("2026-10-23 09:45:00", 15.3, 15.4, 14.5, 15.2), "volume": 200},
            ],
            price=15.1,
            support=15.3,
        )

        self.assertTrue(context.absorption_confirmed)

    def test_blowoff_is_flagged_without_treating_it_as_buy_confirmation(self):
        context = build_intraday_volume_context(
            stock_volume_ratio=1.8,
            price_change_pct=3.0,
            bars15=[
                bar("2026-10-23 09:30:00", 15.0, 15.2, 14.9, 15.1),
                {**bar("2026-10-23 09:45:00", 15.1, 16.0, 15.0, 15.2), "volume": 200},
            ],
        )

        self.assertTrue(context.blowoff_risk)

    def test_current_bar_volume_gates_absorption(self):
        context = build_intraday_volume_context(
            stock_volume_ratio=2.0,
            bars15=[
                bar("2026-10-23 09:30:00", 16.0, 16.1, 15.2, 15.3),
                {**bar("2026-10-23 09:45:00", 15.3, 15.4, 14.5, 15.2), "volume": 100},
            ],
            price=15.1,
            support=15.3,
        )

        self.assertFalse(context.absorption_confirmed)


if __name__ == "__main__":
    unittest.main()
