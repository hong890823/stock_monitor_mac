#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import unittest
import inspect
from types import SimpleNamespace
from unittest.mock import Mock

from menu_bar_window import (
    build_summary,
    build_watchlist_quotes,
    native_menu_root,
    order_main_menu,
    safe_show,
)
from stock_menubar import SETTINGS_TITLE, PORTFOLIO_SETTINGS_TITLE


class MenuBarIntegrationTests(unittest.TestCase):
    def test_settings_icons_are_distinct(self):
        self.assertEqual(PORTFOLIO_SETTINGS_TITLE, "⚙️ 持仓设置")
        self.assertEqual(SETTINGS_TITLE, "🔧 设置")

    def test_watchlist_menu_rows_are_clickable(self):
        from stock_menubar import StockMenuBar

        source = inspect.getsource(StockMenuBar._refresh_menu_display)
        self.assertIn("callback=lambda sender", source)
        self.assertIn("self.show_watchlist_stock", source)

    def test_watchlist_submenu_combines_self_selected_and_holding_quotes(self):
        rows = build_watchlist_quotes(
            watchlist=[{"code": "sh600001", "name": "自选"}],
            positions=[
                {"code": "sh600001", "name": "自选"},
                {"code": "sh600002", "name": "持仓"},
            ],
            stock_data={
                "sh600001": {"price": 10.0, "change_pct": 1.2},
                "sh600002": {"price": 20.0, "change_pct": -0.5},
            },
        )
        self.assertEqual([row["groups"] for row in rows], [["自选股", "持仓股"], ["持仓股"]])

    def test_watchlist_submenu_includes_stocks_waiting_for_quotes(self):
        rows = build_watchlist_quotes(
            watchlist=[{"code": "sh600001", "name": "自选"}],
            positions=[{"code": "sz000002", "name": "持仓"}],
            stock_data={},
            include_missing=True,
        )
        self.assertEqual(
            [(row["code"], row["available"]) for row in rows],
            [("sh600001", False), ("sz000002", False)],
        )

    def test_watchlist_parent_keeps_submenu_and_summary_action(self):
        from stock_menubar import StockMenuBar

        source = inspect.getsource(StockMenuBar.__init__)
        self.assertIn('"📈 监控股票", callback=None', source)
        self.assertNotIn('📋 查看全部行情', source)
        refresh_source = inspect.getsource(StockMenuBar._refresh_menu_display)
        self.assertNotIn('📋 查看全部行情', refresh_source)
        self.assertIn('include_missing=True', inspect.getsource(StockMenuBar._build_menu_data))
        self.assertIn('等待行情', refresh_source)
        self.assertIn('setSubmenu_', refresh_source)
        self.assertIn('setEnabled_(True)', refresh_source)

    def test_main_menu_order_places_reports_above_settings_and_leader_after_portfolio_settings(self):
        labels = {
            "market": "市场概况",
            "watchlist": "监控股票",
            "top_sector": "涨幅榜",
            "bottom_sector": "跌幅榜",
            "concept": "概念板块",
            "limit": "涨跌停",
            "selection": "📈 选股",
            "technical": "技术分析",
            "fundamentals": "基本面",
            "global": "全球",
            "ai": "AI",
            "portfolio_info": "持仓信息",
            "portfolio_settings": "持仓设置",
            "sector_leaders": "板块龙头",
            "trade_calculator": "买卖成本计算",
            "target_cost": "目标成本",
            "summary": "盘面总结",
            "headlines": "今日要闻",
            "settings": "设置",
            "add": "添加股票",
            "remove": "删除股票",
            "refresh": "立即刷新",
            "quit": "退出",
        }
        ordered = order_main_menu(labels)
        self.assertEqual(ordered[ordered.index("市场概况") + 1], "监控股票")
        self.assertEqual(ordered[ordered.index("持仓设置") + 1], "板块龙头")
        self.assertEqual(ordered[ordered.index("设置") - 2], "盘面总结")
        self.assertEqual(ordered[ordered.index("设置") - 1], "今日要闻")

    def test_display_refresh_routes_through_menu_tracking_coordinator(self):
        from stock_menubar import StockMenuBar

        app = StockMenuBar.__new__(StockMenuBar)
        app._menu_update_coordinator = Mock()
        app._refresh_menu_display = Mock()
        app.update_display()
        app._menu_update_coordinator.request_update.assert_called_once_with()
        app._refresh_menu_display.assert_not_called()

    def test_build_summary_keeps_current_alert_count_and_quote_rows(self):
        text = build_summary({
            "indicator": {"buy_count": 1, "ready_count": 2},
            "index": {"items": [{"name": "上证", "price": 3000.0, "pct": 1.2}]},
            "stock": {"items": [{"name": "样本", "price": 10.0, "pct": -0.5}]},
        }, ["两市成交 1.8万亿"])
        self.assertEqual(text.count("今日曾提醒"), 1)
        self.assertIn("📋 今日曾提醒1", text)
        self.assertIn("🟡 候选2", text)
        self.assertIn("上证 3000.00 ▲1.20%", text)
        self.assertIn("样本 10.00 ▼0.50%", text)
        self.assertIn("两市成交 1.8万亿", text)
        self.assertLessEqual(len(text.splitlines()), 4)

    def test_build_summary_puts_quote_rows_side_by_side_and_deduplicates(self):
        text = build_summary({
            "indicator": {"buy_count": 0, "ready_count": 0},
            "index": {"items": [
                {"name": "上证", "price": 3000.0, "pct": 1.2},
                {"name": "深证", "price": 10000.0, "pct": -0.2},
            ]},
            "stock": {"items": [
                {"name": "上证", "price": 3000.0, "pct": 1.2},
                {"name": "样本", "price": 10.0, "pct": -0.5},
            ]},
        }, ["两市成交 1.8万亿"])
        self.assertEqual(text.count("上证"), 1)
        self.assertIn("上证 3000.00 ▲1.20%  |  深证 10000.00 ▼0.20%", text)
        self.assertIn("样本 10.00 ▼0.50%", text)

    def test_build_summary_shows_six_quotes_before_using_menu_hint(self):
        rows = [
            {"name": f"股票{index}", "price": 10.0 + index, "pct": index / 10}
            for index in range(1, 7)
        ]
        text = build_summary({
            "indicator": {"buy_count": 0, "ready_count": 0},
            "index": {"items": rows[:3]},
            "stock": {"items": rows[3:]},
        }, ["两市成交 1.8万亿"])
        self.assertNotIn("其余", text)
        for index in range(1, 7):
            self.assertIn(f"股票{index}", text)

    def test_native_menu_root_reuses_current_rumps_menu(self):
        native = object()
        app = SimpleNamespace(_menu=SimpleNamespace(_menu=native))
        self.assertIs(native_menu_root(app), native)

    def test_menu_error_keeps_window_disabled_without_raising(self):
        errors = []
        window = Mock()
        window.show.side_effect = RuntimeError("AppKit unavailable")
        self.assertFalse(safe_show(window, errors.append))
        self.assertEqual(len(errors), 1)


if __name__ == "__main__":
    unittest.main()
