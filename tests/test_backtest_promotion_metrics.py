#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import unittest
from unittest.mock import patch

from backtest_entry_structure import (
    SignalRecord,
    attach_forward_returns,
    fetch_universe,
    summarize,
)


def bars(closes, lows=None):
    lows = lows or closes
    return [
        {
            "date": f"2026-10-{day:02d}",
            "open": close,
            "high": close,
            "low": lows[index],
            "close": close,
            "volume": 1000,
        }
        for index, (day, close) in enumerate(zip(range(19, 25), closes))
    ]


class BacktestPromotionMetricsTests(unittest.TestCase):
    def test_universe_falls_back_to_cached_codes_when_provider_fails(self):
        with patch("akshare.stock_info_a_code_name", side_effect=RuntimeError("offline")):
            with patch(
                "backtest_entry_structure.load_cache",
                return_value={
                    code: [{}] * 60
                    for code in ("sh600001", "sz000002", "sh600003")
                },
            ):
                result = fetch_universe(2, seed=42)
        self.assertEqual(len(result), 2)
        self.assertTrue(
            set(result).issubset({
                ("sh600001", "sh600001"),
                ("sz000002", "sz000002"),
                ("sh600003", "sh600003"),
            })
        )

    def test_forward_path_return_and_drawdown(self):
        record = SignalRecord(
            code="sh600000", date="2026-10-19", state="BREAKOUT",
            can_buy=True, trend_weak=False, benchmark_ok=True,
            price=100.0, stop_price=95.0,
        )
        attach_forward_returns(
            [record],
            bars([100.0, 105.0, 103.0, 110.0, 108.0, 112.0]),
        )
        self.assertEqual(record.forward_close_path, [100.0, 105.0, 103.0, 110.0, 108.0, 112.0])
        self.assertAlmostEqual(record.fwd[1], 0.05)
        self.assertAlmostEqual(record.fwd[3], 0.10)
        self.assertAlmostEqual(record.fwd[5], 0.12)
        self.assertAlmostEqual(record.max_drawdown_5d, 2.0 / 105.0)

    def test_stop_path_is_recorded_with_drawdown_metrics(self):
        record = SignalRecord(
            code="sz000001", date="2026-10-19", state="PULLBACK_M20",
            can_buy=True, trend_weak=False, benchmark_ok=True,
            price=100.0, stop_price=95.0,
        )
        attach_forward_returns(
            [record],
            bars(
                [100.0, 94.0, 96.0, 97.0, 98.0, 99.0],
                lows=[100.0, 93.5, 95.0, 96.0, 97.0, 98.0],
            ),
        )
        self.assertTrue(record.stopped)
        self.assertAlmostEqual(record.stop_ret, -0.05)
        self.assertGreater(record.max_drawdown_5d, 0.05)

    def test_miss_rate_uses_ordinary_gate_population(self):
        records = [
            SignalRecord("a", "2026-10-19", "BREAKOUT", True, False, True, 10, 9),
            SignalRecord("b", "2026-10-19", "BREAKOUT", False, False, True, 10, 9),
            SignalRecord("c", "2026-10-19", "PULLBACK_M20", True, False, True, 10, 9),
            SignalRecord("d", "2026-10-19", "PULLBACK_M20", False, False, True, 10, 9),
            SignalRecord("e", "2026-10-19", "BREAKOUT", False, True, True, 10, 9),
            SignalRecord("f", "2026-10-19", "NO_STRUCTURE", False, False, True, 10, 9),
        ]
        result = summarize(records)
        self.assertEqual(result["eligible"], 4)
        self.assertEqual(result["eligible_not_promoted"], 2)
        self.assertEqual(result["miss_rate"], 0.5)


if __name__ == "__main__":
    unittest.main()
