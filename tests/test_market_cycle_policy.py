#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import unittest
from datetime import date

from market_cycle_policy import (
    MarketCycleInput,
    MarketCyclePhase,
    MarketCycleRegime,
    assess_market_cycle,
    format_market_cycle_assessment,
)


class MarketCyclePolicyTests(unittest.TestCase):
    def test_strong_up_window_is_dynamic_not_fixed(self):
        result = assess_market_cycle(MarketCycleInput(
            predicted_turnover_yi=27000,
            turnover_history_yi=[22000, 23000, 24000],
            index_today_pct=1.1,
            index_recent_pct=2.4,
            industry_breadth=0.72,
            concept_breadth=0.68,
            phase_hint="上涨",
            phase_age_days=3,
        ))
        self.assertEqual(result.phase, MarketCyclePhase.UP)
        self.assertEqual(result.regime, MarketCycleRegime.VERY_STRONG)
        self.assertEqual(result.up_days_range, (4, 7))
        self.assertEqual(result.divergence_days_range, (3, 5))
        self.assertIn("当前上涨第3天", result.reasons)

    def test_weak_up_window_shortens_before_divergence(self):
        result = assess_market_cycle(MarketCycleInput(
            predicted_turnover_yi=18500,
            turnover_history_yi=[19000, 18500, 18000],
            index_today_pct=0.2,
            index_recent_pct=-0.8,
            industry_breadth=0.48,
            concept_breadth=0.46,
            phase_hint="上涨",
        ))
        self.assertEqual(result.up_days_range, (1, 3))
        self.assertEqual(result.divergence_days_range, (1, 2))
        self.assertTrue(any("上涨窗口提前收窄" in reason for reason in result.reasons))

    def test_turnover_tiers_follow_market_strength_wording(self):
        cases = (
            (14000, MarketCycleRegime.VERY_WEAK),
            (16000, MarketCycleRegime.WEAK),
            (18500, MarketCycleRegime.NEUTRAL),
            (22000, MarketCycleRegime.STRONG),
            (27000, MarketCycleRegime.VERY_STRONG),
        )
        for turnover, expected in cases:
            with self.subTest(turnover=turnover):
                result = assess_market_cycle(MarketCycleInput(
                    predicted_turnover_yi=turnover,
                    index_today_pct=0.2,
                    industry_breadth=0.55,
                    concept_breadth=0.55,
                    phase_hint="上涨",
                ))
                self.assertEqual(result.regime, expected)

    def test_turnover_tier_boundaries_use_five_reviewed_levels(self):
        cases = (
            (14999, MarketCycleRegime.VERY_WEAK),
            (15000, MarketCycleRegime.WEAK),
            (17999, MarketCycleRegime.WEAK),
            (18000, MarketCycleRegime.NEUTRAL),
            (19999, MarketCycleRegime.NEUTRAL),
            (20000, MarketCycleRegime.STRONG),
            (24999, MarketCycleRegime.STRONG),
            (25000, MarketCycleRegime.VERY_STRONG),
        )
        for turnover, expected in cases:
            with self.subTest(turnover=turnover):
                result = assess_market_cycle(MarketCycleInput(
                    predicted_turnover_yi=turnover,
                    index_today_pct=0.2,
                    industry_breadth=0.55,
                    concept_breadth=0.55,
                    phase_hint="上涨",
                ))
                self.assertEqual(result.regime, expected)

    def test_turnover_baseline_requires_five_complete_close_days(self):
        result = assess_market_cycle(MarketCycleInput(
            predicted_turnover_yi=22000,
            turnover_history_yi=[18000, 19000, 20000],
            turnover_history_dates=[
                "2026-10-12", "2026-10-13", "2026-10-14",
            ],
            turnover_history_as_of_date="2026-10-15",
            index_today_pct=0.6,
            industry_breadth=0.60,
            concept_breadth=0.60,
            phase_hint="上涨",
        ))

        self.assertEqual(result.turnover_baseline_sample_count, 3)
        self.assertFalse(result.turnover_baseline_ready)
        self.assertIsNone(result.turnover_ratio)
        self.assertEqual(result.confidence, "低")
        self.assertTrue(any("完整收盘日不足5个" in reason for reason in result.reasons))

    def test_stale_and_gapped_turnover_baseline_is_low_confidence(self):
        result = assess_market_cycle(MarketCycleInput(
            predicted_turnover_yi=22000,
            turnover_history_yi=[18000, 19000, 20000, 21000, 22000],
            turnover_history_dates=[
                "2026-10-01", "2026-10-02", "2026-10-03",
                "2026-10-15", "2026-10-20",
            ],
            turnover_history_as_of_date="2026-10-30",
            index_today_pct=0.6,
            industry_breadth=0.60,
            concept_breadth=0.60,
            phase_hint="上涨",
        ))

        self.assertTrue(result.turnover_baseline_ready)
        self.assertTrue(result.turnover_baseline_stale)
        self.assertGreaterEqual(result.turnover_baseline_gap_count, 1)
        self.assertEqual(result.confidence, "低")
        self.assertTrue(any("历史基准过旧" in reason for reason in result.reasons))
        self.assertTrue(any("日期缺口" in reason for reason in result.reasons))
        self.assertIsNone(result.turnover_ratio)

    def test_high_turnover_divergence_uses_structure_to_adjust_window(self):
        result = assess_market_cycle(MarketCycleInput(
            predicted_turnover_yi=30000,
            turnover_history_yi=[23000, 24000, 25000],
            index_today_pct=-0.4,
            index_recent_pct=1.2,
            industry_breadth=0.42,
            concept_breadth=0.39,
            phase_hint="分歧",
            divergence_age_days=2,
        ))
        self.assertEqual(result.phase, MarketCyclePhase.DIVERGENCE)
        self.assertEqual(result.divergence_days_range, (1, 2))
        self.assertTrue(any("分歧窗口提前收窄" in reason for reason in result.reasons))

    def test_down_window_can_be_longer_but_is_not_a_sell_trigger(self):
        result = assess_market_cycle(MarketCycleInput(
            predicted_turnover_yi=16000,
            index_today_pct=-1.2,
            index_recent_pct=-3.0,
            industry_breadth=0.32,
            concept_breadth=0.35,
            phase_hint="下跌",
            down_age_days=9,
        ))
        self.assertEqual(result.down_days_range, (5, 8))
        self.assertTrue(any("超过5–8天参考区间" in reason for reason in result.reasons))
        self.assertNotIn("卖出", format_market_cycle_assessment(result))

    def test_assessment_exposes_each_phase_age_and_over_window_status(self):
        divergence = assess_market_cycle(MarketCycleInput(
            predicted_turnover_yi=30000,
            turnover_history_yi=[23000, 24000, 25000, 26000, 27000],
            turnover_history_dates=[
                "2026-10-12", "2026-10-13", "2026-10-14",
                "2026-10-15", "2026-10-16",
            ],
            turnover_history_as_of_date="2026-10-19",
            index_today_pct=-0.4,
            industry_breadth=0.42,
            concept_breadth=0.39,
            phase_hint="分歧",
            divergence_age_days=4,
        ))
        down = assess_market_cycle(MarketCycleInput(
            predicted_turnover_yi=16000,
            index_today_pct=-1.2,
            industry_breadth=0.32,
            concept_breadth=0.35,
            phase_hint="下跌",
            down_age_days=9,
        ))

        self.assertEqual(divergence.divergence_age_days, 4)
        self.assertIsNone(divergence.down_age_days)
        self.assertIn("当前分歧第4天", format_market_cycle_assessment(divergence))
        self.assertIn("已超过参考窗口", format_market_cycle_assessment(divergence))
        self.assertEqual(down.down_age_days, 9)
        self.assertIsNone(down.divergence_age_days)
        self.assertIn("当前下跌第9天", format_market_cycle_assessment(down))

    def test_missing_market_data_does_not_guess_a_cycle(self):
        result = assess_market_cycle(MarketCycleInput())
        self.assertEqual(result.phase, MarketCyclePhase.UNKNOWN)
        self.assertIsNone(result.up_days_range)
        self.assertIsNone(result.divergence_days_range)
        self.assertIn("暂不估计", format_market_cycle_assessment(result))


if __name__ == "__main__":
    unittest.main()
