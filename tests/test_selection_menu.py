#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import unittest
from datetime import datetime

from selection_menu import build_selection_sections


NOW = datetime(2026, 10, 23, 15)


def record(code, state, **overrides):
    value = {
        "code": code,
        "name": f"股票{code[-1]}",
        "status": "ready" if state == "BUY_CANDIDATE" else "observing",
        "data_status": "已复核",
        "state": state,
        "price": 10.0 + int(code[-1]),
        "strategy": "PRE_BREAKOUT",
        "first_observed_date": "2026-10-20",
        "observation_days": 2,
        "confirmation_rounds": 1,
        "last_reviewed_date": "2026-10-23",
    }
    value.update(overrides)
    return value


class SelectionMenuTests(unittest.TestCase):
    def test_sections_order_current_promotion_above_historical_alerts(self):
        records = {
            "sh600001": record("sh600001", "OBSERVING"),
            "sh600002": record("sh600002", "NEAR_ENTRY", confirmation_rounds=2),
            "sh600003": record(
                "sh600003", "BUY_CANDIDATE",
                entry_confirmed_at="2026-10-23T10:30:00",
            ),
            "sh600004": record(
                "sh600004", "OBSERVING",
                last_entry_alert={"date": "2026-10-23", "at": "2026-10-23T10:32:00", "price": 11.0},
            ),
        }
        sections = build_selection_sections(records, NOW, per_group=8)
        self.assertEqual(sections.promoted_count, 1)
        self.assertEqual(sections.near_entry_count, 1)
        self.assertEqual(sections.observing_count, 2)
        self.assertEqual(sections.today_alert_count, 1)
        self.assertIn("🎯 股票3", sections.promoted[0])
        self.assertIn("🟡 股票2", sections.near_entry[0])
        self.assertIn("⚪ 股票1", sections.observing[0])
        self.assertIn("📋 股票4", sections.today_alerts[0])

    def test_each_visible_group_is_limited_without_hiding_counts(self):
        records = {
            f"sh60000{index}": record(f"sh60000{index}", "NEAR_ENTRY")
            for index in range(1, 5)
        }
        sections = build_selection_sections(records, NOW, per_group=2)
        self.assertEqual(len(sections.near_entry), 2)
        self.assertEqual(sections.near_entry_count, 4)

    def test_invalid_and_eliminated_records_are_not_listed(self):
        records = {
            "sh600001": record("sh600001", "ELIMINATED", status="eliminated"),
            "sh600002": record("sh600002", "OBSERVING", data_status="待复核"),
            "sh600003": record("sh600003", "OBSERVING", status="expired"),
        }
        sections = build_selection_sections(records, NOW, per_group=8)
        self.assertEqual(sections.promoted + sections.near_entry + sections.observing, [])


if __name__ == "__main__":
    unittest.main()
