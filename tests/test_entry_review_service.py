from datetime import datetime
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import Mock, patch

from entry_review_service import EntryReviewService
from entry_review_store import EntryReviewStore
from tests.test_entry_review import sample_event, histories


class EntryReviewServiceTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.fetch = Mock(side_effect=lambda code, data_length=240: histories()[1 if code == "sh000001" else 0])
        self.service = EntryReviewService(self.directory.name, self.fetch, autostart=False,
                                          clock=lambda: datetime(2026, 11, 2, 18))
        self.addCleanup(self.service.close)

    def event(self):
        return sample_event()

    def test_capture_and_delivery_only_enqueue_without_io(self):
        with patch("entry_review_service.EntryReviewStore", side_effect=AssertionError("DB on alert thread")):
            self.assertTrue(self.service.record(self.event()))
            self.assertTrue(self.service.delivery(self.event(), False))
        self.fetch.assert_not_called()
        self.service.process_pending()
        with EntryReviewStore(self.service.db_path) as store:
            self.assertEqual(store.events()[0]["delivery"], "failed")

    def test_queue_full_is_visible_and_does_not_raise(self):
        service = EntryReviewService(self.directory.name, self.fetch, autostart=False, queue_size=1)
        self.addCleanup(service.close)
        self.assertTrue(service.record(self.event()))
        self.assertFalse(service.record(self.event()))
        self.assertIn("队列", service.status)
        self.assertEqual(service.dropped, 1)

    def test_db_failure_retries_same_event_without_losing_delivery(self):
        self.service.record(self.event())
        self.service.delivery(self.event(), True)
        with patch("entry_review_service.EntryReviewStore", side_effect=OSError("disk full")):
            self.service.process_pending()
        self.assertIn("disk full", self.service.status)
        self.service.process_pending()
        with EntryReviewStore(self.service.db_path) as store:
            self.assertEqual(len(store.events()), 1)
            self.assertEqual(store.events()[0]["delivery"], "sent")

    def test_capture_snapshot_is_immutable_and_does_not_change_input(self):
        event = self.event()
        self.service.record(event)
        event["evidence"]["observation_days"] = 99
        self.service.process_pending()
        with EntryReviewStore(self.service.db_path) as store:
            self.assertEqual(store.events()[0]["event"]["evidence"]["observation_days"], 2)

    def test_intraday_automatic_tick_only_persists_events(self):
        self.service.record(self.event())
        self.service.tick(datetime(2026, 10, 15, 10))
        self.fetch.assert_not_called()
        self.assertTrue(self.service.report_path.exists())

    def test_after_close_reviews_without_observation_pool_or_portfolio(self):
        self.service.record(self.event())
        self.service.tick(datetime(2026, 11, 2, 18))
        self.assertEqual(self.fetch.call_count, 2)
        self.service.tick(datetime(2026, 11, 2, 18, 1))
        self.assertEqual(self.fetch.call_count, 2)
        text = self.service.report_path.read_text()
        self.assertIn("模拟已平仓", text)
        with EntryReviewStore(self.service.db_path) as store:
            self.assertEqual(store.events()[0]["review"]["status"], "complete")

    def test_manual_review_is_allowed_intraday_but_ignores_unfinished_bars(self):
        self.service.record(self.event())
        self.service.request_review()
        self.service.tick(datetime(2026, 10, 15, 10))
        self.assertEqual(self.fetch.call_count, 2)
        with EntryReviewStore(self.service.db_path) as store:
            self.assertEqual(store.events()[0]["review"]["simulation"]["status"], "pending")

    def test_disable_persists_and_bypasses_record_and_refresh(self):
        self.service.set_enabled(False)
        self.assertFalse(self.service.record(self.event()))
        self.service.tick(datetime(2026, 11, 2, 18))
        self.fetch.assert_not_called()
        reopened = EntryReviewService(self.directory.name, self.fetch, autostart=False)
        self.addCleanup(reopened.close)
        self.assertFalse(reopened.enabled)
        self.service.set_enabled(True)
        self.assertTrue(self.service.record(self.event()))

    def test_invalid_config_is_visible_and_does_not_start_io(self):
        self.service.config_path.write_text('{"enabled": "false"}')
        reopened = EntryReviewService(self.directory.name, self.fetch, autostart=False)
        self.addCleanup(reopened.close)
        self.assertFalse(reopened.enabled)
        self.assertIn("配置", reopened.status)

    def test_network_failure_keeps_event_retryable_and_report_visible(self):
        self.service.record(self.event())
        self.fetch.side_effect = OSError("offline")
        self.service.tick(datetime(2026, 11, 2, 18))
        self.assertIn("offline", self.service.status)
        with EntryReviewStore(self.service.db_path) as store:
            self.assertEqual(len(store.pending_events()), 1)
        self.assertTrue(self.service.report_path.exists())

    def test_missing_event_delivery_does_not_block_later_events(self):
        self.service.delivery(self.event(), False)
        event = self.event()
        event["code"] = "sh600002"
        self.service.record(event)
        self.service.process_pending()
        with EntryReviewStore(self.service.db_path) as store:
            self.assertEqual(len(store.events()), 1)
        self.assertIn("事件不存在", self.service.status)

    def test_pending_batches_are_bounded_and_failed_first_event_cannot_starve_others(self):
        for index in range(12):
            event = self.event()
            event["code"] = f"sh{600000 + index}"
            self.service.record(event)
        self.service.tick(datetime(2026, 11, 2, 18))
        self.assertEqual(self.fetch.call_count, 9)
        self.service.tick(datetime(2026, 11, 2, 18, 2))
        self.assertEqual(self.fetch.call_count, 14)
        with EntryReviewStore(self.service.db_path) as store:
            self.assertEqual(store.pending_events(), [])

    def test_tracking_events_are_not_refetched_every_minute_after_batch_finishes(self):
        self.fetch.side_effect = lambda code, data_length=240: histories(2)[1 if code == "sh000001" else 0]
        for index in range(9):
            event = self.event()
            event["code"] = f"sh{600000 + index}"
            self.service.record(event)
        self.service.tick(datetime(2026, 10, 15, 18))
        self.assertEqual(self.fetch.call_count, 9)
        self.service.tick(datetime(2026, 10, 15, 18, 2))
        self.assertEqual(self.fetch.call_count, 11)
        self.service.tick(datetime(2026, 10, 15, 18, 4))
        self.assertEqual(self.fetch.call_count, 11)

    def test_real_worker_uses_its_own_sqlite_connection(self):
        service = EntryReviewService(self.directory.name, self.fetch, autostart=False,
                                     clock=lambda: datetime(2026, 10, 15, 10))
        self.addCleanup(service.close)
        service.record(self.event())
        finished = threading.Event()
        original = service._write_report

        def report():
            original()
            finished.set()

        with patch.object(service, "_write_report", side_effect=report):
            service.start()
            self.assertTrue(finished.wait(3))
        with EntryReviewStore(service.db_path) as store:
            self.assertEqual(len(store.events()), 1)
        self.fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
