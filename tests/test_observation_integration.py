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
from tests.test_observation_pool import _bars, _hit


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
            self.assertEqual(record["status"], "ready")
            self.assertEqual(record["last_reviewed_date"], "2026-10-14")
            self.assertIn("日线候选", (Path(directory) / "_screen_observation.txt").read_text())

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
