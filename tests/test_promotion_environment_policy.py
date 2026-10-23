#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import unittest

from promotion_environment_policy import (
    PromotionEnvironmentInput,
    evaluate_environment,
)


class PromotionEnvironmentPolicyTests(unittest.TestCase):
    def test_default_disabled_records_evidence_without_blocking(self):
        decision = evaluate_environment(
            PromotionEnvironmentInput(-1.0, 0.0, 10, 30, [-2.0])
        )
        self.assertFalse(decision.blocked)
        self.assertTrue(decision.evidence)
        self.assertIsNone(decision.reason)

    def test_enabled_index_threshold_blocks(self):
        decision = evaluate_environment(
            PromotionEnvironmentInput(-0.8, 0.0, 100, 1, []), enabled=True
        )
        self.assertTrue(decision.blocked)
        self.assertIn("主要指数", decision.reason)

    def test_enabled_limit_down_breadth_blocks(self):
        decision = evaluate_environment(
            PromotionEnvironmentInput(0.0, 0.0, 19, 20, []), enabled=True
        )
        self.assertTrue(decision.blocked)
        self.assertIn("跌停数量", decision.reason)

    def test_enabled_all_known_boards_weak_blocks(self):
        decision = evaluate_environment(
            PromotionEnvironmentInput(0.0, 0.0, 100, 1, [-1.0, -1.2]), enabled=True
        )
        self.assertTrue(decision.blocked)
        self.assertIn("板块同步转弱", decision.reason)

    def test_unknown_data_does_not_block(self):
        decision = evaluate_environment(
            PromotionEnvironmentInput(None, None, None, None, [None]), enabled=True
        )
        self.assertFalse(decision.blocked)
        self.assertIsNone(decision.reason)

    def test_partial_weak_boards_do_not_block(self):
        decision = evaluate_environment(
            PromotionEnvironmentInput(0.0, 0.0, 10, 1, [-2.0, 1.0]), enabled=True
        )
        self.assertFalse(decision.blocked)


if __name__ == "__main__":
    unittest.main()
