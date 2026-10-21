#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from datetime import datetime, timedelta
import unittest

import pandas as pd

from trend_assessment import MajorTrend, ShortTrend, TrendConflict, assess_trend


def history(closes):
    start = datetime(2024, 1, 2)
    rows = []
    for index, close in enumerate(closes):
        date = start + timedelta(days=index)
        rows.append({
            "date": date.strftime("%Y-%m-%d"),
            "open": close * 0.995,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": 1000 + index,
        })
    return pd.DataFrame(rows)


def long_uptrend(count=650):
    return history([10 + index * 0.04 for index in range(count)])


def long_downtrend(count=650):
    return history([40 - index * 0.06 for index in range(count)])


class TrendAssessmentTests(unittest.TestCase):
    def test_short_up_and_major_up_use_completed_multi_period_evidence(self):
        frame = long_uptrend()
        completed = frame[pd.to_datetime(frame["date"]) <= datetime(2026, 1, 20, 15, 30)]
        result = assess_trend(
            frame, price=float(completed["close"].iloc[-1]), now=datetime(2026, 1, 20, 15, 30)
        )
        self.assertEqual(result.short_trend, ShortTrend.UP)
        self.assertEqual(result.major_trend, MajorTrend.UP)
        self.assertEqual(result.conflict, TrendConflict.ALIGNED_UP)

    def test_extended_five_day_run_wins_over_normal_up(self):
        closes = [10 + index * 0.01 for index in range(350)]
        closes.extend([closes[-1] * factor for factor in (1.03, 1.07, 1.12, 1.18, 1.25)])
        result = assess_trend(
            history(closes), price=closes[-1], now=datetime(2026, 1, 20, 15, 30)
        )
        self.assertEqual(result.short_trend, ShortTrend.EXTENDED)

    def test_effective_ma20_break_maps_to_short_down(self):
        closes = [40 - index * 0.08 for index in range(355)]
        closes.extend([closes[-1] * 0.94, closes[-1] * 0.92])
        result = assess_trend(
            history(closes), price=closes[-1], now=datetime(2026, 1, 20, 15, 30)
        )
        self.assertEqual(result.short_trend, ShortTrend.DOWN)
        self.assertTrue(result.evidence.trend_weak)

    def test_long_downtrend_is_major_down(self):
        result = assess_trend(
            long_downtrend(), price=18.0, now=datetime(2026, 1, 20, 15, 30)
        )
        self.assertEqual(result.major_trend, MajorTrend.DOWN)
        self.assertEqual(result.conflict, TrendConflict.NO_CONFIRMED_ALIGNMENT)

    def test_insufficient_history_is_missing_and_does_not_guess(self):
        result = assess_trend(
            history([10, 10.1, 10.2]), price=10.2, now=datetime(2026, 1, 20, 15, 30)
        )
        self.assertEqual(result.short_trend, ShortTrend.MISSING)
        self.assertEqual(result.major_trend, MajorTrend.MISSING)
        self.assertTrue(result.missing_data)


if __name__ == "__main__":
    unittest.main()
