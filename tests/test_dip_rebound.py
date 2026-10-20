#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""下探回升提醒测试。与 Android 版 DipReboundEngineTest 一致。"""

import unittest

from dip_rebound import DipReboundEngine, Sample

T0 = 1_000_000.0


def index(change_pct, turnover_ratio=1.4, cumulative=None, timestamp=0.0,
          code="sh000001", name="上证指数"):
    return Sample(
        code=code, name=name, price=4000.0, change_pct=change_pct,
        volume_ratio=turnover_ratio, cumulative_turnover=cumulative,
        timestamp=timestamp, stock=False,
    )


def stock(change_pct, volume_ratio=1.5, cumulative=None, timestamp=0.0,
          atr_pct=None, code="sz002185", name="华天科技"):
    return Sample(
        code=code, name=name, price=18.0, change_pct=change_pct,
        volume_ratio=volume_ratio, cumulative_turnover=cumulative,
        timestamp=timestamp, atr_pct=atr_pct, stock=True,
    )


def step(engine, sample, day="2026-10-09"):
    alerts = engine.update([sample], day)
    return alerts[0] if alerts else None


class TestDipRebound(unittest.TestCase):
    def test_index_v_recovery_with_volume_vibrates(self):
        engine = DipReboundEngine()
        step(engine, index(-0.9))
        step(engine, index(-0.8))
        alert = step(engine, index(-0.2))
        self.assertIsNotNone(alert)
        self.assertTrue(alert.vibrate)
        self.assertFalse(alert.stock)
        self.assertIn("下探回升", alert.title)
        self.assertIn("补仓", alert.body)

    def test_index_recovery_without_volume_is_soft(self):
        engine = DipReboundEngine()
        step(engine, index(-0.9, turnover_ratio=0.9))
        alert = step(engine, index(-0.2, turnover_ratio=0.9))
        self.assertFalse(alert.vibrate)
        self.assertIn("超跌反弹", alert.body)

    def test_shallow_dip_stays_silent(self):
        engine = DipReboundEngine()
        step(engine, index(-0.5))
        self.assertIsNone(step(engine, index(0.1)))

    def test_tiny_rebound_of_deep_dip_stays_silent(self):
        engine = DipReboundEngine()
        step(engine, index(-2.0))
        self.assertIsNone(step(engine, index(-1.4)))

    def test_fires_once_per_dip_episode(self):
        engine = DipReboundEngine()
        step(engine, index(-0.9))
        self.assertIsNotNone(step(engine, index(-0.2)))
        self.assertIsNone(step(engine, index(0.2)))
        self.assertIsNone(step(engine, index(0.5)))

    def test_rearms_after_deeper_low(self):
        engine = DipReboundEngine()
        step(engine, index(-0.9))
        step(engine, index(-0.2))
        # 没创新低的回落不重新武装。
        step(engine, index(-0.85))
        self.assertIsNone(step(engine, index(-0.3)))
        # 创出更深新低后再次收复才算新一段下探。
        step(engine, index(-1.5))
        self.assertIsNotNone(step(engine, index(-0.4)))

    def test_chinext_needs_deeper_dip(self):
        engine = DipReboundEngine()
        step(engine, index(-0.9, code="sz399006", name="创业板指"))
        self.assertIsNone(step(engine, index(-0.1, code="sz399006", name="创业板指")))
        step(engine, index(-1.3, code="sz399006", name="创业板指"))
        alert = step(engine, index(-0.5, code="sz399006", name="创业板指"))
        self.assertIsNotNone(alert)

    def test_stock_needs_deeper_dip_and_bigger_rebound(self):
        engine = DipReboundEngine()
        step(engine, stock(-2.9))
        self.assertIsNone(step(engine, stock(-0.5)))
        step(engine, stock(-3.5))
        self.assertIsNone(step(engine, stock(-2.0)))
        alert = step(engine, stock(-1.2))
        self.assertTrue(alert.stock)
        self.assertIn("仅观察", alert.body)
        self.assertNotIn("做T减仓", alert.body)

    def test_stock_dip_threshold_scales_with_atr(self):
        engine = DipReboundEngine()
        step(engine, stock(-3.0, atr_pct=3.0))
        self.assertIsNone(step(engine, stock(-1.0, atr_pct=3.0)))
        step(engine, stock(-3.8, atr_pct=3.0))
        self.assertIsNotNone(step(engine, stock(-1.2, atr_pct=3.0)))

    def test_rebound_leg_pace_decides_not_cumulative_ratio(self):
        engine = DipReboundEngine()
        step(engine, index(-0.2, turnover_ratio=1.6, cumulative=20.0, timestamp=T0))
        step(engine, index(-1.0, turnover_ratio=1.6, cumulative=80.0, timestamp=T0 + 60 * 60))
        # 回升段 30 分钟只成交 3：缩量反弹，累计倍数再高也不判「放量」。
        alert = step(engine, index(-0.3, turnover_ratio=1.6, cumulative=83.0, timestamp=T0 + 90 * 60))
        self.assertFalse(alert.vibrate)
        self.assertIn("量能缩了", alert.body)

    def test_heavy_rebound_leg_vibrates_even_when_cumulative_weak(self):
        engine = DipReboundEngine()
        step(engine, index(-0.2, turnover_ratio=1.2, cumulative=20.0, timestamp=T0))
        step(engine, index(-1.0, turnover_ratio=1.2, cumulative=80.0, timestamp=T0 + 60 * 60))
        # 回升段 30 分钟成交 40（1.33/分钟 ≥ 下跌段 1.1 倍）：资金承接。
        alert = step(engine, index(-0.3, turnover_ratio=1.2, cumulative=120.0, timestamp=T0 + 90 * 60))
        self.assertTrue(alert.vibrate)
        self.assertIn("量能不缩", alert.body)

    def test_daily_cap_stops_spam(self):
        engine = DipReboundEngine()
        fired = 0
        low = -0.9
        for _ in range(6):
            step(engine, index(low))
            if step(engine, index(low * 0.4)) is not None:
                fired += 1
            low -= 1.0
        self.assertEqual(fired, 3)

    def test_day_change_resets_state(self):
        engine = DipReboundEngine()
        step(engine, index(-0.9), day="2026-10-09")
        step(engine, index(-0.2), day="2026-10-09")
        step(engine, index(-0.9), day="2026-10-10")
        self.assertIsNotNone(step(engine, index(-0.2), day="2026-10-10"))


if __name__ == "__main__":
    unittest.main()
