#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""直接撤出话术门槛测试（trade_action_guard.allows_exit_directive）。"""

import unittest

from trade_action_guard import (
    SMALL_POSITION_SHARE_PCT,
    allows_exit_directive,
    position_cost_share,
)


class PositionCostShareTests(unittest.TestCase):
    def test_share_uses_cost_basis_over_portfolio(self):
        portfolio = {"positions": [
            {"code": "a", "quantity": 100, "cost": 10.0},
            {"code": "b", "quantity": 1000, "cost": 10.0},
        ]}
        # 1000 / 11000 ≈ 9.09%
        self.assertAlmostEqual(
            position_cost_share(portfolio, 100, 10.0), 9.09, places=1,
        )

    def test_share_is_none_without_usable_portfolio(self):
        self.assertIsNone(position_cost_share({}, 100, 10.0))
        self.assertIsNone(position_cost_share({"positions": []}, 100, 10.0))
        self.assertIsNone(position_cost_share({"positions": [{"quantity": 0}]}, 100, 10.0))

    def test_bad_inputs_are_none(self):
        self.assertIsNone(position_cost_share({"positions": []}, "x", 10.0))
        self.assertIsNone(position_cost_share({"positions": []}, 0, 10.0))


class AllowsExitDirectiveTests(unittest.TestCase):
    def test_red_fundamentals_allow_exit(self):
        self.assertTrue(allows_exit_directive("ALERT", "LOW", 3.0))

    def test_high_price_allows_exit(self):
        self.assertTrue(allows_exit_directive("SOUND", "HIGH", 3.0))

    def test_heavy_position_allows_exit(self):
        self.assertTrue(allows_exit_directive(
            "SOUND", "LOW", SMALL_POSITION_SHARE_PCT + 0.5,
        ))

    def test_light_low_sound_position_blocks_exit(self):
        # 2026-10-22 招商证券：基本面好、位置低位、小仓位 → 不直接撤出。
        self.assertFalse(allows_exit_directive("SOUND", "LOW", 3.0))

    def test_missing_evidence_blocks_exit(self):
        # 缺证据不喊撤出：基本面未分析 / 仓位算不出都按不允许处理。
        self.assertFalse(allows_exit_directive(None, "LOW", None))
        self.assertFalse(allows_exit_directive("SOUND", None, None))


if __name__ == "__main__":
    unittest.main()
