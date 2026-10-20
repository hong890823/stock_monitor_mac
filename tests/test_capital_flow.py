#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""盘中资金流标准化、缓存和确认逻辑测试。"""

import unittest
from datetime import datetime, timedelta
from dataclasses import replace

import pandas as pd

from capital_flow import (
    CapitalFlowSnapshot,
    FlowCache,
    FlowDirectionTracker,
    is_fresh,
    normalize_flow_frame,
    parse_amount_yi,
    parse_ratio_pct,
    fetch_sector_flow,
    flow_confirmation_state,
)


class CapitalFlowParsingTests(unittest.TestCase):
    def test_amount_parser_normalizes_yi_wan_and_yuan(self):
        self.assertEqual(1.2, parse_amount_yi("1.2亿"))
        self.assertEqual(1.2, parse_amount_yi("12000万"))
        self.assertEqual(1.2, parse_amount_yi(120000000))
        self.assertIsNone(parse_amount_yi("--"))

    def test_ratio_parser_accepts_percent_text(self):
        self.assertEqual(2.5, parse_ratio_pct("+2.5%"))
        self.assertEqual(-1.2, parse_ratio_pct(-1.2))
        self.assertIsNone(parse_ratio_pct("--"))

    def test_normalize_sector_frame_preserves_scope_and_units(self):
        as_of = datetime(2026, 10, 12, 10, 30)
        frame = pd.DataFrame([
            {
                "行业": "电源设备",
                "流入资金": "12.0亿",
                "流出资金": "9.5亿",
                "净额": "2.5亿",
                "行业-涨跌幅": "1.20",
            }
        ])

        result = normalize_flow_frame(
            frame,
            kind="sector",
            key_column="行业",
            name_column="行业",
            net_column="净额",
            inflow_column="流入资金",
            outflow_column="流出资金",
            ratio_column="行业-涨跌幅",
            quote_date="2026-10-12",
            as_of=as_of,
            source="test",
            scope="行业板块",
            session="intraday",
        )

        snapshot = result["电源设备"]
        self.assertEqual(2.5, snapshot.net_yi)
        self.assertEqual(12.0, snapshot.inflow_yi)
        self.assertEqual(9.5, snapshot.outflow_yi)
        self.assertEqual(1.2, snapshot.net_ratio_pct)
        self.assertEqual("行业板块", snapshot.scope)
        self.assertFalse(snapshot.stale)

    def test_normalize_stock_frame_reads_yuan_net_amount_and_ratio(self):
        frame = pd.DataFrame([
            {
                "代码": "600519",
                "名称": "贵州茅台",
                "今日主力净流入-净额": 120000000,
                "今日主力净流入-净占比": 3.4,
            }
        ])

        result = normalize_flow_frame(
            frame,
            kind="stock",
            key_column="代码",
            name_column="名称",
            net_column="今日主力净流入-净额",
            ratio_column="今日主力净流入-净占比",
            quote_date="2026-10-12",
            as_of=datetime(2026, 10, 12, 10, 30),
            source="test",
            scope="个股",
            session="intraday",
        )

        self.assertEqual(1.2, result["600519"].net_yi)
        self.assertEqual(3.4, result["600519"].net_ratio_pct)

    def test_sector_flow_does_not_treat_price_change_as_net_ratio(self):
        frame = pd.DataFrame([
            {
                "行业": "电源设备",
                "行业-涨跌幅": 5.0,
                "流入资金": "12.0亿",
                "流出资金": "9.5亿",
                "净额": "2.5亿",
            }
        ])

        result = fetch_sector_flow(
            "industry",
            as_of=datetime(2026, 10, 12, 10, 30),
            fetcher=lambda _: frame,
        )

        self.assertIsNone(result["电源设备"].net_ratio_pct)


class CapitalFlowFreshnessTests(unittest.TestCase):
    def test_snapshot_is_stale_when_date_or_age_does_not_match(self):
        snapshot = CapitalFlowSnapshot(
            key="600519",
            name="贵州茅台",
            kind="stock",
            quote_date="2026-10-12",
            as_of=datetime(2026, 10, 12, 10, 0),
            source="test",
            scope="个股",
            session="intraday",
            net_yi=1.0,
        )

        self.assertTrue(is_fresh(snapshot, datetime(2026, 10, 12, 10, 1)))
        self.assertFalse(is_fresh(snapshot, datetime(2026, 10, 12, 10, 3), max_age_seconds=120))
        self.assertFalse(is_fresh(snapshot, datetime(2026, 10, 13, 10, 0)))

    def test_cache_returns_only_fresh_snapshot(self):
        cache = FlowCache(max_age_seconds=120)
        base = datetime(2026, 10, 12, 10, 0)
        snapshot = CapitalFlowSnapshot(
            key="600519",
            name="贵州茅台",
            kind="stock",
            quote_date="2026-10-12",
            as_of=base,
            source="test",
            scope="个股",
            session="intraday",
            net_yi=1.0,
        )
        cache.put(snapshot)

        self.assertIs(cache.get("600519", now=base + timedelta(seconds=60)), snapshot)
        self.assertIsNone(cache.get("600519", now=base + timedelta(seconds=121)))
        self.assertTrue(cache.needs_refresh("600519", now=base + timedelta(seconds=121)))


class CapitalFlowConfirmationTests(unittest.TestCase):
    def _snapshot(self, net):
        return CapitalFlowSnapshot(
            key="600519",
            name="贵州茅台",
            kind="stock",
            quote_date="2026-10-12",
            as_of=datetime(2026, 10, 12, 10, 0),
            source="test",
            scope="个股",
            session="intraday",
            net_yi=net,
        )

    def test_direction_requires_two_consecutive_observations(self):
        tracker = FlowDirectionTracker(required_count=2)

        self.assertEqual(1, tracker.observe(self._snapshot(2.0)))
        self.assertFalse(tracker.confirmed("600519"))
        self.assertEqual(
            2,
            tracker.observe(replace(self._snapshot(1.0), as_of=datetime(2026, 10, 12, 10, 1))),
        )
        self.assertTrue(tracker.confirmed("600519"))

    def test_direction_resets_when_sign_changes_or_data_missing(self):
        tracker = FlowDirectionTracker(required_count=2)

        tracker.observe(self._snapshot(-2.0))
        self.assertEqual(1, tracker.observe(self._snapshot(2.0)))
        self.assertFalse(tracker.confirmed("600519"))
        self.assertEqual(0, tracker.observe(self._snapshot(None)))
        self.assertFalse(tracker.confirmed("600519"))

    def test_duplicate_snapshot_does_not_count_as_a_new_confirmation(self):
        tracker = FlowDirectionTracker(required_count=2)
        snapshot = self._snapshot(-2.0)

        self.assertEqual(1, tracker.observe(snapshot))
        self.assertEqual(1, tracker.observe(snapshot))

    def test_break_support_is_only_strengthened_by_confirmed_double_outflow(self):
        stock = self._snapshot(-2.0)
        sector = CapitalFlowSnapshot(
            key="电源设备",
            name="电源设备",
            kind="sector",
            quote_date="2026-10-12",
            as_of=datetime(2026, 10, 12, 10, 0),
            source="test",
            scope="行业板块",
            session="intraday",
            net_yi=-10.0,
        )

        self.assertEqual(
            "risk_confirmed",
            flow_confirmation_state(
                "跌破支撑", stock, sector,
                stock_confirmed=True, sector_confirmed=True,
            ),
        )
        self.assertIsNone(
            flow_confirmation_state(
                "跌破支撑", stock, sector,
                stock_confirmed=False, sector_confirmed=True,
            )
        )

    def test_breakout_requires_confirmed_double_inflow(self):
        stock = self._snapshot(2.0)
        sector = CapitalFlowSnapshot(
            key="电源设备",
            name="电源设备",
            kind="sector",
            quote_date="2026-10-12",
            as_of=datetime(2026, 10, 12, 10, 0),
            source="test",
            scope="行业板块",
            session="intraday",
            net_yi=10.0,
        )

        self.assertEqual(
            "breakout_confirmed",
            flow_confirmation_state(
                "放量突破", stock, sector,
                stock_confirmed=True, sector_confirmed=True,
            ),
        )


if __name__ == "__main__":
    unittest.main()
