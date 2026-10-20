#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""入场结构判断的三态与失效定义测试。

锁住三条关键行为：
1. 三态划分明确，不给模棱两可的结论；
2. 失败模式（无量突破、M20下行、假反抽）必须落到「暂未形成有效入场结构」；
3. 基本面 × 技术位置的组合判断覆盖五种情形。
"""

import unittest

from entry_structure import (
    EntryInput,
    EntryState,
    FundamentalBucket,
    Ma20Slope,
    VolumeState,
    combine_fundamental_and_technical,
    compute_entry_input,
    evaluate,
    fundamental_bucket_from_score,
)


def _breakout_input(**overrides):
    """放量突破前高的标准输入。"""
    base = dict(
        price=105.0,
        prior_high=100.0,
        ma20=98.0,
        ma20_prev=97.0,          # M20 上行
        volume_ratio=1.8,        # 放量
        closes_above_prior_high=1,
        closes_below_ma20=0,
        today_change_pct=2.0,
        holding=False,
        benchmark_ok=True,
    )
    base.update(overrides)
    return EntryInput(**base)


def _pullback_input(**overrides):
    """回踩上行 M20 的标准输入。"""
    base = dict(
        price=98.5,
        prior_high=105.0,
        ma20=98.0,
        ma20_prev=97.0,          # M20 上行
        volume_ratio=0.7,        # 缩量
        closes_above_prior_high=0,
        closes_below_ma20=0,
        today_change_pct=0.4,    # 收红止跌
        holding=False,
        benchmark_ok=True,
    )
    base.update(overrides)
    return EntryInput(**base)


class TestBreakout(unittest.TestCase):
    def test_valid_breakout_is_observation_only(self):
        """突破结构成立，但降级为观察信号——回测显示负超额，不再作为买入触发。"""
        entry = evaluate(_breakout_input())
        self.assertEqual(entry.state, EntryState.BREAKOUT)
        self.assertFalse(entry.can_buy)
        self.assertFalse(entry.trend_weak)
        self.assertTrue(any("跑输无条件买入" in b for b in entry.blockers))
        self.assertIn("仅作观察", entry.action)

    def test_breakout_still_reports_structure(self):
        """降级不等于不识别：结构、依据、止损位照常输出。"""
        entry = evaluate(_breakout_input())
        self.assertTrue(any("站上前期高点" in r for r in entry.reasons))
        self.assertIsNotNone(entry.stop_price)

    def test_breakout_uses_atr_stop(self):
        """止损位用 ATR 自适应，不再固定 2%。"""
        entry = evaluate(_breakout_input(atr=2.0))
        # 入场价 105，ATR 2.0 → stop = 105 − 1.5×2 = 102.0
        self.assertAlmostEqual(entry.stop_price, 102.0, places=4)
        self.assertIn("ATR", entry.invalid_rule)

    def test_low_volume_breakout_is_not_valid(self):
        """无量突破 → 不算有效入场结构。"""
        entry = evaluate(_breakout_input(volume_ratio=1.0))
        self.assertEqual(entry.state, EntryState.NO_STRUCTURE)
        self.assertFalse(entry.can_buy)
        self.assertTrue(any("无量突破" in b for b in entry.blockers))

    def test_breakout_with_falling_ma20_downgraded(self):
        """M20 明显下行时突破不算有效结构。"""
        entry = evaluate(_breakout_input(ma20=95.0, ma20_prev=96.0))
        self.assertEqual(entry.state, EntryState.NO_STRUCTURE)
        self.assertFalse(entry.can_buy)
        self.assertTrue(any("M20" in b and "下行" in b for b in entry.blockers))

    def test_breakout_demoted_when_benchmark_weak(self):
        """大盘在 M20 下方时，额外注明大盘偏弱。"""
        entry = evaluate(_breakout_input(benchmark_ok=False))
        self.assertEqual(entry.state, EntryState.BREAKOUT)
        self.assertFalse(entry.can_buy)
        self.assertTrue(any("大盘" in b for b in entry.blockers))

    def test_volume_state_label(self):
        self.assertEqual(evaluate(_breakout_input(volume_ratio=1.8)).volume_state, VolumeState.EXPAND)
        self.assertEqual(evaluate(_breakout_input(volume_ratio=0.7)).volume_state, VolumeState.SHRINK)
        self.assertEqual(evaluate(_breakout_input(volume_ratio=1.1)).volume_state, VolumeState.NEUTRAL)
        self.assertEqual(evaluate(_breakout_input(volume_ratio=None)).volume_state, VolumeState.UNKNOWN)


class TestPullback(unittest.TestCase):
    def test_valid_pullback(self):
        entry = evaluate(_pullback_input())
        self.assertEqual(entry.state, EntryState.PULLBACK_M20)
        self.assertTrue(entry.can_buy)
        self.assertIsNotNone(entry.stop_price)

    def test_pullback_stop_uses_atr(self):
        """止损 = 入场价 − 1.5×ATR，不是固定 2%。"""
        entry = evaluate(_pullback_input(atr=1.0))
        # 入场价 98.5，ATR 1.0 → stop = 98.5 − 1.5 = 97.0
        self.assertAlmostEqual(entry.stop_price, 97.0, places=4)
        self.assertIn("ATR", entry.invalid_rule)

    def test_pullback_stop_falls_back_without_atr(self):
        """ATR 缺失时退回 M20 下方 2%，但标记为退化路径。"""
        entry = evaluate(_pullback_input(atr=None))
        self.assertIsNotNone(entry.stop_price)
        self.assertAlmostEqual(entry.stop_price, 98.0 * 0.98, places=4)

    def test_no_stop_without_structure(self):
        """无结构时不给止损位——没入场就没离场计划。"""
        entry = evaluate(_pullback_input(price=100.0, volume_ratio=1.1))
        self.assertEqual(entry.state, EntryState.NO_STRUCTURE)
        self.assertFalse(entry.trend_weak)
        self.assertIsNone(entry.stop_price)
        self.assertEqual(entry.invalid_rule, "")

    def test_pullback_requires_ma20_up(self):
        """M20 下行时的回落不是「回踩」，不提示买入。"""
        entry = evaluate(_pullback_input(ma20=97.0, ma20_prev=98.0))
        self.assertEqual(entry.state, EntryState.NO_STRUCTURE)
        self.assertFalse(entry.can_buy)

    def test_pullback_requires_shrink_volume(self):
        """回踩放量说明是主动抛压，不提示买入。"""
        entry = evaluate(_pullback_input(volume_ratio=1.6))
        self.assertEqual(entry.state, EntryState.NO_STRUCTURE)
        self.assertFalse(entry.can_buy)
        self.assertTrue(any("缩量" in b for b in entry.blockers))

    def test_pullback_requires_stabilization(self):
        """未止跌（收绿且在 M20 下方）不提示买入。"""
        entry = evaluate(_pullback_input(price=97.0, today_change_pct=-1.0))
        self.assertEqual(entry.state, EntryState.NO_STRUCTURE)
        self.assertFalse(entry.can_buy)
        self.assertTrue(any("止跌" in b for b in entry.blockers))

    def test_ma20_slope_classification(self):
        self.assertEqual(evaluate(_pullback_input(ma20=98.0, ma20_prev=97.0)).ma20_slope, Ma20Slope.UP)
        self.assertEqual(evaluate(_pullback_input(ma20=97.0, ma20_prev=98.0)).ma20_slope, Ma20Slope.DOWN)
        self.assertEqual(evaluate(_pullback_input(ma20=98.0, ma20_prev=98.0)).ma20_slope, Ma20Slope.FLAT)


class TestNoStructure(unittest.TestCase):
    def test_moving_average_falling(self):
        entry = evaluate(_pullback_input(
            price=90.0, ma20=97.0, ma20_prev=98.0, volume_ratio=1.1,
        ))
        self.assertEqual(entry.state, EntryState.NO_STRUCTURE)
        self.assertTrue(any("M20 下行" in b for b in entry.blockers))

    def test_failed_breakout_falling_back(self):
        """突破前高后快速回落 → 明确写进失效原因。"""
        entry = evaluate(_breakout_input(
            price=99.0, closes_above_prior_high=2, volume_ratio=1.1,
        ))
        self.assertEqual(entry.state, EntryState.NO_STRUCTURE)
        self.assertTrue(any("突破前高后快速回落" in b for b in entry.blockers))

    def test_rebound_after_breakdown(self):
        """跌破 M20 后只是反抽 → 不算结构。"""
        entry = evaluate(_pullback_input(
            price=96.0, closes_below_ma20=3, volume_ratio=1.1, today_change_pct=1.0,
        ))
        self.assertEqual(entry.state, EntryState.NO_STRUCTURE)
        self.assertTrue(any("反抽" in b for b in entry.blockers))

    def test_has_trend_weak_flag(self):
        """有效跌破 M20 单日破位 2% → trend_weak。"""
        entry = evaluate(_pullback_input(price=95.0, today_change_pct=-3.0))
        self.assertTrue(entry.trend_weak)

    def test_trend_weak_by_consecutive_days(self):
        """连续 2 日收在 M20 下方也算有效跌破。"""
        entry = evaluate(_pullback_input(
            price=97.6, closes_below_ma20=2, today_change_pct=-0.5,
        ))
        self.assertTrue(entry.trend_weak)


class TestBias(unittest.TestCase):
    def test_bias_far_flagged(self):
        entry = evaluate(_breakout_input(price=120.0, ma20=100.0, ma20_prev=99.0))
        self.assertEqual(entry.bias_state, "乖离过大")
        self.assertGreaterEqual(entry.bias_pct, 8.0)


class TestFundamentalBucket(unittest.TestCase):
    def test_score_mapping(self):
        self.assertEqual(fundamental_bucket_from_score(80), FundamentalBucket.GOOD)
        self.assertEqual(fundamental_bucket_from_score(60), FundamentalBucket.NEUTRAL)
        self.assertEqual(fundamental_bucket_from_score(45), FundamentalBucket.WEAK)
        self.assertEqual(fundamental_bucket_from_score(30), FundamentalBucket.DETERIORATING)

    def test_alert_risk_deteriorates(self):
        self.assertEqual(
            fundamental_bucket_from_score(50, has_alert_risk=True),
            FundamentalBucket.DETERIORATING,
        )


class TestCombine(unittest.TestCase):
    def _entry(self, state, trend_weak=False):
        from entry_structure import EntryStructure
        return EntryStructure(
            state=state,
            state_label=state.value,
            trend_weak=trend_weak,
            can_buy=state == EntryState.PULLBACK_M20,
        )

    def test_good_plus_breakout(self):
        """基本面好 + 突破：不再是买点，只作观察。"""
        text = combine_fundamental_and_technical(
            FundamentalBucket.GOOD, self._entry(EntryState.BREAKOUT),
        )
        self.assertIn("只作观察", text)
        self.assertIn("不作为买点", text)

    def test_good_plus_pullback(self):
        text = combine_fundamental_and_technical(
            FundamentalBucket.GOOD, self._entry(EntryState.PULLBACK_M20),
        )
        self.assertIn("分批观察", text)

    def test_good_plus_no_structure(self):
        text = combine_fundamental_and_technical(
            FundamentalBucket.GOOD, self._entry(EntryState.NO_STRUCTURE),
        )
        self.assertIn("不是理想买点", text)

    def test_weak_plus_breakout_rejected(self):
        """基本面弱 + 突破：突破已非买入依据，叠加基本面弱直接否决。"""
        text = combine_fundamental_and_technical(
            FundamentalBucket.WEAK, self._entry(EntryState.BREAKOUT),
        )
        self.assertIn("不建议参与", text)
        self.assertIn("不要把技术反弹包装成中长期买入逻辑", text)

    def test_deteriorating_plus_trend_weak(self):
        text = combine_fundamental_and_technical(
            FundamentalBucket.DETERIORATING,
            self._entry(EntryState.NO_STRUCTURE, trend_weak=True),
        )
        self.assertIn("风险等级", text)
        self.assertIn("便宜", text)


class TestFormat(unittest.TestCase):
    def test_format_includes_state_and_stop(self):
        from entry_structure import format_entry_structure
        entry = evaluate(_breakout_input())
        text = format_entry_structure(entry)
        self.assertIn("放量突破前高", text)
        self.assertIn("失效定义", text)
        self.assertIn("止损参考位", text)


if __name__ == "__main__":
    unittest.main()


class BelowToleranceTests(unittest.TestCase):
    """「收在 M20 下方」用 0.5% 容差：贴线震荡不该算有效跌破。"""

    def _entry(self, tail_close):
        closes = [10.0] * 28 + [tail_close, tail_close]
        highs = [c * 1.01 for c in closes]
        lows = [c * 0.99 for c in closes]
        volumes = [1_000.0] * len(closes)
        from entry_structure import compute_entry_input
        return compute_entry_input(closes, highs, lows, volumes)

    def test_two_closes_slightly_below_ma20_do_not_count(self):
        entry = self._entry(9.97)
        self.assertEqual(entry.closes_below_ma20, 0)

    def test_two_closes_clearly_below_ma20_count(self):
        entry = self._entry(9.85)
        self.assertEqual(entry.closes_below_ma20, 2)


class RecentTrendInputTests(unittest.TestCase):
    def test_compute_entry_input_extracts_three_and_five_day_context(self):
        closes = [10.0] * 20 + [10.0, 10.2, 10.4, 10.6, 10.8, 11.0]
        highs = [value * 1.01 for value in closes]
        lows = [value * 0.99 for value in closes]
        volumes = [1_000.0] * 23 + [2_000.0] * 3

        entry = compute_entry_input(
            closes,
            highs,
            lows,
            volumes,
            price=11.2,
            volume_ratio=1.6,
            today_change_pct=1.8,
        )

        recent = getattr(entry, "recent_trend", None)
        self.assertIsNotNone(recent)
        self.assertAlmostEqual(recent.change_3d_pct, (11.2 / 10.4 - 1) * 100)
        self.assertAlmostEqual(recent.change_5d_pct, (11.2 / 10.0 - 1) * 100)
        self.assertEqual(recent.up_days_3d, 3)
        self.assertEqual(recent.up_days_5d, 5)
        self.assertGreaterEqual(recent.closes_above_ma20_5d, 3)
        self.assertGreater(recent.volume_3d_vs_20d, 1.8)
