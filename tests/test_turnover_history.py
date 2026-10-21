from datetime import datetime
from math import isnan
from pathlib import Path
import tempfile
import unittest

from turnover_tracker import TurnoverTracker


class TurnoverHistoryTests(unittest.TestCase):
    def test_partial_day_is_a_gap_not_full_day_and_today_is_excluded(self):
        with tempfile.TemporaryDirectory() as directory:
            tracker = TurnoverTracker(str(Path(directory) / 'history.json'))
            tracker.sample(16000, datetime(2026, 10, 9, 15, 0), force_record=True)
            tracker.sample(17000, datetime(2026, 10, 12, 14, 45), force_record=True)
            tracker.sample(22000, datetime(2026, 10, 13, 15, 0), force_record=True)
            tracker = TurnoverTracker(str(Path(directory) / 'history.json'))

            history = tracker.recent_full_day_turnovers('2026-10-13')

            self.assertEqual(len(history), 2)
            self.assertEqual(history[0], 16000)
            self.assertTrue(isnan(history[1]))

    def test_dated_history_returns_only_prior_complete_close_days(self):
        with tempfile.TemporaryDirectory() as directory:
            tracker = TurnoverTracker(str(Path(directory) / 'history.json'))
            tracker.sample(16000, datetime(2026, 10, 9, 15, 0), force_record=True)
            tracker.sample(17000, datetime(2026, 10, 12, 14, 45), force_record=True)
            tracker.sample(22000, datetime(2026, 10, 13, 15, 0), force_record=True)

            history = tracker.recent_full_day_turnover_history(
                '2026-10-13', limit=5
            )

            self.assertEqual(history, [('2026-10-09', 16000.0)])

    def test_dated_history_backfills_older_complete_days_when_recent_day_is_partial(self):
        with tempfile.TemporaryDirectory() as directory:
            tracker = TurnoverTracker(str(Path(directory) / 'history.json'))
            for day, value in (
                ('2026-10-05', 15000),
                ('2026-10-06', 16000),
                ('2026-10-07', 17000),
                ('2026-10-08', 18000),
                ('2026-10-09', 19000),
                ('2026-10-12', 20000),
            ):
                tracker.sample(value, datetime.fromisoformat(day + 'T15:00'), force_record=True)
            tracker.sample(7000, datetime.fromisoformat('2026-10-13T14:45'), force_record=True)

            history = tracker.recent_full_day_turnover_history(
                '2026-10-13', limit=5
            )

            self.assertEqual(
                history,
                [
                    ('2026-10-06', 16000.0),
                    ('2026-10-07', 17000.0),
                    ('2026-10-08', 18000.0),
                    ('2026-10-09', 19000.0),
                    ('2026-10-12', 20000.0),
                ],
            )
