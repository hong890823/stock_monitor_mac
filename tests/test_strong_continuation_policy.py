#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import unittest

from strong_continuation_policy import (
    RiskLine,
    StrongContinuationInput,
    StrongContinuationState,
    assess_strong_continuation,
)


def base(**overrides):
    values = dict(
        price=11.0,
        ma5=10.8,
        ma10=10.5,
        ma20=10.2,
        ma20_prev=10.1,
        price_location="MID",
        bias_pct=7.0,
        breakout_reference=10.6,
        volume_ratio=1.2,
        lower_pivots_rising=True,
        support_holds=True,
        market_ok=True,
        sector_ok=True,
    )
    values.update(overrides)
    return StrongContinuationInput(**values)


class StrongContinuationPolicyTests(unittest.TestCase):
    def test_low_price_uses_ma20_break_risk(self):
        result = assess_strong_continuation(base(
            price=9.8, ma5=10.0, ma10=10.1, ma20=10.0, ma20_prev=9.9,
            price_location="LOW", bias_pct=-2.0, breakout_reference=9.7,
            break_confirmed=True,
        ))

        self.assertEqual(result.state, StrongContinuationState.M20_BREAK_RISK)
        self.assertEqual(result.risk_line, RiskLine.MA20)
        self.assertEqual(result.risk_price, 10.0)
        self.assertTrue(result.risk_confirmed)

    def test_high_bias_uses_ma5_risk_line(self):
        result = assess_strong_continuation(base(
            price=10.8, ma5=11.0, ma10=10.5, ma20=10.0, ma20_prev=9.9,
            price_location="HIGH", bias_pct=12.0, breakout_reference=10.6,
            break_confirmed=True,
        ))

        self.assertEqual(result.state, StrongContinuationState.M5_BREAK_RISK)
        self.assertEqual(result.risk_line, RiskLine.MA5)
        self.assertTrue(result.risk_confirmed)

    def test_intraday_break_without_confirmation_stays_as_warning(self):
        result = assess_strong_continuation(base(
            price=10.75, ma5=10.8, price_location="HIGH", bias_pct=9.0,
            bar_complete=False,
        ))

        self.assertEqual(result.state, StrongContinuationState.M5_BREAK_RISK)
        self.assertFalse(result.risk_confirmed)
        self.assertIn("风险预警", result.reasons)

    def test_close_back_above_risk_line_releases_false_break(self):
        result = assess_strong_continuation(base(
            price=10.9, ma5=10.8, price_location="MID", bias_pct=7.0,
            break_confirmed=True,
        ))

        self.assertEqual(result.state, StrongContinuationState.STRONG_CONTINUATION)
        self.assertTrue(result.risk_released)
        self.assertIn("风险解除", result.reasons)

    def test_stable_price_without_prior_break_does_not_report_released(self):
        result = assess_strong_continuation(base())
        self.assertFalse(result.risk_released)
        self.assertNotIn("风险解除", result.reasons)

    def test_bullish_breakout_hold_is_strong_continuation(self):
        result = assess_strong_continuation(base())

        self.assertEqual(result.state, StrongContinuationState.STRONG_CONTINUATION)
        self.assertEqual(result.risk_line, RiskLine.MA20)

    def test_high_bias_above_ma5_is_extended_observation_only(self):
        result = assess_strong_continuation(base(
            price=11.8, ma5=11.0, price_location="HIGH", bias_pct=12.0,
        ))

        self.assertEqual(result.state, StrongContinuationState.EXTENDED_CONTINUATION)
        self.assertFalse(result.eligible_candidate)

    def test_missing_core_data_never_passes(self):
        result = assess_strong_continuation(base(ma20=float("nan")))

        self.assertEqual(result.state, StrongContinuationState.MISSING)
        self.assertTrue(result.missing_data)

    def test_rejected_breakout_wins_over_strong_structure(self):
        result = assess_strong_continuation(base(
            price=10.5, breakout_reference=10.6,
        ))

        self.assertEqual(result.state, StrongContinuationState.REJECTED_BREAKOUT)

    def test_distribution_evidence_wins_over_dynamic_break(self):
        result = assess_strong_continuation(base(
            price=9.8, lower_pivots_rising=False, support_holds=False,
            volume_ratio=1.8,
        ))

        self.assertEqual(result.state, StrongContinuationState.DISTRIBUTION_RISK)

    def test_non_bullish_ma_alignment_is_not_strong(self):
        result = assess_strong_continuation(base(ma10=10.9))

        self.assertEqual(result.state, StrongContinuationState.NOT_STRONG)


if __name__ == "__main__":
    unittest.main()
