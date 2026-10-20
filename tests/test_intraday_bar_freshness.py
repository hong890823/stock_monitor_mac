import json
import unittest
from datetime import datetime
from pathlib import Path

from intraday_bars import build_timeframe_evidence, latest_completed_slot, parse_intraday_rows


FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "sina_intraday_timestamps.json"


def bars(timestamps, scale):
    rows = []
    for timestamp in timestamps:
        rows.append({
            "day": timestamp,
            "open": "12.10",
            "high": "12.18",
            "low": "12.08",
            "close": "12.13",
            "volume": "100",
        })
    return parse_intraday_rows(
        rows, scale=scale, now=datetime(2026, 10, 20, 23, 59),
        timestamp_semantics="end",
    )


class IntradayBarFreshnessTest(unittest.TestCase):
    def test_fixture_confirms_sina_day_is_bar_end_time(self):
        fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

        self.assertEqual(fixture["timestamp_semantics"], "end")
        self.assertEqual(
            fixture["scale15"][3]["day"],
            "2026-10-20 11:30:00",
        )
        self.assertEqual(
            fixture["scale15"][4]["day"],
            "2026-10-20 13:15:00",
        )

    def test_current_forming_bar_is_excluded_under_sina_end_timestamp(self):
        parsed = parse_intraday_rows(
            [
                {"day": "2026-10-20 10:00:00", "open": "12.0", "high": "12.1", "low": "11.9", "close": "12.05", "volume": "100"},
                {"day": "2026-10-20 10:15:00", "open": "12.05", "high": "12.1", "low": "12.0", "close": "12.08", "volume": "100"},
            ],
            scale=15,
            now=datetime(2026, 10, 20, 10, 14),
            timestamp_semantics="end",
        )

        self.assertEqual([item["timestamp"] for item in parsed], ["2026-10-20 10:00:00"])

    def test_latest_completed_slot_handles_lunch_break(self):
        quote_date = "2026-10-20"

        self.assertEqual(
            latest_completed_slot(datetime(2026, 10, 20, 13, 5), 15, quote_date),
            "2026-10-20 11:30:00",
        )
        self.assertEqual(
            latest_completed_slot(datetime(2026, 10, 20, 13, 16), 15, quote_date),
            "2026-10-20 13:15:00",
        )
        self.assertEqual(
            latest_completed_slot(datetime(2026, 10, 20, 13, 5), 30, quote_date),
            "2026-10-20 11:30:00",
        )

    def test_stale_bars_are_not_complete(self):
        stale15 = bars([
            "2026-10-19 11:15:00",
            "2026-10-19 11:30:00",
        ], 15)
        stale30 = bars([
            "2026-10-19 11:00:00",
            "2026-10-19 11:30:00",
        ], 30)

        evidence = build_timeframe_evidence(
            stale15, stale30, price=12.13, support=12.03, resistance=13.0,
            now=datetime(2026, 10, 20, 11, 31), quote_date="2026-10-20",
        )

        self.assertFalse(evidence.complete)
        self.assertFalse(evidence.risk_complete)

    def test_lunch_gap_keeps_1130_bar_current_until_next_slot_completes(self):
        bars15 = bars([
            "2026-10-20 11:15:00",
            "2026-10-20 11:30:00",
        ], 15)
        bars30 = bars([
            "2026-10-20 11:00:00",
            "2026-10-20 11:30:00",
        ], 30)

        evidence_at_1305 = build_timeframe_evidence(
            bars15, bars30, price=12.13, support=12.03, resistance=13.0,
            now=datetime(2026, 10, 20, 13, 5), quote_date="2026-10-20",
        )
        evidence_at_1316 = build_timeframe_evidence(
            bars15, bars30, price=12.13, support=12.03, resistance=13.0,
            now=datetime(2026, 10, 20, 13, 16), quote_date="2026-10-20",
        )

        self.assertTrue(evidence_at_1305.complete)
        self.assertFalse(evidence_at_1316.complete)


if __name__ == "__main__":
    unittest.main()
