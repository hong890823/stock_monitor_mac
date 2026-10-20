#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""513 低位形态选股测试，用例与 Android 版 FiveOneThreeScreenerTest.kt 一一对应。"""

import unittest

from five_one_three import (
    FiveOneThreeStage,
    evaluate,
    inspect,
    order_five_one_three_hits,
)


def _make_bar(close, open_, volume, high=None, low=None):
    _date_counter[0] += 1
    index = _date_counter[0]
    return {
        "date": f"2026-{(index - 1) // 28 + 8:02d}-{(index - 1) % 28 + 1:02d}",
        "open": open_,
        "high": high if high is not None else close * 1.01,
        "low": low if low is not None else open_ * 0.99,
        "close": close,
        "volume": volume,
    }


_date_counter = [0]


def _declining_base_bars():
    bars = []
    for index in range(60):
        # 与 Android 版测试一致：跌到第 55 根后走平，保证形态窗口的 5 日线检查可过
        close = 20.0 - min(index, 55) * 0.16
        bars.append(_make_bar(close=close, open_=close * 1.01, volume=1_000.0))
    return bars


def _rising_base_bars():
    bars = []
    for index in range(60):
        close = 10.0 + index * 0.12
        bars.append(_make_bar(close=close, open_=close * 0.99, volume=1_000.0))
    return bars


def _bullish_pattern(start_close):
    bars = []
    for index in range(1, 6):
        close = start_close * (1.02 ** index)
        open_ = close / 1.02
        bars.append(_make_bar(close=close, open_=open_, volume=1_000.0))
    return bars


def _bars_with_pattern(
    include_double_volume=False,
    confirmation_volumes=(),
    break_signal_open_on_confirmation=False,
    wide_range_on_confirmation=False,
):
    bars = _declining_base_bars()
    bars += _bullish_pattern(start_close=bars[-1]["close"])
    if include_double_volume:
        signal_open = bars[-1]["close"] * 1.01
        signal_close = signal_open * 1.02
        bars.append(
            _make_bar(
                close=signal_close,
                open_=signal_open,
                high=signal_close * 1.01,
                low=signal_open * 0.99,
                volume=2_200.0,
            )
        )
        for index, volume in enumerate(confirmation_volumes):
            if break_signal_open_on_confirmation and index == 0:
                open_ = signal_open * 0.98
            else:
                open_ = signal_close * 0.995
            close = open_ * 1.002
            if wide_range_on_confirmation and index == 1:
                high, low = close * 1.04, close * 0.94
            else:
                high, low = close * 1.005, open_ * 0.998
            bars.append(_make_bar(close=close, open_=open_, volume=volume, high=high, low=low))
    return bars


class FiveOneThreeEvaluateTests(unittest.TestCase):
    def setUp(self):
        _date_counter[0] = 0

    def test_five_bullish_days_return_observing_stage(self):
        result = evaluate("sh600000", "测试股份", _bars_with_pattern())

        self.assertIsNotNone(result)
        self.assertEqual(FiveOneThreeStage.OBSERVING, result["stage"])
        self.assertEqual("5连阳 5/5｜倍量阳 0/1｜缩量确认 0/3", result["detail"])

    def test_double_volume_bullish_day_returns_waiting_stage(self):
        result = evaluate(
            "sh600000",
            "测试股份",
            _bars_with_pattern(include_double_volume=True),
        )

        self.assertEqual(FiveOneThreeStage.WAITING_CONFIRMATION, result["stage"])
        self.assertEqual("5连阳 5/5｜倍量阳 1/1｜缩量确认 0/3", result["detail"])

    def test_three_shrinking_days_return_confirmed_stage(self):
        result = evaluate(
            "sh600000",
            "测试股份",
            _bars_with_pattern(
                include_double_volume=True,
                confirmation_volumes=[1_500.0, 1_200.0, 900.0],
            ),
        )

        self.assertEqual(FiveOneThreeStage.CONFIRMED, result["stage"])
        self.assertEqual("5连阳 5/5｜倍量阳 1/1｜缩量确认 3/3", result["detail"])

    def test_stale_confirmed_pattern_expires(self):
        """513 是短线形态：确认完成超过 3 个交易日不再列出。"""
        confirmed = _bars_with_pattern(
            include_double_volume=True,
            confirmation_volumes=(1_500.0, 1_200.0, 900.0),
        )
        result = evaluate("sh600000", "测试股份", confirmed)
        self.assertEqual(result["stage"], FiveOneThreeStage.CONFIRMED)

        def _quiet(count):
            out = []
            for index in range(count):
                close = 11.5 - index * 0.02
                out.append(_make_bar(close=close, open_=close * 1.005, volume=800.0))
            return out

        within = evaluate("sh600000", "测试股份", confirmed + _quiet(3))
        self.assertEqual(within["stage"], FiveOneThreeStage.CONFIRMED)
        self.assertIsNone(evaluate("sh600000", "测试股份", confirmed + _quiet(4)))

    def test_high_position_candidate_is_excluded(self):
        bars = _rising_base_bars() + _bullish_pattern(start_close=15.0)

        self.assertIsNone(evaluate("sh600000", "测试股份", bars))

    def test_breaking_signal_open_invalidates_confirmation(self):
        result = evaluate(
            "sh600000",
            "测试股份",
            _bars_with_pattern(
                include_double_volume=True,
                confirmation_volumes=[1_500.0, 1_200.0, 900.0],
                break_signal_open_on_confirmation=True,
            ),
        )

        self.assertIsNone(result)

    def test_expanding_volume_or_range_does_not_confirm(self):
        result = evaluate(
            "sh600000",
            "测试股份",
            _bars_with_pattern(
                include_double_volume=True,
                confirmation_volumes=[1_500.0, 1_800.0, 900.0],
                wide_range_on_confirmation=True,
            ),
        )

        self.assertIsNone(result)

    def test_st_stock_is_excluded(self):
        self.assertIsNone(evaluate("sh600000", "ST测试", _bars_with_pattern()))

    def test_close_below_ma5_without_tolerance_is_rejected(self):
        # 收盘站上5日线改为无容差（原 0.97 倍容差取消）：
        # 持续阴跌的底座 + 小阳线，首根收盘低于自身 MA5 → 不算 5 连阳
        bars = []
        for index in range(60):
            close = 20.0 - index * 0.16
            bars.append(_make_bar(close=close, open_=close * 1.01, volume=1_000.0))
        start = bars[-1]["close"]
        for index in range(1, 6):
            close = start * (1.02 ** index)
            bars.append(_make_bar(close=close, open_=close / 1.02, volume=1_000.0))

        self.assertIsNone(evaluate("sh600000", "测试股份", bars))

    def test_inspection_reports_funnel_flags(self):
        bars = _bars_with_pattern()
        inspection = inspect("sh600000", "测试股份", bars)

        self.assertTrue(inspection["history_available"])
        self.assertTrue(inspection["historical_five_bullish"])
        self.assertTrue(inspection["recent_five_bullish"])
        self.assertIsNotNone(inspection["hit"])

    def test_inspection_flags_missing_history(self):
        bars = _bars_with_pattern()[:40]
        inspection = inspect("sh600000", "测试股份", bars)

        self.assertFalse(inspection["history_available"])
        self.assertFalse(inspection["historical_five_bullish"])
        self.assertFalse(inspection["recent_five_bullish"])
        self.assertIsNone(inspection["hit"])

    def test_today_bar_is_not_counted_as_confirmation(self):
        # 盘中未完成的当日 K 线不参与判定：第 3 根缩量确认若还是「今天」，
        # 只能算 2/3 等待确认，不能判形态已确认。
        bars = _bars_with_pattern(
            include_double_volume=True,
            confirmation_volumes=[1_500.0, 1_200.0, 900.0],
        )
        result = evaluate(
            "sh600000",
            "测试股份",
            bars,
            today=bars[-1]["date"],
        )

        self.assertEqual(FiveOneThreeStage.WAITING_CONFIRMATION, result["stage"])
        self.assertEqual("5连阳 5/5｜倍量阳 1/1｜缩量确认 2/3", result["detail"])

    def test_current_price_overrides_close(self):
        bars = _bars_with_pattern()
        result = evaluate("sh600000", "测试股份", bars, current_price=88.8)

        self.assertEqual(88.8, result["price"])


def _confirmed_pattern_fixture(prefix_days, pattern_volume):
    """完整走完 5连阳→倍量阳→3缩量 的形态；prefix_days 为窗口前的历史K线数。"""
    bars = []
    counter = [0]

    def _d():
        counter[0] += 1
        i = counter[0]
        return f"2026-{(i - 1) // 28 + 5:02d}-{(i - 1) % 28 + 1:02d}"

    for index in range(prefix_days):
        close = 20.0 + index * 0.05
        bars.append(_make_bar_with_date(close=close, open_=close * 1.01, volume=1_000.0, date=_d()))
    start_close = bars[-1]["close"] if bars else 10.0
    for index in range(1, 6):
        close = start_close * (1.02 ** index)
        bars.append(_make_bar_with_date(close=close, open_=close / 1.02, volume=pattern_volume, date=_d()))
    signal_open = bars[-1]["close"] * 1.01
    signal_close = signal_open * 1.02
    bars.append(_make_bar_with_date(close=signal_close, open_=signal_open, volume=pattern_volume * 2.2, date=_d()))
    for volume in (pattern_volume * 1.6, pattern_volume * 1.2, pattern_volume * 0.8):
        open_ = signal_close * 0.995
        close = open_ * 1.002
        bars.append(_make_bar_with_date(close=close, open_=open_, volume=volume, date=_d()))
    while len(bars) < 60:
        close = 9.0
        bars.append(_make_bar_with_date(close=close, open_=close * 1.01, volume=1_000.0, date=_d()))
    return bars


def _make_bar_with_date(close, open_, volume, date):
    return {
        "date": date,
        "open": open_,
        "high": close * 1.01,
        "low": open_ * 0.99,
        "close": close,
        "volume": volume,
    }


class FiveOneThreeVolumeBaselineTests(unittest.TestCase):
    """量能基线边界：窗口前没有 20 日基线时不得静默跳过量能约束。

    Android 版空列表 average()=NaN、比较全为 False，会把任意量能的 5 连阳放行；
    这里收紧为直接拒绝，用例锁住该行为。
    """

    def test_pattern_without_volume_baseline_is_rejected(self):
        # 无基线 + 50 倍量能：修复前会误判「形态已确认」，现在必须拒绝
        bars = _confirmed_pattern_fixture(prefix_days=0, pattern_volume=50_000.0)

        self.assertIsNone(evaluate("sh600000", "测试股份", bars))

    def test_absurd_volume_rejected_when_baseline_exists(self):
        # 有基线时 1.8 倍上限正常工作，同样拒绝
        bars = _confirmed_pattern_fixture(prefix_days=20, pattern_volume=50_000.0)

        self.assertIsNone(evaluate("sh600000", "测试股份", bars))

    def test_normal_volume_pattern_with_baseline_still_confirms(self):
        # 正对照：有基线 + 温和量能 → 形态已确认，确保修复没有误伤正常命中。
        # 形态放在最近（尾部不追加长历史）：过期规则会剔除 3 个交易日之前的旧形态。
        bars = _confirmed_pattern_fixture(prefix_days=50, pattern_volume=1_000.0)
        result = evaluate("sh600000", "测试股份", bars)

        self.assertEqual(FiveOneThreeStage.CONFIRMED, result["stage"])
        self.assertEqual("5连阳 5/5｜倍量阳 1/1｜缩量确认 3/3", result["detail"])


class FiveOneThreeOrderTests(unittest.TestCase):
    def test_confirmed_first_then_gain_desc_then_name(self):
        hits = [
            {"name": "乙", "stage": FiveOneThreeStage.OBSERVING, "gain60": 5.0},
            {"name": "甲", "stage": FiveOneThreeStage.CONFIRMED, "gain60": 3.0},
            {"name": "丙", "stage": FiveOneThreeStage.CONFIRMED, "gain60": 12.0},
            {"name": "丁", "stage": FiveOneThreeStage.WAITING_CONFIRMATION, "gain60": 9.0},
        ]

        ordered = order_five_one_three_hits(hits)

        self.assertEqual(["丙", "甲", "丁", "乙"], [item["name"] for item in ordered])

    def test_same_stage_same_gain_sorted_by_name(self):
        # 与 Kotlin thenBy{name} 一致，按字符串自然序（Unicode 码点）排，不是拼音序。
        hits = [
            {"name": "乙", "stage": FiveOneThreeStage.OBSERVING, "gain60": 1.0},
            {"name": "甲", "stage": FiveOneThreeStage.OBSERVING, "gain60": 1.0},
        ]

        ordered = order_five_one_three_hits(hits)

        self.assertEqual(["乙", "甲"], [item["name"] for item in ordered])


class FiveOneThreeMenuTests(unittest.TestCase):
    """513 结果写进「📈 指标策略」子菜单的行格式。"""

    @staticmethod
    def _titles(five_data, stats=None, strict_data=None, relaxed_data=None):
        from stock_menubar import StockMenuBar

        class _Scheduler:
            def last_success_at(self):
                return None

            def last_error(self):
                return None

        class _Stub:
            indicator_data = strict_data or []
            relaxed_indicator_data = relaxed_data or []
            five_one_three_data = five_data
            five_one_three_stats = stats or {
                'total_stocks': 0,
                'history_available': 0,
                'historical_five_bullish': 0,
                'recent_five_bullish': 0,
                'waiting_confirmation': 0,
                'confirmed': 0,
            }
            indicator_results_stale = False
            indicator_results_stale_at = None
            _last_intraday_indicator_at = None
            indicator_refresh_scheduler = _Scheduler()

            def indicator_refresh_status(self):
                return "测试状态"

            def _menu_rows(self, rows, limit=30):
                return StockMenuBar._menu_rows(rows, limit)

        return StockMenuBar.indicator_menu_titles(_Stub())

    def test_menu_rows_render_stage_detail_and_gain(self):
        titles = self._titles([
            {
                "name": "测试股份",
                "price": 18.2,
                "progress": "形态已确认",
                "detail": "5连阳 5/5｜倍量阳 1/1｜缩量确认 3/3",
                "gain60": -12.5,
            }
        ])

        self.assertIn("513形态（1只）", titles)
        self.assertIn(
            "测试股份 | 18.20 | 形态已确认 | 5连阳 5/5｜倍量阳 1/1｜缩量确认 3/3 | 60日-12.5%",
            titles,
        )

    def test_menu_empty_placeholder(self):
        titles = self._titles([])

        self.assertIn("513形态（0只）", titles)
        self.assertIn("暂无符合513形态的股票", titles)

    def test_result_lists_come_first_and_rules_are_not_rendered(self):
        titles = self._titles(
            [{
                "name": "513股份",
                "price": 18.2,
                "progress": "形态已确认",
                "detail": "5连阳 5/5｜倍量阳 1/1｜缩量确认 3/3",
                "gain60": -12.5,
            }],
            strict_data=[{
                "name": "严格股份",
                "price": 10.0,
                "rsi": 22.0,
                "k": 20.0,
                "d": 18.0,
            }],
            relaxed_data=[{
                "name": "宽松股份",
                "price": 11.0,
                "rsi": 35.0,
                "k": 30.0,
                "d": 28.0,
                "relaxed_score": 2,
                "macd_detail": "柱体转正",
                "rsi_ok": True,
                "kdj_ok": True,
            }],
        )
        text = "\n".join(titles)

        self.assertLess(text.index("第一档 · 严格策略"), text.index("第二档 · 宽松预警"))
        self.assertIn("严格股份 | 10.00 | RSI 22 | K 20 > D 18", titles)
        self.assertIn("宽松股份 | 11.00 | 2/3分 | MACD:柱体转正 | RSI | KDJ金叉", titles)
        self.assertNotIn("MACD：", text)
        self.assertNotIn("RSI < 30", text)
        self.assertNotIn("刷新规则", text)
        self.assertNotIn("扫描范围", text)

    def test_menu_caps_rows_when_flooded(self):
        from stock_menubar import StockMenuBar
        rows, hidden = StockMenuBar._menu_rows([{"i": i} for i in range(35)], limit=30)
        self.assertEqual(30, len(rows))
        self.assertEqual(5, hidden)
        rows, hidden = StockMenuBar._menu_rows([{"i": i} for i in range(3)], limit=30)
        self.assertEqual(3, len(rows))
        self.assertEqual(0, hidden)

        titles = self._titles([
            {"name": f"股{i}", "price": 1.0, "progress": "5连阳观察",
             "detail": "5连阳 5/5｜倍量阳 0/1｜缩量确认 0/3", "gain60": 1.0}
            for i in range(35)
        ])
        self.assertIn("  …其余 5 只省略", titles)

    def test_menu_renders_funnel_stats(self):
        titles = self._titles([], stats={
            'total_stocks': 5200,
            'history_available': 5100,
            'historical_five_bullish': 320,
            'recent_five_bullish': 45,
            'waiting_confirmation': 12,
            'confirmed': 3,
        })

        self.assertIn("  全市场扫描5200只｜有效日线5100只", titles)
        self.assertIn("  历史5连阳320只｜当前5连阳45只", titles)
        self.assertIn("  倍量阳确认12只｜完整513确认3只", titles)


if __name__ == "__main__":
    unittest.main()
