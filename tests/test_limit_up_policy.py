#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import unittest

from limit_up_policy import LimitCandidatePolicy, rank_limit_down, rank_limit_up, seal_quality_score


def stock(code, **overrides):
    row = {
        "code": code,
        "name": code,
        "source": "EASTMONEY_LIMIT_POOL",
        "limit_streak": 1,
        "open_count": 1,
        "seal_to_float_pct": 0.5,
        "seal_to_turnover_pct": 9.0,
        "change_pct": 10.0,
        "first_seal_time": "09:35:00",
        "last_seal_time": "14:00:00",
        "main_net_ratio_pct": None,
    }
    row.update(overrides)
    return row


class LimitCandidatePolicyTests(unittest.TestCase):
    def test_admission_boundaries(self):
        self.assertTrue(LimitCandidatePolicy.eligible(stock("a")))
        self.assertTrue(
            LimitCandidatePolicy.eligible(
                stock("b", seal_to_float_pct=None, seal_to_turnover_pct=10.0)
            )
        )
        self.assertFalse(LimitCandidatePolicy.eligible(stock("c", source="SINA_SCAN")))
        self.assertFalse(LimitCandidatePolicy.eligible(stock("d", open_count=2)))
        self.assertFalse(LimitCandidatePolicy.eligible(stock("e", open_count=-1)))
        self.assertFalse(LimitCandidatePolicy.eligible(stock("f", limit_streak=0)))
        self.assertFalse(
            LimitCandidatePolicy.eligible(
                stock("g", seal_to_float_pct=0.49, seal_to_turnover_pct=9.9)
            )
        )

    def test_seal_quality_is_bounded_and_coverage_is_auditable(self):
        strong = stock(
            "strong",
            open_count=0,
            seal_to_float_pct=10.0,
            seal_to_turnover_pct=50.0,
            limit_streak=4,
            main_net_ratio_pct=2.0,
        )
        result = seal_quality_score(strong)
        self.assertEqual(result.score, 100)
        self.assertEqual(result.coverage_pct, 100)
        self.assertGreater(result.components["seal_to_float"], 35)

    def test_limit_up_orders_by_seal_quality(self):
        weak = stock("weak", seal_to_float_pct=0.2, seal_to_turnover_pct=20.0, open_count=1)
        strong = stock("strong", seal_to_float_pct=2.0, seal_to_turnover_pct=30.0, open_count=0)
        rows = rank_limit_up([weak, strong])
        self.assertEqual([row["code"] for row in rows], ["strong", "weak"])
        self.assertGreater(rows[0]["seal_quality_score"], rows[1]["seal_quality_score"])
        self.assertIn("seal_quality_coverage_pct", rows[0])

    def test_limit_down_uses_change_order_without_limit_score(self):
        rows = rank_limit_down([stock("a", change_pct=-8.0), stock("b", change_pct=-10.0)])
        self.assertEqual([row["code"] for row in rows], ["b", "a"])
        self.assertNotIn("seal_quality_score", rows[0])
        self.assertNotIn("seal_quality_coverage_pct", rows[0])


if __name__ == "__main__":
    unittest.main()
