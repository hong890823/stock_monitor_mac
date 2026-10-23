import unittest

from daily_risk_context import DailyRiskContextCache, build_daily_risk_context


class DailyRiskContextTest(unittest.TestCase):
    def test_high_position_trend_weak_is_high_exit_risk(self):
        result = {
            "trade_date": "2026-10-23",
            "cost": 10.0,
            "price": 18.0,
            "atr": 0.5,
            "price_position": {"location_key": "HIGH", "range_low": 12.0},
            "entry_structure": {
                "stop_price": 15.0,
                "trend_weak": True,
                "trend_assessment": {"major_trend": "DOWN"},
            },
        }

        context = build_daily_risk_context(result, {}, "2026-10-23")

        self.assertTrue(context["high_exit_risk"])
        self.assertFalse(context["systemic_risk"])
        self.assertEqual(context["source_date"], "2026-10-23")

    def test_portfolio_breadth_marks_systemic_risk(self):
        overview = {
            "n_holdings": 3,
            "attribution": {"mostly_down": True},
            "trend": {"risk_break": 2},
        }

        context = build_daily_risk_context({}, overview, "2026-10-23")

        self.assertTrue(context["systemic_risk"])
        self.assertTrue(context["portfolio_risk"])

    def test_event_and_position_risk_are_separate_from_price_location(self):
        result = {
            "fundamental_level": "ALERT",
            "position_share_pct": 12.5,
            "price_position": {"location_key": "LOW"},
            "entry_structure": {},
        }

        context = build_daily_risk_context(result, {}, "2026-10-23")

        self.assertTrue(context["event_risk"])
        self.assertTrue(context["position_risk"])
        self.assertFalse(context["high_exit_risk"])

    def test_cache_is_reused_for_same_trade_date(self):
        cache = DailyRiskContextCache()
        overview = {"attribution": {"all_down": True}}

        first = cache.get("sh600999", "2026-10-23", {"price_position": {}}, overview)
        second = cache.get(
            "sh600999", "2026-10-23", {"price_position": {"location_key": "HIGH"}}, overview,
        )

        self.assertEqual(first, second)

    def test_cache_does_not_reuse_context_across_trade_dates(self):
        cache = DailyRiskContextCache()
        overview = {"attribution": {"all_down": True}}

        first = cache.get("sh600999", "2026-10-23", {}, overview)
        second = cache.get("sh600999", "2026-10-24", {}, {"attribution": {}})

        self.assertTrue(first["systemic_risk"])
        self.assertFalse(second["systemic_risk"])


if __name__ == "__main__":
    unittest.main()
