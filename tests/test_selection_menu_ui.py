from datetime import datetime
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from observation_pool import ObservationPool
from stock_menubar import StockMenuBar
from tests.test_menu_format import _FakeMenuItem, _FakeRumps


NOW = datetime(2026, 10, 23, 15)


def record(code, name, state, **overrides):
    value = {
        "code": code,
        "name": name,
        "status": "ready" if state == "BUY_CANDIDATE" else "observing",
        "data_status": "已复核",
        "state": state,
        "price": 10.0,
        "observation_days": 2,
        "confirmation_rounds": 1,
        "last_reviewed_date": "2026-10-23",
    }
    value.update(overrides)
    return value


class SelectionMenuUiTests(unittest.TestCase):
    def setUp(self):
        self.app = StockMenuBar.__new__(StockMenuBar)
        self.app.observation_pool = ObservationPool()
        self.app.observation_pool.records = {
            "sh600003": record("sh600003", "已晋级", "BUY_CANDIDATE"),
            "sh600002": record("sh600002", "待确认", "NEAR_ENTRY"),
            "sh600001": record("sh600001", "持续观察", "OBSERVING"),
            "sh600004": record(
                "sh600004",
                "今日历史",
                "OBSERVING",
                last_entry_alert={
                    "date": "2026-10-23",
                    "at": "2026-10-23T10:32:00",
                    "price": 10.0,
                },
            ),
        }
        self.app.indicator_data = []
        self.app.relaxed_indicator_data = []
        self.app.five_one_three_data = []
        self.app.five_one_three_stats = {}
        self.app.indicator_results_stale = False
        self.app.indicator_results_stale_at = None
        self.app._last_intraday_indicator_at = None
        self.app._observation_last_error = None
        self.app._entry_review_error = None
        self.app.indicator_refresh_status = lambda: "测试"
        self.app.indicator_refresh_scheduler = SimpleNamespace(
            last_success_at=lambda: None,
            last_error=lambda: None,
        )
        self.app._menu = {"📈 选股": _FakeMenuItem("📈 选股")}

    def test_native_menu_flattens_current_states_and_historical_alerts(self):
        with patch("stock_menubar.datetime") as clock, patch("stock_menubar.rumps", _FakeRumps):
            clock.now.return_value = NOW
            self.app.update_selection_menu()

        children = self.app.menu["📈 选股"].children
        item_titles = [
            child.title for child in children if isinstance(child, _FakeMenuItem)
        ]
        self.assertTrue(item_titles[0].startswith("🎯 已晋级 / 买点已确认"))
        self.assertLess(
            item_titles.index("🟡 日线候选 / 等待确认 · 1只"),
            item_titles.index("📋 今日提醒记录 · 1只（历史记录，非当前买点）"),
        )
        self.assertIn("⚪ 持续观察 · 2只", item_titles)
        self.assertTrue(any(title.startswith("🎯 已晋级") and "已晋级" in title for title in item_titles[1:]))
        self.assertNotIn("🔎 持续观察池", item_titles)

    def test_review_and_report_actions_are_direct_menu_items(self):
        with patch("stock_menubar.datetime") as clock, patch("stock_menubar.rumps", _FakeRumps):
            clock.now.return_value = NOW
            self.app.update_selection_menu()

        item_titles = [
            child.title for child in self.app.menu["📈 选股"].children
            if isinstance(child, _FakeMenuItem)
        ]
        self.assertIn("🔄 立即复核观察池", item_titles)
        self.assertIn("📋 查看完整观察报告", item_titles)
        self.assertTrue(any(title.startswith("🔄 重扫选股") for title in item_titles))


if __name__ == "__main__":
    unittest.main()
