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
          atr_pct=None, code="sz002185", name="华天科技",
          major_trend=None, trend_conflict=None):
    return Sample(
        code=code, name=name, price=18.0, change_pct=change_pct,
        volume_ratio=volume_ratio, cumulative_turnover=cumulative,
        timestamp=timestamp, atr_pct=atr_pct, stock=True,
        major_trend=major_trend, trend_conflict=trend_conflict,
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
        # guard_actionable_copy 恒把 takeaway 换成「仅观察」模板——显式断言守卫口径。
        self.assertIn("仅观察", alert.body)
        self.assertNotIn("补仓/做T窗口", alert.body)

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


class TestDipReboundWatch(unittest.TestCase):
    """阶段一「反弹观察」两级信号与防诱多旗标。"""

    def _fire_stage1(self, engine, t0, low=-3.5, level=-2.4):
        """低点后持续回升，3 个采样 / 30 秒后触发阶段一。返回 alert。"""
        self.assertIsNone(step(engine, stock(low, volume_ratio=None, timestamp=t0)))
        self.assertIsNone(step(engine, stock(level + 0.1, volume_ratio=None, timestamp=t0 + 30)))
        self.assertIsNone(step(engine, stock(level, volume_ratio=None, timestamp=t0 + 45)))
        return step(engine, stock(level, volume_ratio=None, timestamp=t0 + 60))

    def test_stock_stage1_fires_on_sustained_shallow_recovery(self):
        engine = DipReboundEngine()
        alert = self._fire_stage1(engine, T0)
        self.assertIsNotNone(alert)
        self.assertEqual(alert.stage, 1)
        self.assertFalse(alert.vibrate)
        self.assertIn("反弹观察", alert.title)
        self.assertIn("仅观察", alert.body)
        self.assertIn("谨防诱多", alert.body)
        self.assertIn("再创新低即取消", alert.body)
        self.assertIn("观察门槛", alert.body)

    def test_stage1_requires_real_dip(self):
        engine = DipReboundEngine()
        # 浅下探（-1.0，未到 -3 门槛）回升再久也不提示。
        self.assertIsNone(step(engine, stock(-1.0, timestamp=T0)))
        for offset in (30, 45, 60, 75):
            self.assertIsNone(step(engine, stock(0.0, timestamp=T0 + offset)))

    def test_stage1_below_watch_threshold_silent(self):
        engine = DipReboundEngine()
        self.assertIsNone(step(engine, stock(-3.5, timestamp=T0)))
        # 回升 0.3~0.4 < 观察门槛 0.75：不提示。
        self.assertIsNone(step(engine, stock(-3.2, timestamp=T0 + 30)))
        self.assertIsNone(step(engine, stock(-3.1, timestamp=T0 + 45)))
        self.assertIsNone(step(engine, stock(-3.1, timestamp=T0 + 60)))

    def test_stage1_single_tick_flicker_does_not_fire(self):
        engine = DipReboundEngine()
        self.assertIsNone(step(engine, stock(-3.5, timestamp=T0)))
        self.assertIsNone(step(engine, stock(-2.5, timestamp=T0 + 30)))   # 站上门槛
        self.assertIsNone(step(engine, stock(-2.9, timestamp=T0 + 45)))   # 掉回去 → 清零
        self.assertIsNone(step(engine, stock(-2.5, timestamp=T0 + 60)))   # 重新计数
        self.assertIsNone(step(engine, stock(-2.4, timestamp=T0 + 75)))   # 仅 15 秒

    def test_stage1_requires_min_hold_duration(self):
        engine = DipReboundEngine()
        self.assertIsNone(step(engine, stock(-3.5, timestamp=T0)))
        self.assertIsNone(step(engine, stock(-2.5, timestamp=T0 + 60)))   # 首次站上门槛
        self.assertIsNone(step(engine, stock(-2.4, timestamp=T0 + 75)))   # 2 采样但仅 15 秒
        alert = step(engine, stock(-2.4, timestamp=T0 + 90))              # 30 秒整
        self.assertIsNotNone(alert)
        self.assertEqual(alert.stage, 1)

    def test_new_low_silently_cancels_watch_and_rearms(self):
        engine = DipReboundEngine()
        first = self._fire_stage1(engine, T0)
        self.assertIsNotNone(first)
        # 创新低：静默作废观察（无新 alert），下一段回升重新观察。
        self.assertIsNone(step(engine, stock(-4.0, timestamp=T0 + 75)))
        self.assertIsNone(step(engine, stock(-3.0, timestamp=T0 + 105)))
        self.assertIsNone(step(engine, stock(-2.9, timestamp=T0 + 120)))
        second = step(engine, stock(-2.9, timestamp=T0 + 135))
        self.assertIsNotNone(second)
        self.assertEqual(second.stage, 1)

    def test_stage1_weak_leg_flags_bull_trap(self):
        engine = DipReboundEngine()
        self.assertIsNone(step(engine, stock(-1.0, cumulative=100.0, timestamp=T0)))
        self.assertIsNone(step(engine, stock(-3.5, cumulative=200.0, timestamp=T0 + 60)))
        self.assertIsNone(step(engine, stock(-2.5, cumulative=230.0, timestamp=T0 + 180)))
        self.assertIsNone(step(engine, stock(-2.4, cumulative=233.0, timestamp=T0 + 195)))
        alert = step(engine, stock(-2.4, cumulative=236.0, timestamp=T0 + 210))
        self.assertIsNotNone(alert)
        self.assertIn("缩量反抽", alert.body)
        self.assertIn("诱多风险高", alert.body)
        self.assertFalse(alert.vibrate)

    def test_stage1_strong_leg_wording(self):
        engine = DipReboundEngine()
        self.assertIsNone(step(engine, stock(-1.0, cumulative=100.0, timestamp=T0)))
        self.assertIsNone(step(engine, stock(-3.5, cumulative=200.0, timestamp=T0 + 60)))
        self.assertIsNone(step(engine, stock(-2.5, cumulative=440.0, timestamp=T0 + 180)))
        self.assertIsNone(step(engine, stock(-2.4, cumulative=500.0, timestamp=T0 + 195)))
        alert = step(engine, stock(-2.4, cumulative=560.0, timestamp=T0 + 210))
        self.assertIsNotNone(alert)
        self.assertIn("量能不缩", alert.body)
        self.assertNotIn("缩量反抽", alert.body)
        self.assertNotIn("衰减", alert.body)

    def test_stage1_trend_down_wording(self):
        for kwargs in (
            {"major_trend": "大趋势向下"},
            {"major_trend": "DOWN"},
            {"trend_conflict": "下跌趋势中的反弹"},
            {"trend_conflict": "DOWN_MAJOR_REBOUND"},
        ):
            engine = DipReboundEngine()
            self.assertIsNone(step(engine, stock(-3.5, timestamp=T0, **kwargs)))
            self.assertIsNone(step(engine, stock(-2.5, timestamp=T0 + 30, **kwargs)))
            self.assertIsNone(step(engine, stock(-2.4, timestamp=T0 + 45, **kwargs)))
            alert = step(engine, stock(-2.4, timestamp=T0 + 60, **kwargs))
            self.assertIsNotNone(alert, kwargs)
            self.assertIn("下跌趋势中的反弹，仅观察", alert.body)
            self.assertFalse(alert.vibrate)

    def test_stage2_trend_down_never_vibrates(self):
        engine = DipReboundEngine()
        kwargs = {"major_trend": "DOWN"}
        self.assertIsNone(step(engine, stock(-1.0, cumulative=100.0, timestamp=T0, **kwargs)))
        self.assertIsNone(step(engine, stock(-3.5, cumulative=200.0, timestamp=T0 + 60, **kwargs)))
        alert = step(engine, stock(-1.4, cumulative=440.0, timestamp=T0 + 120, **kwargs))
        self.assertIsNotNone(alert)
        self.assertEqual(alert.stage, 2)
        # 量能节奏强（2.4 倍）本应振动，趋势向下压住不振动。
        self.assertFalse(alert.vibrate)
        self.assertIn("下跌趋势中的反弹，仅观察", alert.body)

    def test_stage2_upgrade_from_stage1(self):
        engine = DipReboundEngine()
        watch = self._fire_stage1(engine, T0)
        self.assertEqual(watch.stage, 1)
        # 量比 1.5（累计口径放量）→ 确认振动；标题带深跌回升。
        alert = step(engine, stock(-1.4, volume_ratio=1.5, timestamp=T0 + 75))
        self.assertIsNotNone(alert)
        self.assertEqual(alert.stage, 2)
        self.assertIn("深跌回升", alert.title)
        self.assertIn("自「反弹观察」升级确认", alert.body)
        self.assertTrue(alert.vibrate)

    def test_stage2_direct_fast_v_skips_stage1(self):
        engine = DipReboundEngine()
        self.assertIsNone(step(engine, stock(-3.5, volume_ratio=None, timestamp=T0)))
        alert = step(engine, stock(-1.4, volume_ratio=None, timestamp=T0 + 15))
        self.assertIsNotNone(alert)
        self.assertEqual(alert.stage, 2)
        self.assertNotIn("反弹观察", alert.title)
        self.assertNotIn("升级确认", alert.body)

    def test_stage1_daily_cap_two(self):
        engine = DipReboundEngine()
        alerts = []

        def run(seq):
            for ts, chg in seq:
                alert = step(engine, stock(chg, volume_ratio=None, timestamp=ts))
                if alert is not None:
                    alerts.append(alert)

        # 三段下探，每段持续回升到观察门槛但够不到确认门槛。
        run([(T0, -3.5), (T0 + 30, -2.4), (T0 + 45, -2.3), (T0 + 60, -2.3)])
        run([(T0 + 75, -4.5), (T0 + 105, -3.4), (T0 + 120, -3.3), (T0 + 135, -3.3)])
        run([(T0 + 150, -5.5), (T0 + 180, -4.4), (T0 + 195, -4.3), (T0 + 210, -4.3)])
        self.assertEqual(len(alerts), 2)
        self.assertTrue(all(a.stage == 1 for a in alerts))

    def test_spike_flag_on_fast_rebound(self):
        engine = DipReboundEngine()
        self.assertIsNone(step(engine, stock(-3.5, volume_ratio=None, timestamp=T0)))
        self.assertIsNone(step(engine, stock(-2.5, volume_ratio=None, timestamp=T0 + 15)))
        self.assertIsNone(step(engine, stock(-2.4, volume_ratio=None, timestamp=T0 + 30)))
        alert = step(engine, stock(-2.4, volume_ratio=None, timestamp=T0 + 45))
        self.assertIsNotNone(alert)
        self.assertIn("急拉", alert.body)

    def test_no_spike_flag_on_slow_rebound(self):
        engine = DipReboundEngine()
        self.assertIsNone(step(engine, stock(-3.5, volume_ratio=None, timestamp=T0)))
        self.assertIsNone(step(engine, stock(-2.5, volume_ratio=None, timestamp=T0 + 120)))
        self.assertIsNone(step(engine, stock(-2.4, volume_ratio=None, timestamp=T0 + 135)))
        alert = step(engine, stock(-2.4, volume_ratio=None, timestamp=T0 + 150))
        self.assertIsNotNone(alert)
        self.assertNotIn("急拉", alert.body)

    def test_fade_flag_on_dying_bounce(self):
        engine = DipReboundEngine()
        self.assertIsNone(step(engine, stock(-1.0, cumulative=100.0, timestamp=T0)))
        self.assertIsNone(step(engine, stock(-3.5, cumulative=200.0, timestamp=T0 + 60)))
        # 低点后一波脉冲成交，随后 90 秒几乎无量。
        self.assertIsNone(step(engine, stock(-2.5, cumulative=400.0, timestamp=T0 + 90)))
        alert = step(engine, stock(-2.4, cumulative=405.0, timestamp=T0 + 180))
        self.assertIsNotNone(alert)
        self.assertIn("衰减", alert.body)

    def test_market_weak_dampens_stock_copy(self):
        engine = DipReboundEngine()
        day = "2026-10-09"
        market = index(-1.5, timestamp=T0)
        self.assertEqual(engine.update([market, stock(-3.5, volume_ratio=None, timestamp=T0)], day), [])
        self.assertEqual(engine.update([market, stock(-2.5, volume_ratio=None, timestamp=T0 + 30)], day), [])
        self.assertEqual(engine.update([market, stock(-2.4, volume_ratio=None, timestamp=T0 + 45)], day), [])
        alerts = engine.update([market, stock(-2.4, volume_ratio=None, timestamp=T0 + 60)], day)
        self.assertEqual(len(alerts), 1)
        self.assertIn("大盘仍在走弱", alerts[0].body)
        self.assertIn("上证 -1.5%", alerts[0].body)

    def test_index_no_stage1(self):
        engine = DipReboundEngine()
        self.assertIsNone(step(engine, index(-0.9, timestamp=T0)))
        # 回升 0.4~0.45 低于指数确认门槛 0.5；指数不走观察分支，只等确认。
        self.assertIsNone(step(engine, index(-0.5, timestamp=T0 + 30)))
        self.assertIsNone(step(engine, index(-0.45, timestamp=T0 + 45)))
        self.assertIsNone(step(engine, index(-0.45, timestamp=T0 + 60)))
        alert = step(engine, index(-0.2, timestamp=T0 + 75))
        self.assertIsNotNone(alert)
        self.assertEqual(alert.stage, 2)
        self.assertNotIn("反弹观察", alert.title)
        self.assertNotIn("大盘仍在走弱", alert.body)

    def test_stage1_stall_flag_selling_pressure(self):
        engine = DipReboundEngine()
        self.assertIsNone(step(engine, stock(-1.0, cumulative=100.0, timestamp=T0)))
        self.assertIsNone(step(engine, stock(-3.5, cumulative=200.0, timestamp=T0 + 60)))
        self.assertIsNone(step(engine, stock(-2.5, cumulative=500.0, timestamp=T0 + 120)))
        # 近 1 分钟成交 300（节奏不减）但价格原地踏步：放量滞涨。
        alert = step(engine, stock(-2.5, cumulative=800.0, timestamp=T0 + 180))
        self.assertIsNotNone(alert)
        self.assertEqual(alert.stage, 1)
        self.assertIn("放量滞涨", alert.body)
        self.assertIn("抛压较重", alert.body)

    def test_stage2_selling_pressure_never_vibrates(self):
        engine = DipReboundEngine()
        self.assertIsNone(step(engine, stock(-1.0, cumulative=100.0, timestamp=T0)))
        self.assertIsNone(step(engine, stock(-3.5, cumulative=200.0, timestamp=T0 + 60)))
        # 回升 1.99 尚未到确认门槛 2.0。
        self.assertIsNone(step(engine, stock(-1.51, cumulative=500.0, timestamp=T0 + 120)))
        # 站上确认门槛，但近 1 分钟放量滞涨 → 量能再强也不振动。
        alert = step(engine, stock(-1.5, cumulative=800.0, timestamp=T0 + 180))
        self.assertIsNotNone(alert)
        self.assertEqual(alert.stage, 2)
        self.assertIn("量能不缩", alert.body)  # 节奏本身是强的
        self.assertFalse(alert.vibrate)       # 但抛压压住振动
        self.assertIn("放量滞涨", alert.body)

    def test_alert_default_stage_is_two(self):
        from dip_rebound import Alert
        alert = Alert(code="x", title="t", body="b", vibrate=False, stock=True)
        self.assertEqual(alert.stage, 2)


if __name__ == "__main__":
    unittest.main()
