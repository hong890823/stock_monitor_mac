#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import unittest
from datetime import datetime
from types import SimpleNamespace

from observation_ui import build_limit_views, build_observation_view


NOW = datetime(2026, 10, 23, 15)


def record(code, strategy, state, **overrides):
    row = {
        "code": code,
        "name": code,
        "strategy": strategy,
        "state": state,
        "status": state.lower(),
        "entry_destination": strategy + " / 测试",
        "score": 1,
        "sources": ["strict"],
        "history": [],
        "data_status": "已复核",
        "flow_window": [{"date": "2026-10-23", "tone": "NEUTRAL"}],
        "risk_line": "MA20",
        "structure": "回踩站稳",
        "reason": "测试证据",
    }
    row.update(overrides)
    return row


class CoreMigrationUiTests(unittest.TestCase):
    def test_groups_by_strategy_and_excludes_non_entry_states(self):
        pool = SimpleNamespace(
            load_error=None,
            records={
                "a": record("a", "PRE_BREAKOUT", "NEAR_ENTRY"),
                "b": record("b", "LIMIT_UP_CONTINUATION", "OBSERVING"),
                "c": record("c", "LEADER", "ELIMINATED"),
            },
        )
        view = build_observation_view(pool, NOW)
        text = "\n".join(view["focus_lines"])
        self.assertIn("启动前潜力", text)
        self.assertIn("涨停持续观察", text)
        self.assertNotIn("强势龙头", text)
        self.assertEqual(view["groups"]["PRE_BREAKOUT / 启动前潜力"][0]["code"], "a")
        self.assertNotIn("LEADER / 强势龙头", view["groups"])

    def test_page_limit_is_eight(self):
        rows = {
            str(index): record(str(index), "PRE_BREAKOUT", "NEAR_ENTRY")
            for index in range(20)
        }
        view = build_observation_view(
            SimpleNamespace(load_error=None, records=rows), NOW, page=1
        )
        self.assertEqual(len(view["items"]), 8)
        self.assertEqual(view["page"], 1)
        self.assertEqual(view["page_count"], 3)

    def test_items_include_entry_destination_flow_and_risk_fields(self):
        pool = SimpleNamespace(
            load_error=None,
            records={"a": record("a", "PRE_BREAKOUT", "BUY_CANDIDATE")},
        )
        item = build_observation_view(pool, NOW)["items"][0]
        self.assertEqual(item["entry_destination"], "PRE_BREAKOUT / 测试")
        self.assertEqual(item["flow_window"][0]["tone"], "NEUTRAL")
        self.assertEqual(item["risk_line"], "MA20")
        self.assertEqual(item["structure_evidence"], "回踩站稳")

    def test_limit_views_rank_and_keep_lists_separate(self):
        views = build_limit_views(
            up_rows=[
                {"code": "a", "name": "甲", "change_pct": 10, "seal_quality_score": 20},
                {"code": "b", "name": "乙", "change_pct": 10, "seal_quality_score": 80},
            ],
            down_rows=[
                {"code": "c", "name": "丙", "change_pct": -8},
                {"code": "d", "name": "丁", "change_pct": -10},
            ],
        )
        self.assertEqual([item["code"] for item in views["limit_up"]], ["b", "a"])
        self.assertEqual([item["code"] for item in views["limit_down"]], ["d", "c"])
        self.assertNotIn("seal_quality_score", views["limit_down"][0])


if __name__ == "__main__":
    unittest.main()
