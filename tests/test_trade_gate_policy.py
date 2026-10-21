#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from types import SimpleNamespace
import unittest

from trend_assessment import (
    MajorTrend,
    ShortTrend,
    TrendAssessment,
    TrendConflict,
    TrendEvidence,
    build_trade_permissions,
)


def trend(major=MajorTrend.UP, short=ShortTrend.UP, conflict=TrendConflict.ALIGNED_UP):
    return TrendAssessment(
        short_trend=short,
        major_trend=major,
        conflict=conflict,
        reasons=(),
        missing_data=(),
        evidence=TrendEvidence(),
        summary="",
    )


def timeframe(complete=True, m15=2, m30=True):
    return SimpleNamespace(
        complete=complete,
        m15_confirmations=m15,
        m30_aligned=m30,
        clearance_confirmed=True,
    )


class TradeGatePolicyTests(unittest.TestCase):
    def test_normal_buy_requires_aligned_trend_entry_market_and_minutes(self):
        permissions = build_trade_permissions(
            trend(), True, "NOT_HIGH", True, True, timeframe(), "buy", 100000, 5
        )
        self.assertTrue(permissions.allow_normal_buy)

    def test_major_down_blocks_all_normal_opportunities(self):
        permissions = build_trade_permissions(
            trend(MajorTrend.DOWN, ShortTrend.REBOUND, TrendConflict.DOWN_MAJOR_REBOUND),
            True, "NOT_HIGH", True, True, timeframe(), "buy", 100000, 5,
        )
        self.assertTrue(permissions.risk_only)
        self.assertFalse(permissions.allow_normal_buy)
        self.assertFalse(permissions.allow_reentry)
        self.assertFalse(permissions.allow_t)

    def test_bottoming_allows_only_probe_capped_at_quarter_initial_position(self):
        permissions = build_trade_permissions(
            trend(MajorTrend.BOTTOMING, ShortTrend.REBOUND, TrendConflict.BOTTOM_REVERSAL_WATCH),
            True, "NOT_HIGH", True, True, timeframe(), "probe_buy", 100000, 5,
        )
        self.assertTrue(permissions.allow_probe)
        self.assertEqual(permissions.probe_cap_amount, 1250.0)
        self.assertFalse(permissions.allow_normal_buy)

    def test_risk_action_bypasses_normal_gate(self):
        permissions = build_trade_permissions(
            trend(MajorTrend.DOWN, ShortTrend.DOWN, TrendConflict.NO_CONFIRMED_ALIGNMENT),
            False, "DISTRIBUTION_RISK", False, False, timeframe(False, 0, False),
            "risk_exit", 100000, 5,
        )
        self.assertTrue(permissions.allow_risk_action)

    def test_missing_market_or_minutes_keeps_opportunity_waiting(self):
        permissions = build_trade_permissions(
            trend(), True, "NOT_HIGH", None, True, timeframe(False, 1, False),
            "buy", 100000, 5,
        )
        self.assertFalse(permissions.allow_normal_buy)


if __name__ == "__main__":
    unittest.main()
