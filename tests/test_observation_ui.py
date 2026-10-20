from copy import deepcopy
from datetime import datetime
from types import SimpleNamespace
import unittest

from observation_ui import build_observation_view, decorate_stock, prioritize_stocks, indicator_color


NOW = datetime(2026, 10, 14, 11)


def record(code="sh600001", status="observing", alert=False):
    item = dict(code=code, name=f"测试{code[-2:]}", status=status, data_status="已复核",
                first_observed_date="2026-10-12", last_reviewed_date="2026-10-13",
                observation_days=2, price=10, stop_price=9, sources=["strict"], score=4,
                reason="测试复核", invalid_rule="跌破失效价", history=[])
    if alert:
        item["last_entry_alert"] = dict(code=code, date="2026-10-14", at="2026-10-14T10:02:00",
                                         first_observed_date="2026-10-12", price=10.15, stop_price=9)
    return item


def view(*records, at=NOW):
    return build_observation_view(SimpleNamespace(records={item["code"]: item for item in records}, load_error=None), at)


class ObservationUiTests(unittest.TestCase):
    def test_buy_point_is_first_and_distinct_from_ready_candidate(self):
        result = view(record(alert=True), record("sh600002", "ready"), record("sh600003"))
        self.assertEqual(result["buy_count"], 1)
        self.assertEqual(result["ready_count"], 1)
        self.assertEqual(result["focus_lines"][0], "📋 今日提醒记录 · 1只（非当前买点）")
        text = "\n".join(result["focus_lines"])
        self.assertIn("10:02触发10.15", text)
        self.assertIn("待盘中确认", text)
        self.assertIn("并非当前仍可买入", text)
        self.assertTrue(result["badges"]["sh600001"].startswith("📋"))
        self.assertTrue(result["badges"]["sh600002"].startswith("🟡"))
        self.assertTrue(result["badges"]["sh600003"].startswith("⚪"))

    def test_yesterdays_alert_is_not_a_today_buy_point(self):
        result = view(record(status="ready", alert=True), at=datetime(2026, 10, 15, 10))
        self.assertEqual(result["buy_count"], 0)
        self.assertTrue(result["badges"]["sh600001"].startswith("🟡"))

    def test_exited_or_pending_stocks_are_not_highlighted_as_buy_points(self):
        for status in ("eliminated", "expired", "ready"):
            item = record(status=status, alert=True)
            if status == "ready":
                item["data_status"] = "待复核：日线缺失"
            result = view(item)
            self.assertEqual(result["buy_count"], 0)
            self.assertEqual(result["ready_count"], 0)
            self.assertTrue(result["badges"][item["code"]].startswith("⚠️"))
            self.assertIn("今日曾触发", "\n".join(result["focus_lines"]))

    def test_future_time_wrong_round_or_invalid_price_cannot_create_badge(self):
        for change in ({"at": "2026-10-14T12:00:00"}, {"first_observed_date": "2026-10-01"},
                       {"price": float("nan")}, {"code": "sh600002"}, {"at": "broken"}):
            item = record(alert=True)
            item["last_entry_alert"].update(change)
            self.assertEqual(view(item)["buy_count"], 0)

    def test_buy_stocks_are_sorted_before_truncation_without_mutating_screen_results(self):
        rows = [{"code": f"sh{600000 + index}", "name": str(index)} for index in range(40)]
        before = deepcopy(rows)
        result = view(record("sh600039", alert=True), record("sh600037", "ready"))
        ordered = prioritize_stocks(rows, result)
        self.assertEqual([row["code"] for row in ordered[:2]], ["sh600039", "sh600037"])
        self.assertEqual(rows, before)
        self.assertEqual([row["code"] for row in ordered[2:]], [row["code"] for row in rows if row["code"] not in {"sh600039", "sh600037"}])

    def test_focus_includes_pool_buy_points_even_if_not_in_current_screen(self):
        result = view(record("sh600099", alert=True))
        self.assertIn("sh600099", "\n".join(result["focus_lines"]))

    def test_badge_color_takes_precedence_over_indicator_keywords(self):
        result = view(record(alert=True), record("sh600002", "ready"))
        title = decorate_stock("测试 | RSI 20 KDJ金叉", {"code": "sh600001"}, result)
        self.assertEqual(indicator_color(title), "gray")
        self.assertEqual(indicator_color(result["badges"]["sh600002"]), "blue")
        self.assertEqual(indicator_color("⚪【观察中】测试 | 10.00"), "gray")

    def test_missing_or_corrupt_pool_is_explicit_and_never_highlights(self):
        self.assertEqual(build_observation_view(None, NOW)["focus_lines"], [])
        pool = SimpleNamespace(records={"sh600001": record(alert=True)}, load_error="文件损坏")
        result = build_observation_view(pool, NOW)
        self.assertEqual(result["buy_count"], 0)
        self.assertIn("文件损坏", "\n".join(result["focus_lines"]))

    def test_today_buy_points_are_not_hidden_by_candidate_limit(self):
        result = view(*(record(f"sh{600000 + index}", alert=True) for index in range(35)))
        self.assertEqual(result["buy_count"], 35)
        self.assertEqual(sum("触发10.15" in text for text in result["focus_lines"]), 35)


if __name__ == "__main__":
    unittest.main()
