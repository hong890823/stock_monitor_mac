#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""基本面健康分级测试：亮红灯触发、周期封顶、倾向文案、缓存保质。"""

import unittest

from fundamental_health import (
    RETRY_AFTER_FAILURE_MS,
    FundamentalHealth,
    clear_cache,
    grade,
    mark_attempt,
    needs_fetch,
    publish,
    tendency,
)


class _Risk:
    def __init__(self, level, text):
        self.level = level
        self.text = text


class _Quarter:
    def __init__(self, revenue_yoy=None, profit_yoy=None):
        self.revenue_yoy = revenue_yoy
        self.profit_yoy = profit_yoy


class TestGrade(unittest.TestCase):
    def test_weak_score_is_red(self):
        health = grade(45, "较差")
        self.assertEqual(health.level, "ALERT")

    def test_alert_risk_is_red_even_with_good_score(self):
        health = grade(80, "良好", risks=[_Risk("ALERT", "公告提示：立案调查")])
        self.assertEqual(health.level, "ALERT")
        self.assertTrue(any("立案" in reason for reason in health.reasons))

    def test_two_quarters_double_decline_is_red(self):
        health = grade(
            75, "良好",
            quarters=[
                _Quarter(-5.0, -3.0),
                _Quarter(-25.0, -30.0),
                _Quarter(-22.0, -40.0),
            ],
        )
        self.assertEqual(health.level, "ALERT")

    def test_single_quarter_double_decline_is_neutral(self):
        health = grade(
            75, "良好",
            quarters=[_Quarter(10.0, 8.0), _Quarter(-25.0, -30.0)],
        )
        self.assertEqual(health.level, "NEUTRAL")

    def test_missing_yoy_does_not_count_as_downturn(self):
        health = grade(
            75, "良好",
            quarters=[_Quarter(None, None), _Quarter(None, None)],
        )
        self.assertEqual(health.level, "SOUND")

    def test_cyclical_note_caps_at_sound(self):
        health = grade(76, "良好", notes=["周期股在盈利高点的低 PE 是陷阱"])
        self.assertEqual(health.level, "NEUTRAL")

    def test_forecast_downgrade_is_red(self):
        health = grade(76, "良好", risks=[_Risk("ALERT", "公告提示：业绩预亏")])
        self.assertEqual(health.level, "ALERT")


class TestTendency(unittest.TestCase):
    def test_deep_labels_script_priority(self):
        red = grade(40, "较差")
        self.assertIn("亮红灯", tendency(red, deep=True))
        self.assertIn("剧本①", tendency(red, deep=True))
        sound = grade(76, "良好")
        self.assertIn("未见恶化", tendency(sound, deep=True))
        self.assertIn("剧本②", tendency(sound, deep=True))

    def test_shallow_mentions_fundamentals_even_without_data(self):
        self.assertIn("未分析", tendency(None, deep=False))
        self.assertIn("暂无数据", tendency(None, deep=True))

    def test_deep_missing_fundamentals_gives_actionable_guidance(self):
        result = tendency(None, deep=True)

        self.assertIn("风险承受能力", result)
        self.assertIn("等待企稳", result)
        self.assertIn("补充基本面", result)
        self.assertNotIn("自行取舍", result)

    def test_deep_neutral_fundamentals_gives_actionable_guidance(self):
        result = tendency(grade(60, "一般"), deep=True)

        self.assertIn("风险承受能力", result)
        self.assertIn("等待企稳", result)
        self.assertIn("程序不替你做最终决定", result)
        self.assertNotIn("自行取舍", result)


class TestCache(unittest.TestCase):
    def setUp(self):
        clear_cache()

    def tearDown(self):
        clear_cache()

    def test_fresh_cache_is_used_without_fetching(self):
        health = FundamentalHealth("SOUND", 76, "良好", ["评分 76（良好）"], "2026-10-09 10:00")
        day_ms = 24 * 60 * 60 * 1000
        publish("sh600522", health, fetched_at_ms=1_000_000)
        self.assertFalse(needs_fetch("sh600522", now_ms=1_000_000 + 13 * day_ms))
        self.assertTrue(needs_fetch("sh600522", now_ms=1_000_000 + 15 * day_ms))

    def test_failed_fetch_retries_with_throttle(self):
        self.assertTrue(needs_fetch("sz000001", now_ms=RETRY_AFTER_FAILURE_MS + 1))
        mark_attempt("sz000001", now_ms=1_000_000)
        self.assertFalse(needs_fetch("sz000001", now_ms=1_000_000 + RETRY_AFTER_FAILURE_MS - 1))
        self.assertTrue(needs_fetch("sz000001", now_ms=1_000_000 + RETRY_AFTER_FAILURE_MS))


if __name__ == "__main__":
    unittest.main()
