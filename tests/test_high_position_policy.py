#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from types import SimpleNamespace
import unittest

from high_position_policy import HighPositionInput, HighPositionState, assess_high_position


def timeframe(complete=True, m15=2, m30=True):
    return SimpleNamespace(
        complete=complete, m15_confirmations=m15, m30_aligned=m30,
        clearance_confirmed=True,
    )


def base(**overrides):
    values = dict(
        price=30.0, price_location="HIGH", range_width_pct=6.0,
        atr=1.0, volume_ratio=0.8, close=29.8, open=29.5,
        bar_position=0.55, upper_shadow_atr=0.2, lower_pivots_rising=True,
        down_volume_ratio=0.7, support_holds=True, support=28.0,
        resistance=30.0, short_trend="近期向上", market_ok=True,
        sector_ok=True, timeframe=timeframe(),
    )
    values.update(overrides)
    return HighPositionInput(**values)


class HighPositionPolicyTests(unittest.TestCase):
    def test_distribution_evidence_wins_for_high_price(self):
        result = assess_high_position(base(
            volume_ratio=1.6, close=28.2, open=29.5, bar_position=0.15,
            lower_pivots_rising=False, down_volume_ratio=1.4, support_holds=False,
        ))
        self.assertEqual(result.state, HighPositionState.DISTRIBUTION_RISK)

    def test_narrow_range_shrink_and_rising_lows_is_consolidation(self):
        result = assess_high_position(base())
        self.assertEqual(result.state, HighPositionState.HIGH_CONSOLIDATION)

    def test_second_wave_needs_breakout_volume_and_minutes(self):
        result = assess_high_position(base(
            price=30.2, resistance=30.0, volume_ratio=1.4,
        ))
        self.assertEqual(result.state, HighPositionState.SECOND_WAVE_CONFIRMED)

    def test_missing_minutes_keeps_candidate_or_pending(self):
        result = assess_high_position(base(
            price=30.2, resistance=30.0, volume_ratio=1.4,
            timeframe=timeframe(False, 1, False),
        ))
        self.assertIn(result.state, {
            HighPositionState.SECOND_WAVE_CANDIDATE,
            HighPositionState.HIGH_PENDING,
        })

    def test_non_high_is_not_high_position(self):
        result = assess_high_position(base(price_location="MID"))
        self.assertEqual(result.state, HighPositionState.NOT_HIGH)

    def test_non_high_does_not_require_intraday_evidence(self):
        result = assess_high_position(base(price_location="LOW", timeframe=None))
        self.assertEqual(result.state, HighPositionState.NOT_HIGH)

    def test_conflicting_evidence_stays_pending(self):
        result = assess_high_position(base(
            volume_ratio=1.6, close=28.2, open=29.5, bar_position=0.15,
            lower_pivots_rising=True, support_holds=True,
        ))
        self.assertEqual(result.state, HighPositionState.HIGH_PENDING)


if __name__ == "__main__":
    unittest.main()
