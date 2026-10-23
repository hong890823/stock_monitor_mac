#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""菜单标题格式化的健壮性测试。

背景：`指数菜单更新失败` 曾一天报 8454 次，日志里只留了 `{e}` 没有堆栈，
无法反推根因。这里锁住「格式化函数遇到 None / 缺字段 / 非数值时不得抛异常」，
把整类故障挡在源头。
"""

import unittest
from unittest import mock

import stock_menubar
from stock_menubar import StockMenuBar


class SafeFloatTests(unittest.TestCase):
    def test_none_falls_back_to_default(self):
        self.assertEqual(StockMenuBar._safe_float(None), 0.0)
        self.assertEqual(StockMenuBar._safe_float(None, 1.5), 1.5)

    def test_numeric_strings_are_coerced(self):
        self.assertEqual(StockMenuBar._safe_float("12.5"), 12.5)

    def test_garbage_falls_back(self):
        self.assertEqual(StockMenuBar._safe_float("abc"), 0.0)
        self.assertEqual(StockMenuBar._safe_float(object()), 0.0)

    def test_nan_falls_back(self):
        self.assertEqual(StockMenuBar._safe_float(float("nan")), 0.0)

    def test_zero_is_preserved(self):
        self.assertEqual(StockMenuBar._safe_float(0), 0.0)


class FormatPriceTests(unittest.TestCase):
    def test_rounds_at_or_above_one_hundred(self):
        self.assertEqual(StockMenuBar._format_price(3842.6), "3843")

    def test_keeps_two_decimals_below_one_hundred(self):
        self.assertEqual(StockMenuBar._format_price(18.21), "18.21")

    def test_none_becomes_zero_not_an_exception(self):
        self.assertEqual(StockMenuBar._format_price(None), "0.00")


class FormatBarQuoteTests(unittest.TestCase):
    def test_normal_quote(self):
        self.assertEqual(
            StockMenuBar._format_bar_quote("中天", 18.2, -1.1),
            "中天18.20 -1.1%",
        )

    def test_missing_values_do_not_raise(self):
        # 曾经这里只要 price 或 pct 是 None 就 TypeError，打断整次菜单刷新
        self.assertEqual(StockMenuBar._format_bar_quote("中天", None, None), "中天0.00 +0.0%")


class ClipAlertMessageTests(unittest.TestCase):
    """弹窗正文截断：正文过长会把 NSAlert 撑到屏幕外，OK 按钮点不到就关不掉。"""

    def test_short_string_unchanged(self):
        self.assertEqual("出错了", StockMenuBar._clip_alert_message("出错了"))

    def test_long_string_is_truncated(self):
        text = StockMenuBar._clip_alert_message("x" * 500)
        self.assertEqual(301, len(text))  # 300 + 省略号
        self.assertTrue(text.endswith("…"))

    def test_non_string_is_stringified(self):
        # 2026-09-28 基本面分析把 (result, report) 元组当字符串传给弹窗
        clipped = StockMenuBar._clip_alert_message(("a", "b"))
        self.assertIsInstance(clipped, str)
        self.assertIn("a", clipped)

    def test_tuple_report_is_clipped(self):
        report = (object(), "长" * 8000)
        clipped = StockMenuBar._clip_alert_message(report)
        self.assertLessEqual(len(clipped), 301)


class FormatMarketOverviewTitleTests(unittest.TestCase):
    """直接覆盖曾经一天失败 8454 次的那条路径。"""

    def _app(self, turnover):
        app = StockMenuBar.__new__(StockMenuBar)  # 不跑 __init__，避免拉起 AppKit
        app.market_turnover = turnover
        app._turnover_snapshot = None
        app._trading_minutes_elapsed = mock.Mock(return_value=240)
        return app

    def test_normal_indexes(self):
        app = self._app({"today": 2.1e12, "previous": 2.0e12, "date": "2026-09-24"})
        title = app._format_market_overview_title(
            [{"name": "上证", "price": 3842.6, "pct": 0.79}]
        )
        self.assertIn("上证3843+0.8%", title)
        self.assertIn("总21,000亿", title)
        self.assertIn("昨全天+1,000亿(+5.0%)", title)

    def test_snapshot_same_time_comparison(self):
        from turnover_tracker import Snapshot, Source

        app = self._app({"today": 2.1e12, "previous": 2.0e12, "date": "2026-09-28"})
        app._turnover_snapshot = Snapshot(
            slot="10:00",
            slot_minute=30,
            today=8000.0,
            previous_same_time=7600.0,
            previous_full_day=17000.0,
            predicted_full_day=16800.0,
            source=Source.LOCAL,
        )
        title = app._format_market_overview_title(
            [{"name": "上证", "price": 3823.6, "pct": -1.67}]
        )
        self.assertIn("昨同期+400亿(+5.3%)", title)
        self.assertIn("预估16,800亿", title)

    def test_none_price_and_pct_do_not_raise(self):
        app = self._app({"today": 0, "previous": None, "date": None})
        title = app._format_market_overview_title(
            [{"name": "上证", "price": None, "pct": None}]
        )
        self.assertIn("上证0.00+0.0%", title)

    def test_missing_name_falls_back_to_code(self):
        app = self._app({"today": 0, "previous": None, "date": None})
        title = app._format_market_overview_title(
            [{"code": "sh000001", "price": 10, "pct": 0.0}]
        )
        self.assertIn("sh000001", title)

    def test_none_turnover_values_do_not_raise(self):
        # market_turnover 里 today/previous 为 None 时，比较运算曾经直接 TypeError
        app = self._app({"today": None, "previous": None, "date": None})
        title = app._format_market_overview_title([])
        self.assertEqual(title, "市场概览")

    def test_non_dict_items_are_skipped(self):
        app = self._app({"today": 0, "previous": None, "date": None})
        title = app._format_market_overview_title([None, "junk", 42])
        self.assertEqual(title, "市场概览")

    def test_empty_index_falls_back_to_default_title(self):
        app = self._app({"today": 0, "previous": None, "date": None})
        self.assertEqual(app._format_market_overview_title([]), "市场概览")


class MarketTurnoverLinesTests(unittest.TestCase):
    def test_none_today_does_not_raise(self):
        app = StockMenuBar.__new__(StockMenuBar)
        app.market_turnover = {"today": None, "previous": None, "date": None}
        self.assertEqual(app._market_turnover_lines(), ["暂无市场成交数据"])


class MarketSummaryVolumeTests(unittest.TestCase):
    def test_heavy_selloff_requires_turnover_expansion(self):
        from turnover_tracker import Snapshot, Source

        app = StockMenuBar.__new__(StockMenuBar)
        app.market_turnover = {"today": 0, "previous": None, "date": None}
        app._turnover_snapshot = Snapshot(
            slot="10:00",
            slot_minute=30,
            today=8000.0,
            previous_same_time=7000.0,
            previous_full_day=17000.0,
            predicted_full_day=19000.0,
            source=Source.LOCAL,
        )

        self.assertTrue(app._market_turnover_expanded())

        app._turnover_snapshot = Snapshot(
            slot="10:00",
            slot_minute=30,
            today=7000.0,
            previous_same_time=8000.0,
            previous_full_day=17000.0,
            predicted_full_day=15000.0,
            source=Source.LOCAL,
        )
        self.assertFalse(app._market_turnover_expanded())


class _FakeMenuItem:
    """不碰 AppKit 的菜单项替身；故意没有 `separator()`（真实 rumps 也没有）。"""

    def __init__(self, title=None, callback=None):
        self.title = title
        self.callback = callback
        self.children = []

    def add(self, item):
        self.children.append(item)

    def clear(self):
        self.children = []


class _FakeRumps:
    MenuItem = _FakeMenuItem
    separator = object()


class IndicatorMenuSeparatorTests(unittest.TestCase):
    """`rumps.MenuItem.separator()` 不存在——分隔线是 `rumps.separator` 哨兵。

    这里曾经抛 AttributeError，指标结果一行都进不了子菜单，
    且异常被 update_display 吞掉，只在日志里堆「指标策略更新失败」
    （2026-10-12 排查今日盈亏时发现，日志里已堆 4690 条）。
    """

    def _app(self):
        app = StockMenuBar.__new__(StockMenuBar)  # 不跑 __init__，避免拉起 AppKit
        # `menu` 是 rumps 的 property，赋值会走真实 NSMenu；直接写 `_menu`。
        app._menu = {"📈 选股": _FakeMenuItem("📈 选股")}
        app.indicator_menu_titles = lambda: ["趋势突破 甲", "513 乙"]
        return app

    def test_results_are_added_after_the_separator(self):
        app = self._app()
        with mock.patch.object(stock_menubar, "rumps", _FakeRumps):
            app.update_selection_menu()
        children = app._menu["📈 选股"].children
        titles = [c.title for c in children if isinstance(c, _FakeMenuItem)]
        self.assertIn("趋势突破 甲", titles)
        self.assertIn("513 乙", titles)
        # 分隔线必须真的被加进去（且不是 API 不存在的假分隔线）
        self.assertTrue(any(not isinstance(c, _FakeMenuItem) for c in children))

    def test_results_come_after_the_action_buttons(self):
        app = self._app()
        with mock.patch.object(stock_menubar, "rumps", _FakeRumps):
            app.update_selection_menu()
        titles = [c.title for c in app._menu["📈 选股"].children
                  if isinstance(c, _FakeMenuItem)]
        self.assertEqual(titles[-1], "513 乙")
        self.assertLess(
            titles.index("🔄 重扫选股（前200，秒级）"),
            titles.index("趋势突破 甲"),
        )


if __name__ == "__main__":
    unittest.main()
