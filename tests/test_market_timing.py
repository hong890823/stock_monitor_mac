#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""市场时点与资金证据格式测试。"""

import unittest

from market_timing import format_market_timing_data


class MarketTimingTests(unittest.TestCase):
    def test_formats_calendar_window_and_market_evidence_for_ai(self):
        text = format_market_timing_data(
            date_value="2026-10-30",
            index_today_pct=0.80,
            index_recent_pct=1.50,
            turnover_today=19000,
            turnover_previous=15000,
            turnover_history=[
                ("2026-10-26", 12000),
                ("2026-10-27", 13000),
                ("2026-10-28", 14000),
                ("2026-10-29", 15000),
                ("2026-10-30", 19000),
            ],
            sector_breadth=0.35,
            northbound_net=-55.0,
            holiday_eve=True,
            month_end_near=True,
            sector_breadth_label="持仓相关板块上涨占比",
        )

        self.assertIn("周五", text)
        self.assertIn("月末附近", text)
        self.assertIn("节假日前", text)
        self.assertIn("北向净流向：-55.0亿", text)
        self.assertIn("成交额：19000亿", text)
        self.assertIn("较前一交易日 +26.7%", text)
        self.assertIn("持仓相关板块上涨占比：35%", text)
        self.assertIn("不单独构成卖出信号", text)

    def test_missing_flow_data_is_explicit(self):
        text = format_market_timing_data(
            date_value="2026-10-12",
            index_today_pct=-0.40,
            index_recent_pct=-2.10,
        )

        self.assertIn("北向净流向：未取到", text)
        self.assertIn("成交额：未取到", text)
        self.assertIn("板块上涨占比：未取到", text)
        self.assertNotIn("节假日前：未确认", text)

    def test_unknown_holiday_calendar_does_not_create_a_window_label(self):
        text = format_market_timing_data(
            date_value="2026-10-12",
            holiday_eve=None,
        )

        self.assertIn("时点标签：无明确窗口标签", text)

    def test_turnover_and_northbound_metadata_are_explicit(self):
        text = format_market_timing_data(
            date_value="2026-10-12",
            turnover_today=8000,
            turnover_previous=7600,
            turnover_comparison_label="较前一交易日同期",
            turnover_session="盘中截至10:00",
            turnover_source="TurnoverTracker",
            turnover_scope="沪深两市",
            northbound_net=-12.0,
            northbound_date="2026-10-12",
            northbound_3d=-20.0,
            northbound_5d=15.0,
            northbound_source="AkShare",
        )

        self.assertIn("较前一交易日同期 +5.3%", text)
        self.assertIn("盘中截至10:00", text)
        self.assertIn("近3日累计 -20.0亿", text)
        self.assertIn("数据口径", text)
        self.assertIn("TurnoverTracker", text)


if __name__ == "__main__":
    unittest.main()
