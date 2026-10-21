#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from datetime import datetime
from types import SimpleNamespace
import unittest

import pandas as pd

from high_position_policy import HighPositionInput, HighPositionState, assess_high_position
from trend_assessment import (
    MajorTrend,
    ShortTrend,
    TrendConflict,
    assess_trend,
    build_trade_permissions,
)


class TrendPolicyBoundaryTests(unittest.TestCase):
    def test_short_history_is_missing_and_cannot_open_normal_opportunity(self):
        history = pd.DataFrame([{
            "date": "2026-10-14", "open": 10, "high": 11,
            "low": 9, "close": 10, "volume": 1000,
        }])

        trend = assess_trend(history, price=10, now=datetime(2026, 10, 14, 18))
        permissions = build_trade_permissions(
            trend, True, "NOT_HIGH", True, True,
            SimpleNamespace(
                complete=True,
                m15_confirmations=2,
                m30_aligned=True,
                clearance_confirmed=True,
            ),
            "buy", 100000, 5,
        )

        self.assertEqual(trend.short_trend, ShortTrend.MISSING)
        self.assertEqual(trend.major_trend, MajorTrend.MISSING)
        self.assertFalse(permissions.allow_normal_buy)

    def test_bad_history_degrades_to_structured_missing_data(self):
        history = pd.DataFrame([{
            "date": "bad", "open": float("nan"), "high": float("inf"),
            "low": -1, "close": 0, "volume": -1,
        }])

        result = assess_trend(history, price=10)

        self.assertEqual(result.short_trend, ShortTrend.MISSING)
        self.assertTrue(result.missing_data)

    def test_missing_market_or_sector_waits_instead_of_default_pass(self):
        trend = SimpleNamespace(
            major_trend=MajorTrend.UP,
            short_trend=ShortTrend.UP,
            conflict=TrendConflict.ALIGNED_UP,
        )
        permissions = build_trade_permissions(
            trend, True, "NOT_HIGH", None, None,
            SimpleNamespace(
                complete=True,
                m15_confirmations=2,
                m30_aligned=True,
                clearance_confirmed=True,
            ),
            "buy", 100000, 5,
        )

        self.assertFalse(permissions.allow_normal_buy)

    def test_missing_minute_evidence_keeps_high_position_pending(self):
        result = assess_high_position(HighPositionInput(
            price=20,
            price_location="HIGH",
            timeframe=None,
        ))

        self.assertEqual(result.state, HighPositionState.MISSING)

    def test_risk_action_survives_missing_normal_opportunity_data(self):
        permissions = build_trade_permissions(
            SimpleNamespace(
                major_trend=MajorTrend.DOWN,
                short_trend=ShortTrend.DOWN,
                conflict=TrendConflict.NO_CONFIRMED_ALIGNMENT,
            ),
            False,
            HighPositionState.DISTRIBUTION_RISK,
            None,
            None,
            None,
            "risk_exit",
            100000,
            5,
        )

        self.assertTrue(permissions.allow_risk_action)


if __name__ == "__main__":
    unittest.main()
