#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from datetime import datetime
import unittest

from trend_assessment import BoxState, ShortTrend, assess_box, assess_trend
from technical_summary import TechnicalSummary, format_summary


def box_bars(count=40):
    rows = []
    for index in range(count):
        close = 10.5 + (1.0 if index % 2 else -1.0) * (0.45 + 0.03 * (index % 3))
        rows.append({
            "date": f"2026-{1 + index // 28:02d}-{1 + index % 28:02d}",
            "open": close - 0.05,
            "high": close + 0.35,
            "low": close - 0.35,
            "close": close,
            "volume": 1000,
        })
    return rows


class BoxTrendTests(unittest.TestCase):
    def test_technical_summary_shows_hold_continuation_strategy(self):
        summary = TechnicalSummary.from_result({
            "entry_structure": {
                "continuation_hold": {
                    "state": "PROFIT_PROTECTION",
                    "state_label": "部分兑现保护",
                    "can_trim": True,
                    "keep_observation_position": True,
                },
            },
        })
        text = format_summary(summary)
        self.assertIn("持有策略：部分兑现保护", text)
        self.assertIn("保留观察仓", text)

    def test_technical_summary_shows_dynamic_strong_risk(self):
        summary = TechnicalSummary.from_result({
            "entry_structure": {
                "strong_continuation": {
                    "state": "M20_BREAK_RISK",
                    "risk_line": "MA20",
                    "risk_price": 9.6,
                    "confirmed": False,
                },
            },
        })
        text = format_summary(summary)
        self.assertIn("跌破二十日线风险", text)
        self.assertLess(text.index("跌破二十日线风险"), text.index("建议："))

    def test_bounded_two_sided_range_is_box(self):
        result = assess_box(box_bars(), now=datetime(2026, 3, 1, 18))

        self.assertEqual(result.state, BoxState.BOX)
        self.assertGreater(result.upper, result.lower)
        self.assertGreaterEqual(result.high_touches, 2)
        self.assertGreaterEqual(result.low_touches, 2)

    def test_monotonic_trend_is_not_box(self):
        rows = []
        for index in range(40):
            close = 10 + index * 0.3
            rows.append({
                "date": f"2026-{1 + index // 28:02d}-{1 + index % 28:02d}",
                "open": close - 0.1,
                "high": close + 0.2,
                "low": close - 0.2,
                "close": close,
                "volume": 1000,
            })

        result = assess_box(rows, now=datetime(2026, 3, 1, 18))

        self.assertEqual(result.state, BoxState.NOT_BOX)

    def test_close_above_prior_box_is_upside_breakout(self):
        rows = box_bars(39)
        upper = max(row["high"] for row in rows)
        close = upper + 0.8
        rows.append({
            "date": "2026-03-01",
            "open": close - 0.2,
            "high": close + 0.2,
            "low": close - 0.3,
            "close": close,
            "volume": 1800,
        })

        result = assess_box(rows, now=datetime(2026, 3, 1, 18))

        self.assertEqual(result.state, BoxState.UPSIDE_BREAKOUT)

    def test_intraday_pierce_that_closes_back_inside_is_rejected_breakout(self):
        rows = box_bars(39)
        upper = max(row["high"] for row in rows)
        rows.append({
            "date": "2026-03-01",
            "open": 10.5,
            "high": upper + 0.8,
            "low": 10.0,
            "close": 10.5,
            "volume": 1800,
        })

        result = assess_box(rows, now=datetime(2026, 3, 1, 18))

        self.assertEqual(result.state, BoxState.REJECTED_BREAKOUT)

    def test_short_history_is_missing(self):
        result = assess_box(box_bars(10), now=datetime(2026, 2, 1, 18))

        self.assertEqual(result.state, BoxState.MISSING)

    def test_trend_assessment_reports_box_boundaries(self):
        rows = box_bars()

        result = assess_trend(
            rows,
            price=rows[-1]["close"],
            now=datetime(2026, 2, 12, 18),
        )

        self.assertEqual(result.short_trend, ShortTrend.BOX)
        self.assertEqual(result.evidence.box_state, BoxState.BOX.value)
        self.assertGreater(result.evidence.box_upper, result.evidence.box_lower)

    def test_summary_shows_box_boundaries(self):
        summary = TechnicalSummary.from_result({
            "price": 10.5,
            "entry_structure": {
                "trend_assessment": {
                    "short_trend": "箱体震荡",
                    "major_trend": "大趋势向上",
                    "conflict": "趋势尚未形成完整共振",
                    "evidence": {
                        "box_state": "箱体震荡",
                        "box_upper": 11.2,
                        "box_lower": 9.8,
                        "box_width_pct": 14.3,
                    },
                },
            },
        })

        text = format_summary(summary)

        self.assertIn("箱体上沿 11.20", text)
        self.assertIn("箱体下沿 9.80", text)

    def test_summary_shows_rejected_breakout_state(self):
        summary = TechnicalSummary.from_result({
            "entry_structure": {
                "trend_assessment": {
                    "short_trend": "箱体震荡",
                    "evidence": {
                        "box_state": "假突破/回到箱体",
                        "box_upper": 11.2,
                        "box_lower": 9.8,
                    },
                },
            },
        })

        text = format_summary(summary)

        self.assertIn("箱体状态 假突破/回到箱体", text)


if __name__ == "__main__":
    unittest.main()
