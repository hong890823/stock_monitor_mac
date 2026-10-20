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
