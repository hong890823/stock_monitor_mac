from datetime import datetime
import math
import unittest

import pandas as pd

from portfolio_addition import (
    AdditionRecommendation,
    Bar,
    FundamentalSnapshot,
    HoldingAssessmentInput,
    calculate_ma250,
    fundamental_blockers,
    resample_ohlcv,
    rank_addition_candidates,
    score_period,
    score_technical,
    score_fundamental,
    select_addition_candidate,
    suggest_add_quantity,
)


def daily_frame():
    rows = []
    for day in range(30):
        date = pd.Timestamp("2026-01-05") + pd.Timedelta(days=day)
        rows.append({
            "date": date.strftime("%Y-%m-%d"),
            "open": 10 + day * 0.1,
            "high": 11 + day * 0.1,
            "low": 9 + day * 0.1,
            "close": 10.5 + day * 0.1,
            "volume": 1000 + day,
        })
    return pd.DataFrame(rows)


class PeriodResamplingTests(unittest.TestCase):
    def test_weekly_resampling_marks_current_week_incomplete(self):
        result = resample_ohlcv(daily_frame(), "W", datetime(2026, 1, 20, 15))
        self.assertEqual(result.frequency, "W")
        self.assertLess(result.completed[-1].date, "2026-01-20")
        self.assertIsNotNone(result.current)
        self.assertFalse(result.current.completed)

    def test_ma250_requires_enough_valid_closes(self):
        self.assertIsNone(calculate_ma250(daily_frame()))


def uptrend_bars(count=80):
    rows = []
    for index in range(count):
        date = pd.Timestamp("2020-01-01") + pd.Timedelta(days=index)
        close = 10 + index * 0.2
        rows.append(Bar(
            str(date.date()), close - 0.2, close + 0.4, close - 0.5,
            close, 1000 + index * 10,
        ))
    return tuple(rows)


class TimeframeScoringTests(unittest.TestCase):
    def test_three_completed_year_bars_can_be_scored(self):
        self.assertGreaterEqual(
            score_period(uptrend_bars(3), 10.4, 9.0),
            0,
        )

    def test_strong_period_scores_high(self):
        bars = uptrend_bars()
        self.assertGreaterEqual(
            score_period(bars, bars[-1].close, bars[-1].close - 5),
            80,
        )

    def test_technical_score_uses_approved_period_weights(self):
        technical = {"score": 5, "entry_can_buy": True, "trend_weak": False}
        self.assertEqual(
            score_technical(technical, 100.0, 100.0, 100.0),
            100.0,
        )

    def test_ma250_participates_in_period_score(self):
        bars = uptrend_bars()
        self.assertGreater(
            score_period(bars, bars[-1].close, bars[-1].close - 5),
            score_period(bars, bars[-1].close, bars[-1].close + 5),
        )


class FundamentalScoringTests(unittest.TestCase):
    def test_alert_fundamental_blocks_addition(self):
        snapshot = FundamentalSnapshot(
            analyzed_at="2026-10-15", score=90, health_level="ALERT",
            revenue_yoy=10, profit_yoy=12, deduct_ratio=0.9, gross_margin=30,
            net_margin=15, ocf_to_profit=1.0, roe=12, current_pe=15,
            current_pb=2, pe_percentile=30, pb_percentile=30,
            fair_low=8, fair_high=12, risks=("立案调查",),
        )
        self.assertTrue(fundamental_blockers(snapshot, 10.0))

    def test_sound_fundamental_scores_high(self):
        snapshot = FundamentalSnapshot(
            analyzed_at="2026-10-15", score=90, health_level="SOUND",
            revenue_yoy=15, profit_yoy=20, deduct_ratio=0.95, gross_margin=35,
            net_margin=18, ocf_to_profit=1.1, roe=15, current_pe=15,
            current_pb=2, pe_percentile=25, pb_percentile=25,
            fair_low=8, fair_high=12,
        )
        self.assertGreaterEqual(score_fundamental(snapshot, 10.0), 85)
        self.assertEqual(fundamental_blockers(snapshot, 10.0), ())

    def test_two_quarter_double_decline_blocks_addition(self):
        snapshot = FundamentalSnapshot(
            analyzed_at="2026-10-15", score=70, health_level="NEUTRAL",
            revenue_yoy=-22, profit_yoy=-25, deduct_ratio=0.8,
            gross_margin=25, net_margin=10, ocf_to_profit=0.8, roe=8,
            current_pe=15, current_pb=2, pe_percentile=40, pb_percentile=40,
            fair_low=8, fair_high=12,
            quarter_trends=((-25, -30), (-22, -25)),
        )
        self.assertTrue(any(
            "最近两季" in reason
            for reason in fundamental_blockers(snapshot, 10.0)
        ))


class SizingAndRankingTests(unittest.TestCase):
    def test_quantity_is_limited_by_risk_budget_and_rounded_down(self):
        shares = suggest_add_quantity(
            price=10.0, stop_price=9.5, total_capital=100000,
            available_capital=50000, current_value=10000,
            max_position_pct=20,
        )
        self.assertEqual(shares, 1000)

    def test_all_candidates_below_threshold_returns_none(self):
        candidates = (
            AdditionRecommendation(
                code="sh600001", name="甲", selected=False,
                composite_score=69, technical_score=70, fundamental_score=68,
                yearly_score=75, monthly_score=72, weekly_score=70,
                shares=0, amount=0, stop_price=9, position_pct_before=10,
                position_pct_after=10, reasons=("综合分不足",), missing=(),
            ),
        )
        self.assertIsNone(rank_addition_candidates(candidates))


class CandidateSelectionTests(unittest.TestCase):
    def test_no_fundamental_snapshot_is_not_recommended(self):
        source = pd.DataFrame([{
            "date": (datetime(2025, 1, 1) + pd.Timedelta(days=index)).strftime("%Y-%m-%d"),
            "open": 10, "high": 11, "low": 9,
            "close": 10 + index * 0.05, "volume": 1000,
        } for index in range(1250)])
        item = HoldingAssessmentInput(
            "sh600001", "甲", 1000, 10, 15, 100000, 50000, 20,
            source, {"score": 6, "entry_can_buy": True, "trend_weak": False},
            None,
        )
        self.assertIsNone(
            select_addition_candidate([item], datetime(2026, 10, 15, 15))
        )

    def test_selects_one_eligible_holding(self):
        rows = []
        trading_days = pd.bdate_range("2020-01-01", "2026-10-15")
        base_end = len(trading_days) - 60
        for index, date in enumerate(trading_days):
            if index < base_end:
                close = 10 + index * 0.012
            else:
                offset = index - base_end
                close = 28.2 - min(offset, 30) * 0.04 + max(0, offset - 30) * 0.02
            rows.append({
                "date": date.strftime("%Y-%m-%d"),
                "open": close - 0.05,
                "high": close + 0.20,
                "low": close - 0.20,
                "close": close,
                "volume": 1000 + index % 80,
            })
        snapshot = FundamentalSnapshot(
            analyzed_at="2026-10-15", score=90, health_level="SOUND",
            revenue_yoy=15, profit_yoy=20, deduct_ratio=0.95,
            gross_margin=35, net_margin=18, ocf_to_profit=1.1, roe=15,
            current_pe=15, current_pb=2, pe_percentile=25,
            pb_percentile=25, fair_low=20, fair_high=35,
            quarter_trends=((10, 12), (15, 20)),
        )
        item = HoldingAssessmentInput(
            code="sh600001",
            name="甲",
            quantity=1000,
            cost=20,
            price=28.0,
            total_capital=200000,
            available_capital=50000,
            max_position_pct=20,
            daily_history=pd.DataFrame(rows),
            technical={
                "score": 6,
                "entry_can_buy": True,
                "trend_weak": False,
                "entry_stop_price": 26.6,
                "weekly_stop_price": 26.4,
            },
            fundamental=snapshot,
        )
        selected = select_addition_candidate(
            [item], datetime(2026, 10, 15, 15)
        )
        self.assertIsNotNone(selected)
        self.assertEqual(selected.code, "sh600001")
        self.assertGreater(selected.shares, 0)


if __name__ == "__main__":
    unittest.main()
