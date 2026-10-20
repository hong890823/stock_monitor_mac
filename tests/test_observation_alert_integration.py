from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pandas as pd

from observation_alerts import ObservationEntryTracker
from stock_menubar import StockMenuBar
from tests.test_observation_alerts import observed_pool, quote, flow
from tests.test_observation_pool import _bars


class ObservationAlertIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.now = datetime(2026, 10, 14, 10)
        self.app = StockMenuBar.__new__(StockMenuBar)
        self.app.log_dir = self.directory.name
        self.app.observation_pool = observed_pool(Path(self.directory.name) / "pool.json")
        self.app._observation_refresh_lock = threading.Lock()
        self.app._observation_entry_lock = threading.Lock()
        self.app._observation_entry_tracker = ObservationEntryTracker()
        self.app._observation_entry_context_date = "2026-10-14"
        self.app._observation_entry_histories = {"sh600001": _bars("2026-10-13")}
        self.app._observation_entry_benchmark = _bars("2026-10-13")
        self.app._observation_entry_next_at = None
        self.app._observation_entry_last_error = None
        self.app.log = lambda *args: None
        self.app._dispatch_to_main = lambda callback: callback()
        self.notifications = []
        self.banners = []
        self.app.alert_popup = SimpleNamespace(show=lambda *args, **kwargs: self.banners.append(args))

    def run_round(self, minute, sent=True, stock_price=None):
        now = self.now + timedelta(minutes=minute)

        def get_quote(code):
            price = stock_price if stock_price is not None else 11.15 + minute * 0.005
            return quote(now, 11.3 if code == "sh000001" else price)

        def notify(*args, **kwargs):
            self.notifications.append((args, kwargs))
            return sent

        with patch.object(self.app, "get_stock_data", side_effect=get_quote), \
             patch("stock_menubar.fetch_stock_flow_snapshot", return_value=flow(now)), \
             patch.object(self.app, "_notify", side_effect=notify):
            self.app._check_observation_entry_alerts(now)

    def test_limit_stock_stays_observed_and_notifies_only_after_new_confirmation(self):
        with patch('observation_alerts.evaluate', return_value=SimpleNamespace(
                can_buy=True, trend_weak=False, stop_price=10)):
            for minute in (0, 1, 2):
                self.run_round(minute, stock_price=12.41)
        self.assertEqual(self.notifications, [])
        self.assertEqual(self.banners, [])
        record = self.app.observation_pool.records['sh600001']
        self.assertFalse(record.get('last_entry_alert'))
        self.assertIn(record['status'], {'observing', 'ready'})
        for minute in (3, 4):
            self.run_round(minute)
            self.assertEqual(self.notifications, [])
        self.run_round(5)
        self.assertEqual(len(self.notifications), 1)
        self.assertEqual(len(self.banners), 1)
        record = self.app.observation_pool.records['sh600001']
        self.assertEqual(record['last_entry_alert']['at'], '2026-10-14T10:05:00')

    def test_non_portfolio_stock_gets_system_notification_banner_and_saved_receipt(self):
        for minute in (0, 1, 2, 3):
            self.run_round(minute)
        self.assertEqual(len(self.notifications), 1)
        self.assertEqual(len(self.banners), 1)
        record = self.app.observation_pool.records["sh600001"]
        self.assertEqual(record["last_entry_alert"]["date"], "2026-10-14")
        self.assertIn("资金", self.notifications[0][0][2])
        self.assertIn("最近买点提醒", (Path(self.directory.name) / "_screen_observation.txt").read_text())

    def test_failed_delivery_can_retry_and_is_not_marked_as_sent(self):
        self.run_round(0)
        self.run_round(1)
        self.run_round(2, sent=False)
        self.assertNotIn("last_entry_alert", self.app.observation_pool.records["sh600001"])
        self.assertEqual(len(self.banners), 0)
        self.run_round(3)
        self.assertEqual(len(self.notifications), 2)
        self.assertIn("last_entry_alert", self.app.observation_pool.records["sh600001"])

    def test_closed_session_does_not_fetch_or_send_and_resets_streak(self):
        with patch.object(self.app, "get_stock_data", side_effect=AssertionError("must not fetch after close")):
            self.app._check_observation_entry_alerts(datetime(2026, 10, 14, 16))
        self.assertEqual(self.notifications, [])

    def test_persisted_daily_receipt_survives_background_review(self):
        for minute in (0, 1, 2):
            self.run_round(minute)
        pool = self.app.observation_pool
        pool.review([], datetime(2026, 10, 14, 18),
                    histories={"sh600001": _bars()}, benchmark=_bars())
        self.assertEqual(pool.records["sh600001"]["last_entry_alert"]["date"], "2026-10-14")

    def test_startup_refreshes_daily_background_before_intraday_confirmation(self):
        self.app._observation_entry_context_date = None
        with patch.object(self.app, "get_stock_analysis_history", return_value=pd.DataFrame(_bars("2026-10-13"))):
            for minute in (0, 1, 2):
                self.run_round(minute)
        self.assertEqual(self.app._observation_entry_context_date, "2026-10-14")
        self.assertEqual(len(self.notifications), 1)
        self.assertEqual(self.app.observation_pool.records["sh600001"]["observation_days"], 2)

    def test_transient_missing_daily_data_recovers_without_waiting_until_close(self):
        self.app.observation_pool.records["sh600001"]["data_status"] = "待复核：日线请求失败"
        with patch.object(self.app, "get_stock_analysis_history", return_value=pd.DataFrame(_bars("2026-10-13"))):
            for minute in (0, 1, 2):
                self.run_round(minute)
        self.assertEqual(len(self.notifications), 1)

    def test_buy_alert_does_not_replace_an_existing_risk_banner(self):
        self.app.alert_popup.visible = True
        for minute in (0, 1, 2):
            self.run_round(minute)
        self.assertEqual(len(self.notifications), 1)
        self.assertEqual(len(self.banners), 0)

    def test_scheduler_throttles_checks_and_does_not_run_during_lunch(self):
        class InlineThread:
            def __init__(self, target, **kwargs):
                self.target = target

            def start(self):
                self.target()

        checks = []
        with patch("stock_menubar.threading", SimpleNamespace(Thread=InlineThread)), \
             patch.object(self.app, "_check_observation_entry_alerts", side_effect=lambda: checks.append(True)):
            self.app._maybe_check_observation_entries(self.now)
            self.app._maybe_check_observation_entries(self.now + timedelta(seconds=15))
            self.app._maybe_check_observation_entries(self.now + timedelta(seconds=60))
            self.app._maybe_check_observation_entries(self.now.replace(hour=12))
        self.assertEqual(len(checks), 2)

    def test_persist_failure_is_visible_without_repeating_successful_delivery(self):
        self.run_round(0)
        self.run_round(1)
        with patch.object(self.app.observation_pool, "_save", side_effect=OSError("disk full")):
            self.run_round(2)
        self.assertIn("disk full", self.app._observation_entry_last_error)
        self.run_round(3)
        self.assertEqual(len(self.notifications), 1)
