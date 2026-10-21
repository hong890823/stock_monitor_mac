import unittest

from market_environment import MarketEnvironmentStatus, evaluate_market_environment


class MarketEnvironmentEvidenceTests(unittest.TestCase):
    def evaluate(self, **overrides):
        values = dict(
            predicted_turnover_yi=22000, market_change_pct=1.2,
            market_price=3200, market_high_price=3210,
            industry_breadth=0.72, concept_breadth=0.68,
            recent_full_day_turnovers_yi=[17000, 18000, 19000],
        )
        values.update(overrides)
        return evaluate_market_environment(**values)

    def test_missing_breadth_is_unknown_not_observed_divergence(self):
        self.assertEqual(self.evaluate(industry_breadth=None).status, MarketEnvironmentStatus.UNKNOWN)

    def test_missing_index_high_is_unknown(self):
        self.assertEqual(self.evaluate(market_high_price=None).status, MarketEnvironmentStatus.UNKNOWN)

    def test_invalid_breadth_cannot_confirm_strength(self):
        for invalid in (-0.1, 1.2, 60, float('nan')):
            with self.subTest(invalid=invalid):
                self.assertEqual(self.evaluate(industry_breadth=invalid).status, MarketEnvironmentStatus.UNKNOWN)

    def test_missing_history_breaks_continuity(self):
        result = self.evaluate(recent_full_day_turnovers_yi=[16000, float('nan'), 17000])
        self.assertEqual(result.persistence_days, 1)
        self.assertFalse(result.strong)

    def test_reasons_identify_today_as_estimate(self):
        summary = self.evaluate().summary()
        self.assertIn('此前完整收盘日连续3日', summary)
        self.assertIn('最近3日均值18,000亿', summary)
        self.assertIn('今日预估单独观察', summary)

    def test_environment_quality_and_turnover_activity_are_separate_dimensions(self):
        result = self.evaluate(predicted_turnover_yi=18500)

        self.assertEqual(result.activity_label, "中性偏弱")
        self.assertIn("量能活跃度=中性偏弱", result.summary())
        self.assertEqual(result.status, MarketEnvironmentStatus.NORMAL)

    def test_three_days_above_floor_but_low_average_stays_unconfirmed(self):
        result = self.evaluate(recent_full_day_turnovers_yi=[17000, 17000, 17000])
        self.assertEqual(result.persistence_days, 3)
        self.assertEqual(result.persistence_average_yi, 17000)
        self.assertEqual(result.status, MarketEnvironmentStatus.STRONG_SINGLE_DAY)

    def test_nonpositive_turnover_is_unknown(self):
        for amount in (0, -22000):
            self.assertEqual(self.evaluate(predicted_turnover_yi=amount).status, MarketEnvironmentStatus.UNKNOWN)

    def test_impossible_index_price_does_not_confirm_strength(self):
        self.assertEqual(self.evaluate(market_price=3300).status, MarketEnvironmentStatus.UNKNOWN)
