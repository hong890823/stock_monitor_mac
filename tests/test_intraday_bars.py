import unittest
from datetime import datetime

from intraday_bars import build_timeframe_evidence, parse_intraday_rows


def row(day, open_, high, low, close, volume=100):
    return {
        "day": day,
        "open": str(open_),
        "high": str(high),
        "low": str(low),
        "close": str(close),
        "volume": str(volume),
    }


class IntradayBarsTest(unittest.TestCase):
    def test_current_incomplete_bar_is_excluded(self):
        now = datetime(2026, 10, 20, 10, 14)
        bars = parse_intraday_rows([
            row("2026-10-20 09:45:00", 12, 12.2, 11.9, 12.1),
            row("2026-10-20 10:00:00", 12.1, 12.3, 12, 12.2),
            row("2026-10-20 10:15:00", 12.1, 12.3, 12, 12.2),
        ], scale=15, now=now)

        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[-1]["timestamp"], "2026-10-20 10:00:00")

    def test_buy_evidence_requires_two_completed_15m_confirmations(self):
        now = datetime(2026, 10, 20, 10, 31)
        bars15 = parse_intraday_rows([
            row("2026-10-20 10:15:00", 12, 12.2, 11.9, 12.1),
            row("2026-10-20 10:30:00", 12.1, 12.3, 12, 12.2),
        ], scale=15, now=now)
        bars30 = parse_intraday_rows([
            row("2026-10-20 10:00:00", 11.8, 12.1, 11.7, 12.0),
            row("2026-10-20 10:30:00", 12, 12.3, 11.9, 12.2),
        ], scale=30, now=now)

        evidence = build_timeframe_evidence(
            bars15, bars30, price=12.2, support=12.0, resistance=13.0, now=now
            , quote_date="2026-10-20"
        )

        self.assertEqual(evidence.m15_confirmations, 2)
        self.assertTrue(evidence.m30_aligned)
        self.assertTrue(evidence.complete)

    def test_risk_confirmation_ignores_30m_direction(self):
        now = datetime(2026, 10, 20, 10, 31)
        bars15 = parse_intraday_rows([
            row("2026-10-20 10:30:00", 11.9, 11.95, 11.7, 11.8),
        ], scale=15, now=now)
        bars30 = parse_intraday_rows([
            row("2026-10-20 10:00:00", 12.1, 12.2, 11.7, 12.1),
            row("2026-10-20 10:30:00", 12.1, 12.2, 11.7, 11.8),
        ], scale=30, now=now)

        evidence = build_timeframe_evidence(
            bars15, bars30, price=11.75, support=12.0, resistance=13.0,
            now=now, quote_date="2026-10-20"
        )

        self.assertTrue(evidence.risk_complete)
        self.assertTrue(evidence.risk_confirmed)
        self.assertFalse(evidence.m30_aligned)

    def test_missing_data_never_produces_complete_evidence(self):
        evidence = build_timeframe_evidence(
            [], [], price=12.1, support=12.0, resistance=13.0,
            now=datetime(2026, 10, 20, 10, 31),
        )

        self.assertFalse(evidence.complete)
        self.assertFalse(evidence.risk_complete)


if __name__ == "__main__":
    unittest.main()
