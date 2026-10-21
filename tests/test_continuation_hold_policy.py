#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import unittest

from continuation_hold_policy import (
    ContinuationHoldInput,
    ContinuationHoldState,
    assess_continuation_hold,
    format_continuation_hold,
)


def base(**overrides):
    values = dict(
        strong_state="STRONG_CONTINUATION",
        high_state="NOT_HIGH",
        risk_line="MA20",
        risk_price=9.6,
        bias_pct=4.0,
        volume_ratio=0.8,
        first_wave=True,
    )
    values.update(overrides)
    return ContinuationHoldInput(**values)


class ContinuationHoldPolicyTests(unittest.TestCase):
    def test_intact_first_wave_keeps_observation_position(self):
        result = assess_continuation_hold(base())
        self.assertEqual(result.state, ContinuationHoldState.HOLD_CONTINUATION)
        self.assertTrue(result.keep_observation_position)
        self.assertFalse(result.can_trim)

    def test_extended_first_wave_allows_partial_profit_protection(self):
        result = assess_continuation_hold(base(
            strong_state="EXTENDED_CONTINUATION", bias_pct=12.0,
        ))
        self.assertEqual(result.state, ContinuationHoldState.PROFIT_PROTECTION)
        self.assertTrue(result.can_trim)
        self.assertTrue(result.keep_observation_position)
        self.assertIn("保留观察仓", format_continuation_hold(result))

    def test_high_bias_uses_profit_protection_even_if_labelled_strong(self):
        result = assess_continuation_hold(base(bias_pct=12.0))
        self.assertEqual(result.state, ContinuationHoldState.PROFIT_PROTECTION)

    def test_second_wave_candidate_is_watch_not_exit(self):
        result = assess_continuation_hold(base(high_state="SECOND_WAVE_CANDIDATE"))
        self.assertEqual(result.state, ContinuationHoldState.SECOND_WAVE_WATCH)
        self.assertTrue(result.keep_observation_position)

    def test_confirmed_second_wave_is_kept(self):
        result = assess_continuation_hold(base(
            high_state="SECOND_WAVE_CONFIRMED", timeframe_ok=True,
        ))
        self.assertEqual(result.state, ContinuationHoldState.SECOND_WAVE_CONFIRMED)
        self.assertTrue(result.keep_observation_position)

    def test_dynamic_break_uses_defense_before_profit_talk(self):
        result = assess_continuation_hold(base(
            strong_state="M5_BREAK_RISK", bias_pct=12.0,
        ))
        self.assertEqual(result.state, ContinuationHoldState.BREAK_DEFENSE)
        self.assertFalse(result.can_trim)
        self.assertFalse(result.keep_observation_position)

    def test_distribution_risk_wins_over_second_wave_watch(self):
        result = assess_continuation_hold(base(
            strong_state="DISTRIBUTION_RISK", high_state="SECOND_WAVE_CANDIDATE",
        ))
        self.assertEqual(result.state, ContinuationHoldState.DISTRIBUTION_RISK)

    def test_missing_evidence_never_says_keep_or_trim(self):
        result = assess_continuation_hold(ContinuationHoldInput(
            strong_state="MISSING", high_state="MISSING",
        ))
        self.assertEqual(result.state, ContinuationHoldState.MISSING)
        self.assertFalse(result.keep_observation_position)
        self.assertFalse(result.can_trim)

    def test_format_never_describes_automatic_trading(self):
        text = format_continuation_hold(assess_continuation_hold(base(
            strong_state="EXTENDED_CONTINUATION", bias_pct=12.0,
        )))
        self.assertIn("部分兑现", text)
        self.assertNotIn("自动", text)
        self.assertNotIn("交易数量", text)


if __name__ == "__main__":
    unittest.main()
