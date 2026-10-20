#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""盘中买卖点信号测试。

信号触发策略（与Android版一致）：
- 跌破支撑：一轮触发，立即提醒（风险信号，不延迟）
- 其他信号：连续两轮确认（约30秒），避免假信号
"""

import unittest
from intraday_tactics import IntradayTactics, IntradaySignalType, limit_pct_for_board
from market_environment import MarketEnvironmentStatus, evaluate_market_environment


class TestMarketEnvironment(unittest.TestCase):
    def test_high_turnover_with_breadth_and_persistence_is_sustained(self):
        quality = evaluate_market_environment(
            predicted_turnover_yi=22000,
            market_change_pct=1.2,
            market_price=3200,
            market_high_price=3210,
            industry_breadth=0.72,
            concept_breadth=0.68,
            recent_full_day_turnovers_yi=[17000, 18000, 19000],
        )
        self.assertEqual(quality.status, MarketEnvironmentStatus.STRONG_SUSTAINED)

    def test_single_day_high_turnover_is_not_trend_confirmation(self):
        quality = evaluate_market_environment(
            predicted_turnover_yi=22000,
            market_change_pct=1.2,
            market_price=3200,
            market_high_price=3210,
            industry_breadth=0.72,
            concept_breadth=0.68,
            recent_full_day_turnovers_yi=[12000, 13000],
        )
        self.assertEqual(quality.status, MarketEnvironmentStatus.STRONG_SINGLE_DAY)
        self.assertFalse(quality.strong)

    def test_nonconsecutive_high_turnover_days_do_not_count_as_persistence(self):
        quality = evaluate_market_environment(
            predicted_turnover_yi=22000,
            market_change_pct=1.2,
            market_price=3200,
            market_high_price=3210,
            industry_breadth=0.72,
            concept_breadth=0.68,
            recent_full_day_turnovers_yi=[17000, 12000, 18000],
        )
        self.assertEqual(quality.persistence_days, 1)
        self.assertEqual(quality.status, MarketEnvironmentStatus.STRONG_SINGLE_DAY)

    def test_high_turnover_with_stagnant_index_and_weak_breadth_is_divergence(self):
        quality = evaluate_market_environment(
            predicted_turnover_yi=22000,
            market_change_pct=0.1,
            market_price=3200,
            market_high_price=3260,
            industry_breadth=0.38,
            concept_breadth=0.42,
            recent_full_day_turnovers_yi=[16000, 17000, 18000],
        )
        self.assertEqual(quality.status, MarketEnvironmentStatus.DIVERGENCE)

    def test_missing_breadth_never_confirms_strong_environment(self):
        quality = evaluate_market_environment(
            predicted_turnover_yi=22000,
            market_change_pct=1.0,
            market_price=3200,
            market_high_price=3210,
            recent_full_day_turnovers_yi=[16000, 17000],
        )
        self.assertEqual(quality.status, MarketEnvironmentStatus.UNKNOWN)


class TestLimitPctForBoard(unittest.TestCase):
    """测试涨跌停幅度判定。"""

    def test_main_board(self):
        """主板（60/00开头）→ 10%"""
        self.assertEqual(limit_pct_for_board("sh600519"), 10.0)
        self.assertEqual(limit_pct_for_board("sz000001"), 10.0)

    def test_chinext(self):
        """创业板（300开头）→ 20%"""
        self.assertEqual(limit_pct_for_board("sz300750"), 20.0)

    def test_star(self):
        """科创板（688开头）→ 20%"""
        self.assertEqual(limit_pct_for_board("sh688001"), 20.0)


class TestBreakSupport(unittest.TestCase):
    """测试跌破支撑信号（一轮触发，立即提醒）。"""

    def setUp(self):
        self.tactics = IntradayTactics()

    def test_break_support_triggers_immediately(self):
        """跌破支撑：一轮触发，立即提醒"""
        kwargs = dict(
            code="sh600519", name="贵州茅台", price=1750.0,
            change_pct=-2.0, volume_ratio=1.0, limit_pct=10.0,
            support=1780.0, resistance=1850.0, score=3, quantity=100,
        )
        # 第一轮就触发（风险信号，不延迟）
        signal = self.tactics.check_signal(**kwargs)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.signal_type, IntradaySignalType.BREAK_SUPPORT)
        self.assertIn("跌破支撑", signal.title)

    def test_no_break_support_without_holding(self):
        """未持仓时不触发跌破支撑"""
        signal = self.tactics.evaluate(
            code="sh600519", name="贵州茅台", price=1750.0,
            change_pct=-2.0, volume_ratio=1.0, limit_pct=10.0,
            support=1780.0, resistance=1850.0, score=3, quantity=0,
        )
        # 未持仓时跌破支撑不触发
        if signal:
            self.assertNotEqual(signal.signal_type, IntradaySignalType.BREAK_SUPPORT)


class TestBreakout(unittest.TestCase):
    """测试放量突破信号（需要两轮确认）。"""

    def setUp(self):
        self.tactics = IntradayTactics()
        self.kwargs = dict(
            code="sh600519", name="贵州茅台", price=1860.0,
            change_pct=3.0, volume_ratio=1.5, limit_pct=10.0,
            support=1750.0, resistance=1850.0, score=3, quantity=300,
        )

    def test_breakout_requires_two_rounds(self):
        """放量突破：需要两轮确认"""
        # 第一轮：不触发（待确认）
        signal1 = self.tactics.check_signal(**self.kwargs)
        self.assertIsNone(signal1)

        # 第二轮：触发
        signal2 = self.tactics.check_signal(**self.kwargs)
        self.assertIsNotNone(signal2)
        self.assertEqual(signal2.signal_type, IntradaySignalType.BREAKOUT)
        self.assertIn("放量突破", signal2.title)

    def test_no_breakout_without_volume(self):
        """无放量时不触发突破"""
        self.kwargs["volume_ratio"] = 0.8
        signal = self.tactics.evaluate(**self.kwargs)
        if signal:
            self.assertNotEqual(signal.signal_type, IntradaySignalType.BREAKOUT)


class TestNonHoldingNotificationPolicy(unittest.TestCase):
    """非持仓股只提醒买点，不提醒突破等非买点信号。"""

    def setUp(self):
        self.tactics = IntradayTactics()

    def test_non_holding_breakout_is_not_notified(self):
        kwargs = dict(
            code="sh600519", name="贵州茅台", price=1860.0,
            change_pct=3.0, volume_ratio=1.5, limit_pct=10.0,
            support=1750.0, resistance=1850.0, score=3, quantity=0,
        )

        self.assertIsNone(self.tactics.check_signal(**kwargs))
        self.assertIsNone(self.tactics.check_signal(**kwargs))

    def test_non_holding_pullback_buy_point_is_still_notified(self):
        kwargs = dict(
            code="sh600884", name="杉杉股份", price=15.05,
            change_pct=0.4, volume_ratio=0.7, limit_pct=10.0,
            support=15.0, resistance=16.5, score=3, quantity=0,
            ma20=15.0, ma20_prev=14.8, closes_below_ma20=0,
        )

        self.assertIsNone(self.tactics.check_signal(**kwargs))
        signal = self.tactics.check_signal(**kwargs)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.signal_type, IntradaySignalType.PULLBACK)


class TestTSell(unittest.TestCase):
    """测试冲高做T信号（需要两轮确认）。"""

    def setUp(self):
        self.tactics = IntradayTactics()
        self.kwargs = dict(
            code="sh600519", name="贵州茅台", price=1850.0,
            change_pct=3.5, volume_ratio=1.0, limit_pct=10.0,
            support=1750.0, resistance=1850.0, score=3, quantity=300,
        )

    def test_t_sell_requires_two_rounds(self):
        """冲高做T：需要两轮确认"""
        # 第一轮：不触发（待确认）
        signal1 = self.tactics.check_signal(**self.kwargs)
        self.assertIsNone(signal1)

        # 第二轮：触发
        signal2 = self.tactics.check_signal(**self.kwargs)
        self.assertIsNotNone(signal2)
        self.assertEqual(signal2.signal_type, IntradaySignalType.T_SELL)
        self.assertIn("冲高做T", signal2.title)
        self.assertIn("减出", signal2.body)

    def test_t_sell_does_not_show_zero_shares_for_small_position(self):
        """持仓不足一手的三分之一时，提示少量减仓而不是约0股。"""
        self.kwargs["quantity"] = 200
        tactics = IntradayTactics()
        signal = tactics.check_signal(**self.kwargs)
        signal = tactics.check_signal(**self.kwargs)

        self.assertIsNotNone(signal)
        self.assertNotIn("约0股", signal.body)
        self.assertIn("少量", signal.body)
        self.assertIn("1/3", signal.body)

    def test_no_t_sell_without_holding(self):
        """未持仓时不触发做T"""
        self.kwargs["quantity"] = 0
        signal = self.tactics.evaluate(**self.kwargs)
        if signal:
            self.assertNotEqual(signal.signal_type, IntradaySignalType.T_SELL)

    def test_strong_market_defers_ordinary_t_sell(self):
        """大盘、板块和个股同步强时，普通冲高做T改为持有观察。"""
        kwargs = dict(self.kwargs)
        kwargs.update(
            volume_ratio=1.5,
            predicted_market_turnover=22000.0,
            market_change_pct=1.2,
            sector_change_pct=2.0,
            daily_high_price=1855.0,
            market_index_price=3200.0,
            market_index_high_price=3210.0,
            industry_breadth=0.72,
            concept_breadth=0.68,
            recent_market_turnovers=[17000.0, 18000.0, 19000.0],
        )
        signal = self.tactics.evaluate(**kwargs)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.signal_type, IntradaySignalType.T_HOLD)
        self.assertIn("强势环境", signal.title)
        self.assertIn("暂缓做T", signal.body)

    def test_missing_market_context_keeps_t_sell(self):
        """环境数据缺失时保持原有做T行为，不误判为强势环境。"""
        signal = self.tactics.evaluate(**self.kwargs)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.signal_type, IntradaySignalType.T_SELL)

    def test_support_break_keeps_risk_priority_in_strong_market(self):
        """强势环境不能吞掉跌破支撑风险信号。"""
        kwargs = dict(self.kwargs)
        kwargs.update(
            volume_ratio=1.5,
            price=1700.0,
            change_pct=-2.0,
            support=1750.0,
            predicted_market_turnover=22000.0,
            market_change_pct=1.2,
            sector_change_pct=2.0,
            daily_high_price=1855.0,
        )
        signal = self.tactics.evaluate(**kwargs)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.signal_type, IntradaySignalType.BREAK_SUPPORT)

    def test_filter_is_signal_time_only_and_rearms_when_context_fades(self):
        strong = dict(self.kwargs)
        strong.update(
            volume_ratio=1.5,
            predicted_market_turnover=22000.0,
            market_change_pct=1.2,
            sector_change_pct=2.0,
            daily_high_price=1855.0,
            market_index_price=3200.0,
            market_index_high_price=3210.0,
            industry_breadth=0.72,
            concept_breadth=0.68,
            recent_market_turnovers=[17000.0, 18000.0, 19000.0],
        )
        deferred = self.tactics.evaluate(**strong)
        self.assertEqual(deferred.signal_type, IntradaySignalType.T_HOLD)

        normal = dict(self.kwargs)
        normal.update(
            predicted_market_turnover=19000.0,
            market_change_pct=-0.2,
            sector_change_pct=-0.1,
            daily_high_price=1855.0,
        )
        resumed = self.tactics.evaluate(**normal)
        self.assertEqual(resumed.signal_type, IntradaySignalType.T_SELL)

    def test_single_day_high_turnover_only_defers_with_caution(self):
        kwargs = dict(self.kwargs)
        kwargs.update(
            volume_ratio=1.5,
            predicted_market_turnover=22000.0,
            market_change_pct=1.2,
            sector_change_pct=2.0,
            daily_high_price=1855.0,
            market_index_price=3200.0,
            market_index_high_price=3210.0,
            industry_breadth=0.72,
            concept_breadth=0.68,
            recent_market_turnovers=[12000.0, 13000.0],
        )
        signal = self.tactics.evaluate(**kwargs)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.signal_type, IntradaySignalType.T_HOLD_CAUTION)
        self.assertIn("持续性", signal.body)

    def test_high_turnover_divergence_does_not_defer_t_sell(self):
        kwargs = dict(self.kwargs)
        kwargs.update(
            volume_ratio=1.5,
            predicted_market_turnover=22000.0,
            market_change_pct=0.1,
            sector_change_pct=0.2,
            daily_high_price=1855.0,
            market_index_price=3200.0,
            market_index_high_price=3260.0,
            industry_breadth=0.38,
            concept_breadth=0.42,
            recent_market_turnovers=[17000.0, 18000.0, 19000.0],
        )
        signal = self.tactics.evaluate(**kwargs)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.signal_type, IntradaySignalType.T_SELL)


class TestTLeaderHold(unittest.TestCase):
    """板块龙头封板/强承接时抑制冲高做T（见 sector_leaders.LeaderGuard）。"""

    def setUp(self):
        self.tactics = IntradayTactics()
        self.kwargs = dict(
            code="sh600522", name="中天科技", price=28.30,
            change_pct=6.3, volume_ratio=1.5, limit_pct=10.0,
            support=26.16, resistance=38.48, score=3, quantity=1000,
        )

    def test_leader_hold_defers_t_sell(self):
        """龙头封板时，冲高做T改为「龙头封板｜持有，暂缓做T」。"""
        kwargs = dict(self.kwargs)
        kwargs.update(leader_hold=True, leader_note="龙头封板")
        signal = self.tactics.evaluate(**kwargs)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.signal_type, IntradaySignalType.T_HOLD_LEADER)
        self.assertIn("龙头封板", signal.title)
        self.assertIn("暂缓做T", signal.body)
        self.assertIn("暂缓做T", signal.notification_body)

    def test_leader_hold_beats_strong_market_wording(self):
        """龙头封板与强势环境同时成立时，优先报龙头口径（更具体）。"""
        kwargs = dict(self.kwargs)
        kwargs.update(
            leader_hold=True,
            leader_note="龙头强承接",
            volume_ratio=1.5,
            predicted_market_turnover=22000.0,
            market_change_pct=1.2,
            sector_change_pct=2.0,
            daily_high_price=28.35,
            market_index_price=3200.0,
            market_index_high_price=3210.0,
            industry_breadth=0.72,
            concept_breadth=0.68,
            recent_market_turnovers=[17000.0, 18000.0, 19000.0],
        )
        signal = self.tactics.evaluate(**kwargs)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.signal_type, IntradaySignalType.T_HOLD_LEADER)
        self.assertIn("强承接", signal.title)

    def test_leader_hold_still_notifies_when_recent_weak(self):
        """近5日走弱本来会把做T整体拦掉；龙头口径要照出，让用户知道在暂缓。"""
        kwargs = dict(self.kwargs)
        kwargs.update(
            leader_hold=True,
            leader_note="龙头封板",
            recent_trend={"change_5d_pct": -4.0, "up_days_5d": 1},
        )
        signal = self.tactics.evaluate(**kwargs)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.signal_type, IntradaySignalType.T_HOLD_LEADER)

    def test_no_leader_hold_keeps_t_sell(self):
        """龙头未封板时恢复原规则，仍出冲高做T。"""
        signal = self.tactics.evaluate(**self.kwargs)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.signal_type, IntradaySignalType.T_SELL)

    def test_leader_hold_requires_two_rounds(self):
        """新信号也走两轮确认，避免盘口抖动误抑制。"""
        kwargs = dict(self.kwargs)
        kwargs.update(leader_hold=True, leader_note="龙头封板")
        self.assertIsNone(self.tactics.check_signal(**kwargs))
        signal = self.tactics.check_signal(**kwargs)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.signal_type, IntradaySignalType.T_HOLD_LEADER)


class TestTBuy(unittest.TestCase):
    """测试回踩接回信号（需要两轮确认）。"""

    def setUp(self):
        self.tactics = IntradayTactics()
        self.kwargs = dict(
            code="sh600519", name="贵州茅台", price=1785.0,
            change_pct=-3.5, volume_ratio=0.8, limit_pct=10.0,
            support=1780.0, resistance=1850.0, score=3, quantity=300,
        )

    def test_t_buy_requires_two_rounds(self):
        """回踩接回：需要两轮确认"""
        # 第一轮：不触发（待确认）
        signal1 = self.tactics.check_signal(**self.kwargs)
        self.assertIsNone(signal1)

        # 第二轮：触发
        signal2 = self.tactics.check_signal(**self.kwargs)
        self.assertIsNotNone(signal2)
        self.assertEqual(signal2.signal_type, IntradaySignalType.T_BUY)
        self.assertIn("回踩接回", signal2.title)


class TestRecentTrendContext(unittest.TestCase):
    def setUp(self):
        self.tactics = IntradayTactics()

    def _evaluate(self, **kwargs):
        try:
            return self.tactics.evaluate(**kwargs)
        except TypeError as error:
            self.fail(f"recent_trend evaluation API is missing: {error}")

    def test_t_buy_is_suppressed_when_recent_trend_is_weak(self):
        signal = self._evaluate(
            code="sh600519", name="贵州茅台", price=1785.0,
            change_pct=-3.5, volume_ratio=0.8, limit_pct=10.0,
            support=1780.0, resistance=1850.0, score=3, quantity=300,
            recent_trend={
                "change_3d_pct": -3.0,
                "change_5d_pct": -5.0,
                "up_days_3d": 0,
                "up_days_5d": 1,
                "closes_above_ma20_5d": 1,
            },
        )
        self.assertIsNone(signal)

    def test_t_buy_requires_pullback_volume_to_shrink(self):
        signal = self._evaluate(
            code="sh600519", name="贵州茅台", price=1785.0,
            change_pct=-3.5, volume_ratio=1.2, limit_pct=10.0,
            support=1780.0, resistance=1850.0, score=3, quantity=300,
            recent_trend={
                "change_3d_pct": 2.0,
                "change_5d_pct": 4.0,
                "up_days_3d": 2,
                "up_days_5d": 3,
                "closes_above_ma20_5d": 4,
            },
        )
        self.assertIsNone(signal)

    def test_missing_recent_context_keeps_legacy_t_buy_behavior(self):
        signal = self._evaluate(
            code="sh600519", name="贵州茅台", price=1785.0,
            change_pct=-3.5, volume_ratio=1.2, limit_pct=10.0,
            support=1780.0, resistance=1850.0, score=3, quantity=300,
            recent_trend={},
        )
        self.assertIsNotNone(signal)
        self.assertEqual(signal.signal_type, IntradaySignalType.T_BUY)

    def test_recent_breakout_defers_t_sell_without_large_bias(self):
        signal = self._evaluate(
            code="sh600519", name="贵州茅台", price=1848.0,
            change_pct=3.5, volume_ratio=1.5, limit_pct=10.0,
            support=1750.0, resistance=1850.0, score=3, quantity=300,
            recent_trend={
                "change_3d_pct": 3.0,
                "change_5d_pct": 5.0,
                "up_days_3d": 2,
                "up_days_5d": 3,
                "closes_above_ma20_5d": 4,
                "closes_above_prior_high_5d": 1,
            },
        )
        self.assertIsNone(signal)

    def test_extended_recent_run_keeps_t_sell_and_reports_context(self):
        signal = self._evaluate(
            code="sh600519", name="贵州茅台", price=1848.0,
            change_pct=3.5, volume_ratio=1.5, limit_pct=10.0,
            support=1750.0, resistance=1850.0, score=3, quantity=300,
            recent_trend={
                "change_3d_pct": 6.0,
                "change_5d_pct": 12.0,
                "up_days_3d": 3,
                "up_days_5d": 4,
                "closes_above_ma20_5d": 5,
                "volume_3d_vs_20d": 1.4,
            },
        )
        self.assertIsNotNone(signal)
        self.assertEqual(signal.signal_type, IntradaySignalType.T_SELL)
        self.assertIn("近5日", signal.body)


class TestPullback(unittest.TestCase):
    """测试回踩支撑信号（需要两轮确认）。"""

    def setUp(self):
        self.tactics = IntradayTactics()
        self.kwargs = dict(
            code="sh600519", name="贵州茅台", price=1790.0,
            change_pct=-1.0, volume_ratio=0.8, limit_pct=10.0,
            support=1780.0, resistance=1850.0, score=3, quantity=0,
        )

    def test_pullback_requires_two_rounds(self):
        """回踩支撑：需要两轮确认"""
        # 第一轮：不触发（待确认）
        signal1 = self.tactics.check_signal(**self.kwargs)
        self.assertIsNone(signal1)

        # 第二轮：触发
        signal2 = self.tactics.check_signal(**self.kwargs)
        self.assertIsNotNone(signal2)
        self.assertEqual(signal2.signal_type, IntradaySignalType.PULLBACK)
        # 未给 M20 上下文时退回旧口径，标题用新的「回踩上行M20」措辞
        self.assertIn("回踩", signal2.title)


class TestAntiSpam(unittest.TestCase):
    """测试防重复机制。"""

    def setUp(self):
        self.tactics = IntradayTactics()

    def test_break_support_not_triggered_twice(self):
        """跌破支撑：同一信号不重复触发"""
        kwargs = dict(
            code="sh600519", name="贵州茅台", price=1750.0,
            change_pct=-2.0, volume_ratio=1.0, limit_pct=10.0,
            support=1780.0, resistance=1850.0, score=3, quantity=100,
        )
        # 第一次触发
        signal1 = self.tactics.check_signal(**kwargs)
        self.assertIsNotNone(signal1)

        # 第二次不触发（已触发状态）
        signal2 = self.tactics.check_signal(**kwargs)
        self.assertIsNone(signal2)

    def test_breakout_not_triggered_twice(self):
        """放量突破：两轮确认后不再重复触发"""
        kwargs = dict(
            code="sh600519", name="贵州茅台", price=1860.0,
            change_pct=3.0, volume_ratio=1.5, limit_pct=10.0,
            support=1750.0, resistance=1850.0, score=3, quantity=300,
        )
        # 两轮确认
        self.tactics.check_signal(**kwargs)  # 第一轮：待确认
        signal = self.tactics.check_signal(**kwargs)  # 第二轮：触发
        self.assertIsNotNone(signal)

        # 第三次不触发（已触发状态）
        signal3 = self.tactics.check_signal(**kwargs)
        self.assertIsNone(signal3)

    def test_signal_resets_when_condition_clears(self):
        """条件消失后信号状态重置"""
        kwargs_break = dict(
            code="sh600519", name="贵州茅台", price=1750.0,
            change_pct=-2.0, volume_ratio=1.0, limit_pct=10.0,
            support=1780.0, resistance=1850.0, score=3, quantity=100,
        )
        # 触发跌破支撑
        signal1 = self.tactics.check_signal(**kwargs_break)
        self.assertIsNotNone(signal1)

        # 价格回到支撑上方（条件消失）
        kwargs_ok = dict(
            code="sh600519", name="贵州茅台", price=1800.0,
            change_pct=0.5, volume_ratio=1.0, limit_pct=10.0,
            support=1780.0, resistance=1850.0, score=3, quantity=100,
        )
        signal2 = self.tactics.check_signal(**kwargs_ok)
        self.assertIsNone(signal2)

        # 再次跌破支撑（应该能再次触发）
        signal3 = self.tactics.check_signal(**kwargs_break)
        self.assertIsNotNone(signal3)

    def test_pending_cleared_when_condition_clears(self):
        """待确认状态在条件消失时清除"""
        kwargs_breakout = dict(
            code="sh600519", name="贵州茅台", price=1860.0,
            change_pct=3.0, volume_ratio=1.5, limit_pct=10.0,
            support=1750.0, resistance=1850.0, score=3, quantity=300,
        )
        # 第一轮：进入待确认
        signal1 = self.tactics.check_signal(**kwargs_breakout)
        self.assertIsNone(signal1)  # 待确认，未触发

        # 条件消失
        kwargs_ok = dict(
            code="sh600519", name="贵州茅台", price=1800.0,
            change_pct=0.5, volume_ratio=1.0, limit_pct=10.0,
            support=1750.0, resistance=1850.0, score=3, quantity=300,
        )
        self.tactics.check_signal(**kwargs_ok)

        # 再次触发突破：需要重新两轮确认（pending被清除）
        signal3 = self.tactics.check_signal(**kwargs_breakout)
        self.assertIsNone(signal3)  # pending被清除，重新开始第一轮

        signal4 = self.tactics.check_signal(**kwargs_breakout)
        self.assertIsNotNone(signal4)  # 第二轮确认，触发


class TestTrendWeak(unittest.TestCase):
    """有效跌破 M20 → 趋势转弱观察（两轮确认，不等同于立即清仓）。"""

    def setUp(self):
        self.tactics = IntradayTactics()

    def test_trend_weak_requires_two_rounds(self):
        """趋势转弱需要连续两轮确认，不立即提醒"""
        kwargs = dict(
            code="sh600519", name="贵州茅台", price=1750.0,
            change_pct=-1.0, volume_ratio=0.9, limit_pct=10.0,
            support=1700.0, resistance=1850.0, score=3, quantity=100,
            ma20=1800.0, ma20_prev=1810.0, closes_below_ma20=3,
        )
        signal1 = self.tactics.check_signal(**kwargs)
        self.assertIsNone(signal1)  # 第一轮待确认
        signal2 = self.tactics.check_signal(**kwargs)
        self.assertIsNotNone(signal2)
        self.assertEqual(signal2.signal_type, IntradaySignalType.TREND_WEAK)
        self.assertIn("趋势转弱观察", signal2.title)

    def test_trend_weak_body_not_liquidation(self):
        """趋势转弱提醒不得写成『立即清仓』"""
        kwargs = dict(
            code="sh600519", name="贵州茅台", price=1750.0,
            change_pct=-1.0, volume_ratio=0.9, limit_pct=10.0,
            support=1700.0, resistance=1850.0, score=3, quantity=100,
            ma20=1800.0, ma20_prev=1810.0, closes_below_ma20=3,
        )
        self.tactics.check_signal(**kwargs)
        signal = self.tactics.check_signal(**kwargs)
        self.assertIsNotNone(signal)
        self.assertNotIn("清仓", signal.body)
        self.assertIn("不加仓", signal.body)

    def test_no_trend_weak_without_holding(self):
        """未持仓不触发趋势转弱"""
        signal = self.tactics.evaluate(
            code="sh600519", name="贵州茅台", price=1750.0,
            change_pct=-1.0, volume_ratio=0.9, limit_pct=10.0,
            support=1700.0, resistance=1850.0, score=3, quantity=0,
            ma20=1800.0, ma20_prev=1810.0, closes_below_ma20=3,
        )
        if signal:
            self.assertNotEqual(signal.signal_type, IntradaySignalType.TREND_WEAK)

    def test_no_trend_weak_when_ma20_holds(self):
        """价格仍在 M20 上方时不触发趋势转弱"""
        signal = self.tactics.evaluate(
            code="sh600519", name="贵州茅台", price=1820.0,
            change_pct=-1.0, volume_ratio=0.9, limit_pct=10.0,
            support=1700.0, resistance=1850.0, score=3, quantity=100,
            ma20=1800.0, ma20_prev=1810.0, closes_below_ma20=0,
        )
        if signal:
            self.assertNotEqual(signal.signal_type, IntradaySignalType.TREND_WEAK)

    def test_high_price_non_deep_trend_weak_is_exit_risk(self):
        kwargs = dict(
            code="sh600519", name="贵州茅台", price=1785.0,
            change_pct=-2.0, volume_ratio=1.0, limit_pct=10.0,
            support=1750.0, resistance=1850.0, score=3, quantity=100,
            cost=1800.0, price_location="HIGH", trend_weak=True,
            ma20=1800.0, ma20_prev=1810.0, closes_below_ma20=3,
        )
        signal = self.tactics.check_signal(**kwargs)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.signal_type, IntradaySignalType.EXIT_RISK)
        self.assertIn("清仓离场", signal.body)
        self.assertIn("执行可分批", signal.body)


class TestPullbackWithMa20(unittest.TestCase):
    """回踩上行 M20：M20 下行或未止跌时不提示买入。"""

    def setUp(self):
        self.tactics = IntradayTactics()

    def _kwargs(self, **overrides):
        kwargs = dict(
            code="sh600884", name="杉杉股份", price=15.05,
            change_pct=0.4, volume_ratio=0.7, limit_pct=10.0,
            support=15.0, resistance=16.5, score=3, quantity=0,
            ma20=15.0, ma20_prev=14.8, closes_below_ma20=0,
        )
        kwargs.update(overrides)
        return kwargs

    def test_pullback_with_rising_ma20(self):
        signal = self.tactics.evaluate(**self._kwargs())
        self.assertIsNotNone(signal)
        self.assertEqual(signal.signal_type, IntradaySignalType.PULLBACK)

    def test_no_pullback_when_ma20_falling(self):
        """M20 下行时不提示买入"""
        signal = self.tactics.evaluate(**self._kwargs(ma20=14.8, ma20_prev=15.0))
        self.assertIsNone(signal)

    def test_no_pullback_when_not_stabilized(self):
        """收绿且在 M20 下方：未止跌，不提示买入"""
        signal = self.tactics.evaluate(**self._kwargs(
            price=14.85, change_pct=-1.2,
        ))
        self.assertIsNone(signal)

    def test_no_pullback_when_ma20_validly_broken(self):
        """已有效跌破 M20：不提示买入"""
        signal = self.tactics.evaluate(**self._kwargs(closes_below_ma20=2))
        self.assertIsNone(signal)


class TestBreakoutWithMa20(unittest.TestCase):
    """放量突破：一律仅作观察，非买入依据（回测显示负超额）。"""

    def test_breakout_is_observation_only(self):
        """即使 M20 上行，也不提示买入仓位。"""
        tactics = IntradayTactics()
        signal = tactics.evaluate(
            code="sz300750", name="宁德时代", price=210.0,
            change_pct=3.5, volume_ratio=1.8, limit_pct=20.0,
            support=190.0, resistance=205.0, score=4, quantity=0,
            ma20=195.0, ma20_prev=192.0,
        )
        self.assertIsNotNone(signal)
        self.assertEqual(signal.signal_type, IntradaySignalType.BREAKOUT)
        self.assertIn("仅作观察", signal.body)
        self.assertIn("非买入依据", signal.body)
        self.assertNotIn("初始仓位", signal.body)

    def test_breakout_body_flags_falling_ma20(self):
        """M20 下行时额外注明可靠性不足。"""
        tactics = IntradayTactics()
        signal = tactics.evaluate(
            code="sz300750", name="宁德时代", price=210.0,
            change_pct=3.5, volume_ratio=1.8, limit_pct=20.0,
            support=190.0, resistance=205.0, score=4, quantity=0,
            ma20=195.0, ma20_prev=198.0,
        )
        self.assertIsNotNone(signal)
        self.assertIn("仅作观察", signal.body)
        self.assertIn("M20 仍在下行", signal.body)

    def test_breakout_holding_says_no_chasing(self):
        """持仓时只提示持有观察、不追高。"""
        tactics = IntradayTactics()
        signal = tactics.evaluate(
            code="sz300750", name="宁德时代", price=210.0,
            change_pct=3.5, volume_ratio=1.8, limit_pct=20.0,
            support=190.0, resistance=205.0, score=4, quantity=300,
            ma20=195.0, ma20_prev=192.0,
        )
        self.assertIsNotNone(signal)
        self.assertIn("不追高", signal.body)
        self.assertNotIn("初始仓位", signal.body)


class TestDeepLossFramework(unittest.TestCase):
    """深套破位决策框架：浅亏反抽确认、深套两套剧本、站回/离场窗口。"""

    def _signal(self, **kw):
        tactics = IntradayTactics()
        params = dict(
            code="sh600522", name="中天科技", price=30.0, change_pct=-1.2,
            volume_ratio=1.0, limit_pct=10.0, support=30.5, resistance=32.5,
            score=-1, quantity=500,
        )
        params.update(kw)
        return tactics.evaluate(**params)

    def test_shallow_break_waits_for_failed_reclaim(self):
        signal = self._signal(cost=32.0, quantity=2000)
        self.assertEqual(signal.signal_type, IntradaySignalType.BREAK_SUPPORT)
        self.assertIn("反抽站不回", signal.body)
        self.assertIn("减约600股", signal.body)

    def test_rebreak_after_reclaim_says_cut_now(self):
        signal = self._signal(cost=21.0, quantity=2000, price=18.8, support=19.0,
                              reclaimed_support=True)
        self.assertIn("再度破位", signal.body)
        self.assertIn("减约600股", signal.body)

    def test_deep_break_gives_two_scripts_not_cut(self):
        signal = self._signal(cost=45.0, total_capital=200_000.0)
        self.assertEqual(signal.signal_type, IntradaySignalType.BREAK_SUPPORT)
        self.assertNotIn("减约", signal.body)
        self.assertIn("割肉摊成本", signal.body)
        self.assertIn("①逻辑/基本面已破坏", signal.body)
        self.assertIn("②逻辑还在", signal.body)
        self.assertIn("补仓降成本", signal.body)
        self.assertIn("再创新低", signal.body)
        # 补仓股数：初始仓位 5% 的一半 = 5000 元 → 约 166 股 → 按手 100
        self.assertIn("约100股", signal.body)

    def test_deep_loss_threshold_scales_with_volatility(self):
        # ATR 3.5% 的题材股阈值 -28%：-22% 浮亏还走减仓口径。
        shallow = self._signal(cost=38.5, quantity=2000, atr_pct=3.5)
        self.assertIn("反抽站不回", shallow.body)
        deep = self._signal(cost=38.5, quantity=2000, atr_pct=1.0)
        self.assertIn("割肉摊成本", deep.body)

    def test_reclaim_is_the_add_window_for_deep_loss(self):
        signal = self._signal(
            cost=45.0, total_capital=200_000.0, price=30.2, support=30.0,
            broken_support=True,
        )
        self.assertEqual(signal.signal_type, IntradaySignalType.RECLAIM_SUPPORT)
        self.assertIn("补仓", signal.body)
        self.assertIn("约100股", signal.body)

    def test_reclaim_with_red_fundamentals_prefers_exit(self):
        class _Health:
            level = "ALERT"
            reasons = ["评分 40（较差）"]

        signal = self._signal(
            cost=45.0, total_capital=200_000.0, price=30.2, support=30.0,
            broken_support=True, fundamental_health=_Health(),
        )
        self.assertEqual(signal.signal_type, IntradaySignalType.RECLAIM_SUPPORT)
        self.assertIn("不补仓", signal.body)
        self.assertIn("分批离场", signal.body)

    def test_exit_window_fires_for_deep_red_at_resistance(self):
        class _Health:
            level = "ALERT"
            reasons = ["评分 40（较差）"]

        signal = self._signal(
            cost=45.0, price=32.0, support=28.0, resistance=32.2,
            change_pct=1.0, quantity=2000, fundamental_health=_Health(),
        )
        self.assertEqual(signal.signal_type, IntradaySignalType.EXIT_WINDOW)
        self.assertIn("分批离场", signal.body)

    def test_exit_window_needs_red_fundamentals(self):
        signal = self._signal(
            cost=45.0, price=32.0, support=28.0, resistance=32.2,
            change_pct=1.0, quantity=2000,
        )
        self.assertIsNone(signal)


class TestTrendWeakCopy(unittest.TestCase):
    def _signal(self, **overrides):
        params = dict(
            code="sh600522", name="中天科技", price=30.0,
            change_pct=-1.5, volume_ratio=0.9, limit_pct=10.0,
            support=28.0, resistance=32.0, score=1, quantity=500,
            ma20=32.0, ma20_prev=33.0, closes_below_ma20=2,
        )
        params.update(overrides)
        return IntradayTactics().evaluate(**params)

    def test_falling_ma20_names_direction_and_action(self):
        signal = self._signal()

        self.assertEqual(signal.signal_type, IntradaySignalType.TREND_WEAK)
        self.assertEqual(signal.notification_body, "M20 32.00下行｜暂不加仓")
        self.assertNotIn("站稳前", signal.notification_body)

    def test_reclaimed_support_uses_next_checkpoint(self):
        signal = self._signal(
            broken_support=True,
            reclaimed_support=True,
        )

        self.assertEqual(signal.signal_type, IntradaySignalType.TREND_WEAK)
        self.assertEqual(
            signal.notification_body,
            "支撑28.00已收｜暂不加仓，再看M20",
        )


class TestSharedBudget(unittest.TestCase):
    """风险提醒（破位）不占共享预算，机会类共享每日上限。"""

    def test_break_is_not_starved_by_opportunity_signals(self):
        from intraday_tactics import SHARED_PUSH_MAX_PER_DAY

        base = dict(
            code="sh600522", name="中天科技", volume_ratio=1.5,
            limit_pct=10.0, support=19.0, resistance=21.0, score=0,
            quantity=2000, cost=21.0, confirmation_rounds=1,
        )
        # 预算已满：机会类（冲高做T）被挡住。
        blocked_tactics = IntradayTactics()
        blocked_tactics._ensure_state(base["code"])["shared_count"] = SHARED_PUSH_MAX_PER_DAY
        self.assertIsNone(blocked_tactics.check_signal(price=20.5, change_pct=3.2, **base))

        # 同等预算下，风险提醒（破位）仍然必须发。
        risk_tactics = IntradayTactics()
        risk_tactics._ensure_state(base["code"])["shared_count"] = SHARED_PUSH_MAX_PER_DAY
        signal = risk_tactics.check_signal(price=18.0, change_pct=-3.5, **base)
        self.assertIsNotNone(signal)
        self.assertEqual(signal.signal_type, IntradaySignalType.BREAK_SUPPORT)


if __name__ == "__main__":
    unittest.main()
