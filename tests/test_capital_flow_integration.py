#!/usr/bin/env python3
"""盘中资金流接入层测试。"""

import threading
import json
import tempfile
import unittest
from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime, timedelta
from unittest import mock

import pandas as pd

from capital_flow import CapitalFlowSnapshot, FlowCache, FlowDirectionTracker
from stock_menubar import StockMenuBar


def snapshot(key, net, kind="stock", as_of=None):
    as_of = as_of or datetime(2026, 10, 12, 10, 0)
    return CapitalFlowSnapshot(
        key=key,
        name=key,
        kind=kind,
        quote_date=as_of.strftime("%Y-%m-%d"),
        as_of=as_of,
        source="test",
        scope="个股" if kind == "stock" else "行业板块",
        session="intraday",
        net_yi=net,
        net_ratio_pct=2.0 if net > 0 else -2.0,
    )


class CapitalFlowIntegrationTests(unittest.TestCase):
    def app(self):
        app = StockMenuBar.__new__(StockMenuBar)
        app.portfolio = {"positions": [{"code": "sh600519", "name": "贵州茅台"}]}
        app._sector_map = {"sh600519": ["白酒"]}
        app._capital_flow_refresh_lock = threading.Lock()
        app._capital_flow_lock = threading.Lock()
        app._capital_flow_refreshing = False
        app._capital_flow_next_refresh_monotonic = 0
        app._capital_flow_refresh_interval_seconds = 60
        app._capital_flow_fail_backoff_seconds = 120
        app._capital_flow_cache = FlowCache(max_age_seconds=150)
        app._capital_flow_stock_tracker = FlowDirectionTracker(required_count=2)
        app._capital_flow_sector_tracker = FlowDirectionTracker(required_count=2)
        app.log_dir = tempfile.gettempdir()
        app._capital_flow_file = ""
        app.log = mock.Mock()
        app._ensure_sector_map = mock.Mock(return_value=app._sector_map)
        app._is_trading_hours = mock.Mock(return_value=True)
        return app

    def test_refresh_is_started_once_within_minimum_interval(self):
        app = self.app()
        with mock.patch("stock_menubar.threading.Thread") as thread:
            self.assertTrue(app._maybe_refresh_capital_flow(now_monotonic=100))
            self.assertFalse(app._maybe_refresh_capital_flow(now_monotonic=120))

        self.assertEqual(1, thread.call_count)
        thread.return_value.start.assert_called_once()

    def test_refresh_failure_keeps_quote_path_unblocked_and_backs_off(self):
        app = self.app()
        with mock.patch("stock_menubar.fetch_stock_flow_snapshot", side_effect=RuntimeError("down")), \
                mock.patch("stock_menubar.fetch_sector_flow", side_effect=RuntimeError("down")):
            app._refresh_capital_flow(["sh600519"], ["白酒"], now_monotonic=100)

        self.assertFalse(app._capital_flow_refreshing)
        self.assertEqual(220, app._capital_flow_next_refresh_monotonic)
        self.assertIsNone(app._capital_flow_cache.get_any("sh600519"))

    def test_successful_refresh_updates_cache_and_writes_only_related_scope(self):
        app = self.app()
        now = datetime.now().replace(microsecond=0)
        app._capital_flow_file = tempfile.mktemp(
            dir=app.log_dir, prefix="capital-flow-test-", suffix=".json"
        )
        stock = snapshot("sh600519", 1.5, as_of=now)
        sector = snapshot("白酒", 8.0, kind="sector", as_of=now)
        try:
            with mock.patch("stock_menubar.fetch_stock_flow_snapshot", return_value=stock), \
                    mock.patch("stock_menubar.fetch_sector_flow", return_value={"白酒": sector, "无关": snapshot("无关", 99, kind="sector", as_of=now)}):
                app._refresh_capital_flow(["sh600519"], ["白酒"], now_monotonic=100)

            self.assertIs(app._capital_flow_cache.get_any("sh600519"), stock)
            with open(app._capital_flow_file, encoding="utf-8") as handle:
                payload = json.load(handle)
            self.assertIn("sh600519", payload["stocks"])
            self.assertIn("白酒", payload["sectors"])
            self.assertNotIn("无关", payload["sectors"])
        finally:
            try:
                import os
                os.unlink(app._capital_flow_file)
            except OSError:
                pass

    def test_refreshes_industry_and_concept_targets(self):
        app = self.app()
        now = datetime.now().replace(microsecond=0)
        industry = snapshot("白酒", 8.0, kind="sector", as_of=now)
        concept = snapshot("人工智能", 6.0, kind="sector", as_of=now)

        def fetch(kind):
            return {
                "industry": {"白酒": industry, "无关行业": snapshot("无关行业", 99, kind="sector", as_of=now)},
                "concept": {"人工智能": concept, "无关概念": snapshot("无关概念", 88, kind="sector", as_of=now)},
            }[kind]

        with mock.patch("stock_menubar.fetch_stock_flow_snapshot", return_value=None), \
                mock.patch("stock_menubar.fetch_sector_flow", side_effect=fetch) as fetch_flow:
            app._refresh_capital_flow(
                ["sh600519"], ["白酒", "人工智能"], now_monotonic=100
            )

        self.assertEqual(
            [mock.call("industry"), mock.call("concept")],
            fetch_flow.call_args_list,
        )
        self.assertIs(app._capital_flow_cache.get_any("白酒"), industry)
        self.assertIs(app._capital_flow_cache.get_any("人工智能"), concept)
        self.assertIsNone(app._capital_flow_cache.get_any("无关行业"))
        self.assertIsNone(app._capital_flow_cache.get_any("无关概念"))

    def test_builds_concept_mapping_for_current_concept_watchlist(self):
        app = self.app()
        app.concept_data = {"top": [{"name": "人工智能"}], "bottom": []}
        app._capital_flow_concept_map = {}
        app._capital_flow_concept_map_names = set()
        app._capital_flow_concept_map_refreshing = False
        app.requests_timeout = lambda _seconds: nullcontext()
        constituents = pd.DataFrame([{"代码": "600519", "名称": "贵州茅台"}])

        with mock.patch("akshare.stock_board_concept_cons_em", return_value=constituents):
            app._refresh_capital_flow_concept_map(["人工智能"])

        self.assertEqual(["人工智能"], app._capital_flow_sector_names_for("sh600519")[1:])
        self.assertEqual({"人工智能"}, app._capital_flow_concept_map_names)

    def test_flow_confirmation_includes_ratio_and_optional_large_orders(self):
        app = self.app()
        for minute in (0, 1):
            when = datetime(2026, 10, 12, 10, minute)
            stock = snapshot("sh600519", 2, as_of=when)
            stock = replace(
                stock,
                net_ratio_pct=2.4,
                large_net_yi=0.8,
                super_large_net_yi=1.1,
            )
            sector = snapshot("白酒", 10, kind="sector", as_of=when)
            app._capital_flow_cache.put(stock)
            app._capital_flow_cache.put(sector)
            app._capital_flow_stock_tracker.observe(stock)
            app._capital_flow_sector_tracker.observe(sector)

        state, note, level = app._capital_flow_signal_context(
            "sh600519", "放量突破", now=datetime(2026, 10, 12, 10, 1, 30)
        )

        self.assertEqual("breakout_confirmed", state)
        self.assertIsNone(level)
        self.assertIn("平台口径主力净流入", note)
        self.assertIn("主力净占比 +2.40%", note)
        self.assertIn("超大单 +1.1亿", note)
        self.assertIn("大单 +0.8亿", note)

    def test_breakout_divergence_is_observation_only(self):
        app = self.app()
        for minute in (0, 1):
            when = datetime(2026, 10, 12, 10, minute)
            stock = snapshot("sh600519", -2, as_of=when)
            sector = snapshot("白酒", -10, kind="sector", as_of=when)
            app._capital_flow_cache.put(stock)
            app._capital_flow_cache.put(sector)
            app._capital_flow_stock_tracker.observe(stock)
            app._capital_flow_sector_tracker.observe(sector)

        state, note, level = app._capital_flow_signal_context(
            "sh600519", "放量突破", now=datetime(2026, 10, 12, 10, 1, 30)
        )

        self.assertEqual("divergence_observation", state)
        self.assertIn("上涨缺乏资金确认", note)
        self.assertIsNone(level)

    def test_stale_flow_does_not_confirm_existing_signal(self):
        app = self.app()
        old = datetime(2026, 10, 12, 9, 0)
        app._capital_flow_cache.put(snapshot("sh600519", -2, as_of=old))
        app._capital_flow_cache.put(snapshot("白酒", -10, kind="sector", as_of=old))
        app._capital_flow_stock_tracker.observe(snapshot("sh600519", -2, as_of=old))
        app._capital_flow_stock_tracker.observe(
            snapshot("sh600519", -2, as_of=old + timedelta(minutes=1))
        )
        app._capital_flow_sector_tracker.observe(snapshot("白酒", -10, kind="sector", as_of=old))
        app._capital_flow_sector_tracker.observe(
            snapshot("白酒", -10, kind="sector", as_of=old + timedelta(minutes=1))
        )

        state, note, level = app._capital_flow_signal_context(
            "sh600519", "跌破支撑", now=datetime(2026, 10, 12, 10, 0)
        )

        self.assertIsNone(state)
        self.assertEqual("", note)
        self.assertIsNone(level)

    def test_break_support_adds_risk_confirmation_after_two_new_samples(self):
        app = self.app()
        for minute in (0, 1):
            when = datetime(2026, 10, 12, 10, minute)
            stock = snapshot("sh600519", -2, as_of=when)
            sector = snapshot("白酒", -10, kind="sector", as_of=when)
            app._capital_flow_cache.put(stock)
            app._capital_flow_cache.put(sector)
            app._capital_flow_stock_tracker.observe(stock)
            app._capital_flow_sector_tracker.observe(sector)

        state, note, level = app._capital_flow_signal_context(
            "sh600519", "跌破支撑", now=datetime(2026, 10, 12, 10, 1, 30)
        )

        self.assertEqual("risk_confirmed", state)
        self.assertIn("平台口径主力净流出", note)
        self.assertEqual("danger", level)

    def test_breakout_adds_inflow_confirmation_without_new_signal_type(self):
        app = self.app()
        for minute in (0, 1):
            when = datetime(2026, 10, 12, 10, minute)
            stock = snapshot("sh600519", 2, as_of=when)
            sector = snapshot("白酒", 10, kind="sector", as_of=when)
            app._capital_flow_cache.put(stock)
            app._capital_flow_cache.put(sector)
            app._capital_flow_stock_tracker.observe(stock)
            app._capital_flow_sector_tracker.observe(sector)

        state, note, level = app._capital_flow_signal_context(
            "sh600519", "放量突破", now=datetime(2026, 10, 12, 10, 1, 30)
        )

        self.assertEqual("breakout_confirmed", state)
        self.assertIn("平台口径主力净流入", note)
        self.assertIsNone(level)


if __name__ == "__main__":
    unittest.main()
