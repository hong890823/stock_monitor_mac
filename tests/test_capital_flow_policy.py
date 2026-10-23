#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import unittest
from datetime import datetime

from capital_flow import CapitalFlowSnapshot
from capital_flow_policy import (
    FundFlowTone,
    assess_single_day,
    assess_window,
    score_delta,
)


def snapshot(day, net_ratio, turnover_yi=1.0):
    date = f"2026-10-{day:02d}"
    return CapitalFlowSnapshot(
        key="sh600000",
        name="样本",
        kind="stock",
        quote_date=date,
        as_of=datetime.fromisoformat(f"{date}T15:00:00"),
        source="EastMoney.fflow.kline",
        scope="个股",
        session="daily",
        net_yi=turnover_yi * net_ratio / 100,
        net_ratio_pct=net_ratio,
        turnover_yi=turnover_yi,
    )


class FundFlowPolicyTests(unittest.TestCase):
    def test_single_day_four_directions(self):
        cases = (
            (1.0, 3.0, FundFlowTone.SUPPORTIVE),
            (1.0, -3.0, FundFlowTone.DIVERGENT),
            (-1.0, -3.0, FundFlowTone.RISK),
            (-1.0, 3.0, FundFlowTone.ABSORPTION_WATCH),
        )
        for change_pct, net_ratio, expected in cases:
            with self.subTest(change_pct=change_pct, net_ratio=net_ratio):
                result = assess_single_day(change_pct, snapshot(23, net_ratio))
                self.assertEqual(result.tone, expected)
                self.assertFalse(result.low_confidence)

    def test_small_turnover_is_low_confidence_and_zero_score(self):
        result = assess_single_day(1.0, snapshot(23, 4.0, turnover_yi=0.049))
        self.assertTrue(result.low_confidence)
        self.assertEqual(score_delta(result), 0)

    def test_missing_turnover_is_low_confidence(self):
        flow = snapshot(23, 4.0)
        flow = CapitalFlowSnapshot(**{**flow.__dict__, "turnover_yi": None})
        result = assess_single_day(1.0, flow)
        self.assertTrue(result.low_confidence)
        self.assertEqual(score_delta(result), 0)

    def test_sustained_three_day_inflow_adds_one_point(self):
        flows = [snapshot(21, 3.0), snapshot(22, 4.0), snapshot(23, 2.0)]
        result = assess_window(flows, change_pct=1.0)
        self.assertEqual(result.tone, FundFlowTone.SUSTAINED_INFLOW)
        self.assertEqual(result.score_delta, 1)
        self.assertFalse(result.low_confidence)

    def test_sustained_three_day_outflow_blocks_promotion(self):
        flows = [snapshot(21, -4.0), snapshot(22, -3.0), snapshot(23, -2.0)]
        result = assess_window(flows, change_pct=-1.0)
        self.assertEqual(result.tone, FundFlowTone.SUSTAINED_OUTFLOW)
        self.assertTrue(result.blocks_promotion)
        self.assertEqual(result.score_delta, 0)

    def test_two_positive_days_and_small_mean_do_not_confirm_inflow(self):
        flows = [snapshot(21, 1.0), snapshot(22, 4.0), snapshot(23, 3.0)]
        result = assess_window(flows, change_pct=1.0)
        self.assertEqual(result.tone, FundFlowTone.NEUTRAL)
        self.assertEqual(result.score_delta, 0)

    def test_insufficient_or_duplicate_dates_accumulate_without_scoring(self):
        incomplete = assess_window([snapshot(22, 4.0), snapshot(23, 4.0)], change_pct=1.0)
        self.assertEqual(incomplete.tone, FundFlowTone.ACCUMULATING)
        self.assertEqual(incomplete.score_delta, 0)

        duplicate = assess_window(
            [snapshot(23, 4.0), snapshot(23, 4.0), snapshot(23, 4.0)],
            change_pct=1.0,
        )
        self.assertEqual(duplicate.tone, FundFlowTone.ACCUMULATING)
        self.assertEqual(duplicate.score_delta, 0)

    def test_any_low_turnover_sample_disables_window_score(self):
        flows = [
            snapshot(21, 3.0),
            snapshot(22, 4.0, turnover_yi=0.01),
            snapshot(23, 2.0),
        ]
        result = assess_window(flows, change_pct=1.0)
        self.assertTrue(result.low_confidence)
        self.assertEqual(result.score_delta, 0)


if __name__ == "__main__":
    unittest.main()
