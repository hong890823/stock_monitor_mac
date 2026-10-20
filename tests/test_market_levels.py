import unittest

from market_levels import compute_levels, merge_live_bar


def kline(date, open_, high, low, close):
    return {"date": date, "open": open_, "high": high, "low": low, "close": close}


# 2026-09-28 实测场景：近5日K线（09-18~09-24，日线接口未含今日）
# 箱体 3888~3968，当日实时行情暴跌到 3824。
HISTORY = [
    kline("2026-09-18", 3892.0, 3919.7, 3888.5, 3911.9),
    kline("2026-09-21", 3920.3, 3950.9, 3918.1, 3949.9),
    kline("2026-09-22", 3963.8, 3967.7, 3945.4, 3952.1),
    kline("2026-09-23", 3951.4, 3951.5, 3934.5, 3936.5),
    kline("2026-09-24", 3925.3, 3930.5, 3888.4, 3888.4),
]
LIVE_CRASH = {
    "date": "2026-09-28",
    "price": 3823.6,
    "open": 3878.4,
    "high": 3878.4,
    "low": 3806.7,
}


class MergeLiveBarTests(unittest.TestCase):
    def test_appends_today_when_missing(self):
        merged = merge_live_bar(HISTORY, LIVE_CRASH)

        self.assertEqual(len(HISTORY) + 1, len(merged))
        self.assertEqual("2026-09-28", merged[-1]["date"])
        self.assertEqual(3823.6, merged[-1]["close"])
        self.assertEqual(3806.7, merged[-1]["low"])

    def test_replaces_existing_today_bar(self):
        stale_today = HISTORY + [kline("2026-09-28", 3878.0, 3880.0, 3860.0, 3865.0)]

        merged = merge_live_bar(stale_today, LIVE_CRASH)

        self.assertEqual(len(stale_today), len(merged))
        self.assertEqual(3823.6, merged[-1]["close"])
        self.assertEqual(3806.7, merged[-1]["low"])

    def test_keeps_existing_volume_fields_when_refreshing_today(self):
        today_bar = dict(LIVE_CRASH)
        today_bar.update({"volume": 123, "amount": 456})
        stale_today = HISTORY + [today_bar]

        merged = merge_live_bar(stale_today, LIVE_CRASH)

        self.assertEqual(123, merged[-1]["volume"])
        self.assertEqual(456, merged[-1]["amount"])

    def test_skips_when_live_is_older_than_last_bar(self):
        older = dict(LIVE_CRASH)
        older["date"] = "2026-09-20"

        merged = merge_live_bar(HISTORY, older)

        self.assertEqual(len(HISTORY), len(merged))

    def test_skips_when_live_has_no_date_or_price(self):
        self.assertEqual(
            len(HISTORY), len(merge_live_bar(HISTORY, {"price": 3823.6}))
        )
        self.assertEqual(
            len(HISTORY), len(merge_live_bar(HISTORY, {"date": "2026-09-28"}))
        )

    def test_does_not_mutate_input(self):
        merge_live_bar(HISTORY, LIVE_CRASH)

        self.assertEqual(5, len(HISTORY))

    def test_high_low_bracket_price_even_if_quote_is_odd(self):
        odd = {"date": "2026-09-28", "price": 3823.6, "high": 3800.0, "low": 3900.0}

        merged = merge_live_bar(HISTORY, odd)

        self.assertLessEqual(merged[-1]["low"], 3823.6)
        self.assertGreaterEqual(merged[-1]["high"], 3823.6)


class ComputeLevelsTests(unittest.TestCase):
    def test_crash_below_box_puts_support_under_price(self):
        """回归：现价3824时，支撑不得写成3888。"""
        merged = merge_live_bar(HISTORY, LIVE_CRASH)

        levels = compute_levels(3823.6, merged, today="2026-09-28")

        self.assertIsNotNone(levels)
        self.assertTrue(levels.broke_down)
        self.assertTrue(levels.supports)
        self.assertTrue(all(s < 3823.6 for s in levels.supports))
        self.assertTrue(all(r > 3823.6 for r in levels.resistances))
        # 破位的前箱体下沿必须转为压力
        self.assertIn(3888.4, levels.resistances)

    def test_crash_without_live_bar_still_reports_break(self):
        """日线过期且拿不到实时行情时，也不能把3888叫成支撑。"""
        levels = compute_levels(3823.6, HISTORY, today="2026-09-28")

        self.assertIsNotNone(levels)
        self.assertTrue(levels.broke_down)
        self.assertEqual([], [s for s in levels.supports if s >= 3823.6])
        self.assertIn(3888.4, levels.resistances)

    def test_price_inside_box_keeps_old_semantics(self):
        bars = merge_live_bar(HISTORY, {"date": "2026-09-28", "price": 3930.0})

        levels = compute_levels(3930.0, bars, today="2026-09-28")

        self.assertFalse(levels.broke_down)
        self.assertFalse(levels.broke_up)
        self.assertAlmostEqual(3888.4, min(levels.supports))
        self.assertAlmostEqual(3967.7, min(levels.resistances))
        self.assertGreater(levels.pos, 0.2)
        self.assertLess(levels.pos, 0.8)

    def test_support_and_resistance_straddle_price(self):
        for price in (3790.0, 3823.6, 3888.4, 3930.0, 3990.0):
            bars = merge_live_bar(HISTORY, {"date": "2026-09-28", "price": price})
            levels = compute_levels(price, bars, today="2026-09-28")

            self.assertTrue(all(s < price for s in levels.supports), price)
            self.assertTrue(all(r > price for r in levels.resistances), price)

    def test_breakout_above_box(self):
        bars = merge_live_bar(HISTORY, {"date": "2026-09-28", "price": 4000.0})

        levels = compute_levels(4000.0, bars, today="2026-09-28")

        self.assertTrue(levels.broke_up)
        self.assertIn(3967.7, levels.supports)

    def test_returns_none_without_data(self):
        self.assertIsNone(compute_levels(3823.6, [], today="2026-09-28"))
        self.assertIsNone(compute_levels(None, HISTORY, today="2026-09-28"))


if __name__ == "__main__":
    unittest.main()
