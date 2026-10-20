from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from indicator_refresh import IndicatorRefreshScheduler


class IndicatorRefreshSchedulerTests(unittest.TestCase):
    def test_night_window_uses_previous_date_after_midnight(self):
        state_file = Path(self._tmpdir.name) / "state.json"
        scheduler = IndicatorRefreshScheduler(state_file)

        self.assertIsNone(scheduler.window_date_for(datetime(2026, 8, 18, 17, 59)))
        self.assertEqual(scheduler.window_date_for(datetime(2026, 8, 18, 18, 0)), "2026-08-18")
        self.assertEqual(scheduler.window_date_for(datetime(2026, 8, 19, 5, 59)), "2026-08-18")
        self.assertIsNone(scheduler.window_date_for(datetime(2026, 8, 19, 6, 0)))


    def setUp(self):
        self._tmpdir = TemporaryDirectory()

    def tearDown(self):
        self._tmpdir.cleanup()

    def test_success_blocks_same_window_after_restart(self):
        state_file = Path(self._tmpdir.name) / "state.json"
        now = datetime(2026, 8, 18, 18, 0)
        scheduler = IndicatorRefreshScheduler(state_file)

        self.assertTrue(scheduler.can_attempt(now))
        scheduler.begin_attempt(now)
        scheduler.record_success(now)

        restarted = IndicatorRefreshScheduler(state_file)
        self.assertFalse(restarted.can_attempt(datetime(2026, 8, 19, 1, 0)))
        self.assertEqual(restarted.status(datetime(2026, 8, 19, 1, 0)), "本窗口已成功刷新")


    def test_failures_are_limited_to_eight_and_reset_next_window(self):
        state_file = Path(self._tmpdir.name) / "state.json"
        scheduler = IndicatorRefreshScheduler(state_file)
        now = datetime(2026, 8, 18, 18, 0)

        for attempt in range(8):
            attempt_time = now + timedelta(minutes=90 * attempt)
            scheduler.begin_attempt(attempt_time)
            scheduler.record_failure(attempt_time, "network unavailable")

        self.assertFalse(scheduler.can_attempt(datetime(2026, 8, 19, 5, 59)))
        self.assertEqual(scheduler.status(datetime(2026, 8, 19, 5, 59)), "本窗口已尝试 8 次，等待下一晚")
        self.assertTrue(scheduler.can_attempt(datetime(2026, 8, 19, 18, 0)))


    def test_scheduler_defers_failed_retry_for_ninety_minutes(self):
        state_file = Path(self._tmpdir.name) / "state.json"
        scheduler = IndicatorRefreshScheduler(state_file)
        now = datetime(2026, 8, 18, 18, 0)

        scheduler.begin_attempt(now)
        scheduler.record_failure(now, "spot request timed out")

        self.assertFalse(scheduler.can_attempt(datetime(2026, 8, 18, 19, 29)))
        self.assertTrue(scheduler.can_attempt(datetime(2026, 8, 18, 19, 30)))
