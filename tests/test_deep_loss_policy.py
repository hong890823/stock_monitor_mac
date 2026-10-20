#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""深套口径测试：阈值按 ATR 缩放、补仓数量减半。与 Android 版 DeepLossPolicyTest 一致。"""

import unittest

from deep_loss_policy import (
    deep_loss_threshold_pct,
    floating_pnl_pct,
    is_deep_loss,
    suggested_add_quantity,
)


class TestDeepLossPolicy(unittest.TestCase):
    def test_threshold_falls_back_without_atr(self):
        self.assertEqual(deep_loss_threshold_pct(None), -20.0)
        self.assertEqual(deep_loss_threshold_pct(0.0), -20.0)

    def test_threshold_scales_with_atr_and_clips(self):
        # ATR 2.5%：8 个 ATR 日 = -20%
        self.assertAlmostEqual(deep_loss_threshold_pct(2.5), -20.0)
        # 波动很小的股截到 -12%
        self.assertAlmostEqual(deep_loss_threshold_pct(1.0), -12.0)
        # 波动很大的股截到 -30%
        self.assertAlmostEqual(deep_loss_threshold_pct(5.0), -30.0)

    def test_is_deep_loss_uses_scaled_threshold(self):
        # -22% 浮亏：题材股（阈值 -28%）不算深套，稳态股（阈值 -12%）算。
        self.assertFalse(is_deep_loss(38.5, 30.0, atr_pct=3.5))
        self.assertTrue(is_deep_loss(38.5, 30.0, atr_pct=1.0))

    def test_floating_pnl(self):
        self.assertAlmostEqual(floating_pnl_pct(45.0, 30.0), -33.333, places=2)
        self.assertIsNone(floating_pnl_pct(0, 30.0))

    def test_add_quantity_is_half_of_initial_entry(self):
        qty = suggested_add_quantity(
            price=30.0,
            holding_quantity=0.0,
            total_capital=400_000.0,
            available_capital=0.0,
            initial_entry_pct=5.0,
            max_position_pct=20.0,
        )
        # 5% 的一半 = 10000 元 → 333 股 → 按手取整 300
        self.assertEqual(qty, 300.0)

    def test_add_quantity_respects_headroom_and_cash(self):
        self.assertEqual(
            suggested_add_quantity(30.0, 700.0, 100_000.0, 50_000.0, 5.0, 20.0),
            0.0,
        )
        self.assertEqual(
            suggested_add_quantity(30.0, 100.0, 100_000.0, 1_000.0, 5.0, 20.0),
            0.0,
        )


if __name__ == "__main__":
    unittest.main()
