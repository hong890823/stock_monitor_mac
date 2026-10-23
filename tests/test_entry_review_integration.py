from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from entry_review_service import EntryReviewService
from entry_review_store import EntryReviewStore
from stock_menubar import StockMenuBar
from tests import test_observation_alert_integration as alert_tests
from tests.test_menu_format import _FakeMenuItem, _FakeRumps


class EntryReviewIntegrationTests(unittest.TestCase):
    setUp = alert_tests.ObservationAlertIntegrationTests.setUp
    run_round = alert_tests.ObservationAlertIntegrationTests.run_round

    def attach(self):
        service = EntryReviewService(self.directory.name, Mock(side_effect=AssertionError("no network")), autostart=False)
        self.app._entry_review = service
        self.addCleanup(service.close)
        return service

    def test_success_records_structured_evidence_and_preserves_existing_notification(self):
        service = self.attach()
        for minute in (0, 1, 2, 3):
            self.run_round(minute)
        service.process_pending()
        self.assertEqual(len(self.notifications), 1)
        self.assertEqual(len(self.banners), 1)
        with EntryReviewStore(service.db_path) as store:
            rows = store.events()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["delivery"], "sent")
        event = rows[0]["event"]
        self.assertEqual(event["sources"], self.app.observation_pool.records["sh600001"]["sources"])
        self.assertEqual(event["evidence"]["observation_days"], 2)
        self.assertEqual(len(event["evidence"]["samples"]), 3)
        self.assertEqual(event["evidence"]["benchmark_price"], 11.3)
        self.assertEqual(event["evidence"]["flow_source"], "fixture")
        self.assertGreater(event["evidence"]["ma20"], 0)
        self.assertEqual(event["policy"]["version"], "daily-paper-v1")

    def test_notification_failure_keeps_signal_and_retry_does_not_duplicate(self):
        service = self.attach()
        self.run_round(0)
        self.run_round(1)
        self.run_round(2, sent=False)
        service.process_pending()
        with EntryReviewStore(service.db_path) as store:
            self.assertEqual(store.events()[0]["delivery"], "failed")
        self.run_round(3)
        service.process_pending()
        with EntryReviewStore(service.db_path) as store:
            self.assertEqual(len(store.events()), 1)
            self.assertEqual(store.events()[0]["delivery"], "sent")

    def test_addon_error_does_not_change_alert_success_or_retry_state(self):
        service = SimpleNamespace(record=Mock(side_effect=OSError("review unavailable")))
        self.app._entry_review = service
        for minute in (0, 1, 2, 3):
            self.run_round(minute)
        self.assertEqual(len(self.notifications), 1)
        self.assertEqual(len(self.banners), 1)
        self.assertIsNone(self.app._observation_entry_last_error)
        self.assertIn("review unavailable", self.app._entry_review_error)

    def test_delivery_error_cannot_prevent_receipt_or_banner(self):
        self.app._entry_review = SimpleNamespace(record=lambda event: True,
                                                 delivery=Mock(side_effect=OSError("delivery storage")))
        for minute in (0, 1, 2):
            self.run_round(minute)
        self.assertIn("last_entry_alert", self.app.observation_pool.records["sh600001"])
        self.assertEqual(len(self.banners), 1)
        self.assertIsNone(self.app._observation_entry_last_error)

    def test_receipt_write_failure_does_not_erase_review_event(self):
        service = self.attach()
        self.run_round(0)
        self.run_round(1)
        with patch.object(self.app.observation_pool, "_save", side_effect=OSError("pool disk full")):
            self.run_round(2)
        service.process_pending()
        with EntryReviewStore(service.db_path) as store:
            self.assertEqual(store.events()[0]["delivery"], "sent")

    def test_disabled_addon_leaves_notifications_and_pool_identical(self):
        service = self.attach()
        service.set_enabled(False)
        for minute in (0, 1, 2):
            self.run_round(minute)
        self.assertEqual(len(self.notifications), 1)
        self.assertEqual(len(self.banners), 1)
        self.assertFalse(service.db_path.exists())

    def test_initialization_failure_is_optional(self):
        with patch("stock_menubar.EntryReviewService", side_effect=OSError("bad db")):
            self.app._init_entry_review()
        self.assertIsNone(self.app._entry_review)
        self.assertIn("bad db", self.app._entry_review_error)
        for minute in (0, 1, 2):
            self.run_round(minute)
        self.assertEqual(len(self.notifications), 1)

    def test_missing_optional_module_does_not_prevent_initialization(self):
        with patch("stock_menubar.EntryReviewService", None):
            self.app._init_entry_review()
        self.assertIsNone(self.app._entry_review)
        self.assertTrue(self.app._entry_review_error)

    def test_menu_controls_are_clickable_and_pause_preserves_original_actions(self):
        service = self.attach()
        self.app._menu = {"📈 选股": _FakeMenuItem("📈 选股")}
        self.app._observation_last_error = None
        self.app.indicator_menu_titles = lambda: ["原有选股结果"]
        with patch("stock_menubar.rumps", _FakeRumps):
            self.app.update_selection_menu()
            parent = self.app.menu["📈 选股"]
            actions = {child.title: child.callback for child in parent.children if isinstance(child, _FakeMenuItem)}
            self.assertTrue(callable(actions["📒 查看买点复盘报告"]))
            actions["⏸ 暂停买点复盘（选股提醒继续）"](None)
            self.assertFalse(service.enabled)
            self.assertEqual(parent.children[-1].title, "原有选股结果")
            self.assertTrue(any(
                getattr(child, "title", "") == "▶ 恢复买点复盘"
                for child in parent.children
            ))


if __name__ == "__main__":
    unittest.main()
