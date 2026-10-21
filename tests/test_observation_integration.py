from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pandas as pd

from five_one_three import FiveOneThreeStage
from observation_pool import ObservationPool
from stock_menubar import StockMenuBar
from tests.test_observation_pool import _bars, _high_break_bars, _hit


class ObservationIntegrationTests(unittest.TestCase):
    def app(self, directory):
        app = StockMenuBar.__new__(StockMenuBar)
        app.log_dir = directory
        app.observation_pool = ObservationPool(Path(directory) / "pool.json")
        app._observation_refresh_lock = threading.Lock()
        app._observation_next_refresh_at = None
        app._observation_last_error = None
        app.log = lambda *args: None
        return app

    def test_screen_hits_are_normalised_with_strongest_signal(self):
        hits = StockMenuBar._observation_hits(
            strict=[{"code": "sh600001", "name": "甲", "price": 10}],
            relaxed=[{"code": "sh600001", "name": "甲", "price": 10}],
            five=[{
                "code": "sz000001",
                "name": "乙",
                "price": 8,
                "stage": FiveOneThreeStage.CONFIRMED,
            }],
        )

        self.assertEqual(
            {(item["code"], item["source"]) for item in hits},
            {
                ("sh600001", "strict"),
                ("sh600001", "relaxed"),
                ("sz000001", "five_confirmed"),
            },
        )

    def test_strong_hit_keeps_risk_context(self):
        hits = StockMenuBar._observation_hits(
            strict=[], relaxed=[], five=[],
            strong=[{
                "code": "sz000001",
                "name": "强势股",
                "price": 10.0,
                "strong_state": "STRONG_CONTINUATION",
                "risk_line": "MA20",
                "risk_price": 9.6,
                "continuation_state": "HOLD_CONTINUATION",
                "continuation_hold": {"state": "HOLD_CONTINUATION"},
            }],
        )
        self.assertEqual(hits[0]["source"], "strong")
        self.assertEqual(hits[0]["strong_state"], "STRONG_CONTINUATION")
        self.assertEqual(hits[0]["risk_line"], "MA20")
        self.assertEqual(hits[0]["risk_price"], 9.6)
        self.assertEqual(hits[0]["continuation_state"], "HOLD_CONTINUATION")
        self.assertEqual(hits[0]["continuation_hold"]["state"], "HOLD_CONTINUATION")

    def test_daily_strong_candidate_keeps_eligibility_and_risk_line(self):
        candidate = StockMenuBar._strong_candidate_from_bars(
            "sz000001", "强势股", _bars(),
        )
        self.assertEqual(candidate["strong_state"], "STRONG_CONTINUATION")
        self.assertTrue(candidate["eligible_candidate"])
        self.assertEqual(candidate["risk_line"], "MA20")
        self.assertEqual(candidate["continuation_hold"]["state"], "HOLD_CONTINUATION")

    def test_dynamic_break_stock_is_not_returned_as_strong_candidate(self):
        candidate = StockMenuBar._strong_candidate_from_bars(
            "sz000001", "高位股", _high_break_bars(),
        )
        self.assertIsNone(candidate)

    def test_extended_stock_is_observation_only(self):
        bars = _high_break_bars()
        bars[-1].update(open=20.0, high=20.4, low=19.9, close=20.3, volume=120)
        candidate = StockMenuBar._strong_candidate_from_bars(
            "sz000001", "高位股", bars,
        )
        self.assertEqual(candidate["strong_state"], "EXTENDED_CONTINUATION")
        self.assertFalse(candidate["eligible_candidate"])
        self.assertEqual(candidate["continuation_hold"]["state"], "PROFIT_PROTECTION")

    def test_strong_candidates_survive_screen_state_reload(self):
        with TemporaryDirectory() as directory:
            app = self.app(directory)
            app.screen_results_state_file = str(Path(directory) / "screen.json")
            app.indicator_data = []
            app.relaxed_indicator_data = []
            app.strong_candidate_data = []
            app.five_one_three_data = []
            app.five_one_three_stats = {}
            app._indicator_group_at = None
            app._pattern_group_at = None
            app.indicator_results_stale = False
            app.indicator_results_stale_at = None
            strong = [{
                "code": "sh600001",
                "name": "强势股",
                "price": 10.0,
                "strong_state": "STRONG_CONTINUATION",
                "risk_line": "MA20",
                "risk_price": 9.6,
            }]
            app._merge_screen_results(
                strict=[], relaxed=[], strong=strong, five=[], stats={},
                include_patterns=False, completed_at=datetime.now(),
            )

            restored = self.app(directory)
            restored.screen_results_state_file = app.screen_results_state_file
            restored.indicator_data = []
            restored.relaxed_indicator_data = []
            restored.strong_candidate_data = []
            restored.five_one_three_data = []
            restored.five_one_three_stats = {}
            restored._load_screen_results()
            self.assertEqual(restored.strong_candidate_data, strong)

    def test_existing_candidate_is_reviewed_even_without_new_screen_hits(self):
        with TemporaryDirectory() as directory:
            app = self.app(directory)
            with patch.object(app, "get_stock_analysis_history", return_value=pd.DataFrame(_bars("2026-10-13"))), \
                 patch.object(app, "get_stock_data", return_value={"name": "测试股份"}):
                app._refresh_observation_pool([_hit()], datetime(2026, 10, 13, 18))
            with patch.object(app, "get_stock_analysis_history", return_value=pd.DataFrame(_bars())), \
                 patch.object(app, "get_stock_data", return_value={"name": "测试股份"}):
                app._refresh_observation_pool([], datetime(2026, 10, 14, 18))
            record = app.observation_pool.records["sh600001"]
            self.assertEqual(record["status"], "observing")
            self.assertFalse(record["can_enter"])
            self.assertEqual(record["last_reviewed_date"], "2026-10-14")
            self.assertIn("日线候选", (Path(directory) / "_screen_observation.txt").read_text())

    def test_observation_history_requests_include_long_trend_window(self):
        with TemporaryDirectory() as directory:
            app = self.app(directory)
            calls = []

            def history(code, data_length=240):
                calls.append((code, data_length))
                return pd.DataFrame(_bars())

            with patch.object(app, "get_stock_analysis_history", side_effect=history), \
                 patch.object(app, "get_stock_data", return_value={"name": "测试股份"}):
                app._refresh_observation_pool([_hit()], datetime(2026, 10, 14, 18))

            self.assertTrue(calls)
            self.assertEqual({length for _, length in calls}, {400})

    def test_review_failure_retains_candidate_and_schedules_retry(self):
        with TemporaryDirectory() as directory:
            app = self.app(directory)
            with patch.object(app, "get_stock_analysis_history", side_effect=RuntimeError("offline")), \
                 patch.object(app, "get_stock_data", return_value=None):
                app._refresh_observation_pool([_hit()], datetime(2026, 10, 14, 18))
            self.assertEqual(app.observation_pool.records["sh600001"]["observation_days"], 0)
            self.assertIsNotNone(app._observation_last_error)
            self.assertEqual(app._observation_next_refresh_at, datetime(2026, 10, 14, 19, 30))

    def test_same_day_duplicate_hits_do_not_reset_state(self):
        with TemporaryDirectory() as directory:
            app = self.app(directory)
            with patch.object(app, "get_stock_analysis_history", return_value=pd.DataFrame(_bars())), \
                 patch.object(app, "get_stock_data", return_value={"name": "测试股份"}):
                app._refresh_observation_pool([_hit()], datetime(2026, 10, 14, 18))
                app._refresh_observation_pool([_hit("strict")], datetime(2026, 10, 14, 20))
            self.assertEqual(app.observation_pool.records["sh600001"]["observation_days"], 1)
            self.assertEqual(len(app.observation_pool.records["sh600001"]["sources"]), 2)

    def test_st_name_is_retained_even_when_stock_history_request_fails(self):
        with TemporaryDirectory() as directory:
            app = self.app(directory)
            app.observation_pool.review([_hit()], datetime(2026, 10, 13, 18))
            with patch.object(app, "get_stock_analysis_history", side_effect=RuntimeError("offline")), \
                 patch.object(app, "get_stock_data", return_value={"name": "ST测试"}):
                app._refresh_observation_pool([], datetime(2026, 10, 14, 18))
            self.assertEqual(app.observation_pool.records["sh600001"]["status"], "eliminated")

    def test_auto_review_only_runs_after_close_and_respects_next_attempt(self):
        with TemporaryDirectory() as directory:
            app = self.app(directory)
            app.observation_pool.review([_hit()], datetime(2026, 10, 13, 18))

            class InlineThread:
                def __init__(self, target, args=(), **kwargs):
                    self.target, self.args = target, args

                def start(self):
                    self.target(*self.args)

            with patch("stock_menubar.threading", SimpleNamespace(Thread=InlineThread)), \
                 patch.object(app, "get_stock_analysis_history", side_effect=RuntimeError("offline")), \
                 patch.object(app, "get_stock_data", return_value=None):
                app.maybe_refresh_observation_pool(datetime(2026, 10, 14, 14))
                self.assertIsNone(app._observation_next_refresh_at)
                app.maybe_refresh_observation_pool(datetime(2026, 10, 14, 15, 10))
                self.assertEqual(app._observation_next_refresh_at, datetime(2026, 10, 14, 16, 40))
                app.maybe_refresh_observation_pool(datetime(2026, 10, 14, 15, 11))
                self.assertEqual(app._observation_next_refresh_at, datetime(2026, 10, 14, 16, 40))


if __name__ == "__main__":
    unittest.main()
