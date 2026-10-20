from datetime import datetime
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from observation_pool import ObservationPool
from stock_menubar import StockMenuBar
from tests.test_menu_format import _FakeMenuItem, _FakeRumps
from tests.test_observation_ui import NOW, record


class ObservationUiIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.app = StockMenuBar.__new__(StockMenuBar)
        self.app.observation_pool = ObservationPool()
        self.app.observation_pool.records = {"sh600099": record("sh600099", alert=True),
                                            "sh600098": record("sh600098", "ready")}
        self.app.indicator_data = []
        self.app.relaxed_indicator_data = []
        self.app.five_one_three_data = []
        self.app.five_one_three_stats = {}
        self.app.indicator_results_stale = False
        self.app.indicator_results_stale_at = None
        self.app._last_intraday_indicator_at = None
        self.app._observation_last_error = None
        self.app.indicator_refresh_status = lambda: "测试"
        self.app.indicator_refresh_scheduler = SimpleNamespace(last_success_at=lambda: None, last_error=lambda: None)
        self.app._menu = {"📈 指标策略": _FakeMenuItem("📈 指标策略")}

    def titles(self):
        with patch("stock_menubar.datetime") as clock:
            clock.now.return_value = NOW
            return self.app.indicator_menu_titles()

    def test_buy_focus_is_first_even_without_a_current_screen_hit(self):
        titles = self.titles()
        self.assertEqual(titles[0], "📋 今日提醒记录 · 1只（非当前买点）")
        self.assertIn("sh600099", titles[1])

    def test_buy_point_below_screen_limit_is_promoted_and_decorated(self):
        self.app.indicator_data = [dict(code=f"sh{600000 + index}", name=f"普通{index}", price=10, rsi=20, k=20, d=15) for index in range(35)]
        self.app.indicator_data.append(dict(code="sh600099", name="目标", price=10, rsi=20, k=20, d=15))
        titles = self.titles()
        rows = [title for title in titles if "RSI 20 |" in title]
        self.assertTrue(rows[0].startswith("📋【今日曾提醒"))
        self.assertIn("目标", rows[0])
        self.assertEqual(self.app.indicator_data[-1]["name"], "目标")

    def test_all_three_strategy_lists_share_badges_and_focus_deduplicates(self):
        common = dict(code="sh600099", name="目标", price=10)
        self.app.indicator_data = [dict(common, rsi=20, k=20, d=15)]
        self.app.relaxed_indicator_data = [dict(common, relaxed_score=2, rsi_ok=True)]
        self.app.five_one_three_data = [dict(common, progress="形态已确认", detail="测试形态", gain60=2)]
        titles = self.titles()
        self.assertEqual(titles[0], "📋 今日提醒记录 · 1只（非当前买点）")
        self.assertEqual(sum(title.startswith("📋【今日曾提醒") for title in titles), 3)

    def test_candidate_row_never_uses_confirmed_buy_badge(self):
        self.app.indicator_data = [dict(code="sh600098", name="候选", price=10, rsi=20, k=20, d=15)]
        title = next(title for title in self.titles() if "RSI 20 |" in title)
        self.assertTrue(title.startswith("🟡【日线候选·等待回踩确认】"))

    def test_native_menu_has_buy_points_above_tools_and_pool(self):
        with patch("stock_menubar.datetime") as clock, patch("stock_menubar.rumps", _FakeRumps):
            clock.now.return_value = NOW
            self.app.update_indicator_menu()
        children = self.app.menu["📈 指标策略"].children
        self.assertEqual(children[0].title, "📋 今日提醒记录 · 1只（非当前买点）")
        self.assertTrue(any(child.title == "🔄 重扫指标（前200，秒级）" for child in children if isinstance(child, _FakeMenuItem)))
        self.assertEqual(sum(child.title == "📋 今日提醒记录 · 1只（非当前买点）" for child in children if isinstance(child, _FakeMenuItem)), 1)

    def test_floating_window_has_compact_count_and_highlighted_buy_rows(self):
        self.app.floating_window = Mock()
        self.app._market_turnover_lines = lambda: []
        self.app._update_floating_window({"indicator": {"items": self.titles(), "buy_count": 1,
                                                       "title": "📈 指标策略 · 📋 今日曾提醒1"}})
        text = self.app.floating_window.set_compact.call_args.args[0]
        self.assertIn("📋 今日曾提醒1只", text)
        menu = self.app.floating_window.set_menu_data.call_args.args[0]
        self.assertEqual(menu["indicator"]["items"][1]["color"], "gray")
        self.assertIn("今日曾提醒1", menu["indicator"]["title"])

    def test_pool_menu_uses_same_labels_and_removes_today_badge_next_day(self):
        today = self.app.observation_pool.menu_lines(at=NOW)
        tomorrow = self.app.observation_pool.menu_lines(at=datetime(2026, 10, 15, 11))
        self.assertEqual(today[0], "📋 今日提醒记录 · 1只（非当前买点）")
        self.assertEqual(tomorrow[0], "📋 今日提醒记录 · 0只（非当前买点）")


if __name__ == "__main__":
    unittest.main()
