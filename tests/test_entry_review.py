from copy import deepcopy
from datetime import datetime, timedelta
import unittest

from entry_review import DEFAULT_POLICY, evaluate_event


def bar(date, price=10, **values):
    return dict(date=date, open=price, close=price, high=price + .5,
                low=price - .5, volume=100000, **values)


def sample_event():
    return dict(code="sh600001", name="测试", date="2026-10-14", at="2026-10-14T10:02:00",
                first_observed_date="2026-10-12", price=10, stop_price=9,
                strategy_version="observation-pullback-v1", sources=["strict"],
                evidence={"benchmark_price": 100, "observation_days": 2},
                policy=deepcopy(DEFAULT_POLICY))


def histories(count=12):
    stock, benchmark = [], []
    day = datetime(2026, 10, 14)
    while len(stock) < count:
        if day.weekday() < 5:
            date = day.strftime("%Y-%m-%d")
            stock.append(bar(date))
            benchmark.append(bar(date, 100))
        day += timedelta(days=1)
    return stock, benchmark


class EntryReviewTests(unittest.TestCase):
    def evaluate(self, stock=None, benchmark=None, event=None, at=None):
        defaults = histories()
        return evaluate_event(event or sample_event(), stock if stock is not None else defaults[0],
                              benchmark if benchmark is not None else defaults[1],
                              at or datetime(2026, 11, 2, 18))

    def test_horizons_follow_benchmark_sessions_and_relative_return(self):
        stock, benchmark = histories()
        stock[3].update(close=10.3)
        benchmark[3].update(close=100.5)
        result = self.evaluate(stock, benchmark)
        self.assertEqual(result["horizons"]["3"]["date"], "2026-10-19")
        self.assertAlmostEqual(result["horizons"]["3"]["return_pct"], 3)
        self.assertAlmostEqual(result["horizons"]["3"]["excess_pct"], 2.5)
        self.assertEqual(result["status"], "complete")

    def test_missing_stock_day_does_not_shift_horizon(self):
        stock, benchmark = histories()
        stock.pop(1)
        result = self.evaluate(stock, benchmark)
        self.assertEqual(result["horizons"]["1"]["status"], "missing")
        self.assertEqual(result["horizons"]["3"]["date"], "2026-10-19")
        self.assertEqual(result["simulation"]["status"], "pending_data")
        self.assertEqual(result["simulation"]["trades"], [])

    def test_trigger_day_extremes_are_excluded(self):
        stock, benchmark = histories()
        stock[0].update(high=99, low=1)
        result = self.evaluate(stock, benchmark)
        self.assertAlmostEqual(result["max_favorable_pct"], 5)
        self.assertAlmostEqual(result["max_adverse_pct"], -5)

    def test_future_and_unfinished_bars_do_not_enter_result(self):
        result = self.evaluate(at=datetime(2026, 10, 15, 14))
        self.assertEqual(result["horizons"]["1"]["status"], "pending")
        self.assertEqual(result["simulation"]["status"], "pending")
        self.assertIsNone(result["max_favorable_pct"])

    def test_missing_calendar_anchor_does_not_invent_offsets(self):
        stock, benchmark = histories()
        result = self.evaluate(stock, benchmark[1:])
        self.assertEqual(result["status"], "pending_data")
        self.assertEqual(result["simulation"]["trades"], [])

    def test_gap_in_benchmark_does_not_shift_known_stock_sessions(self):
        stock, benchmark = histories()
        benchmark.pop(1)
        result = self.evaluate(stock, benchmark)
        self.assertEqual(result["status"], "pending_data")
        self.assertEqual(result["horizons"]["1"]["status"], "pending")
        self.assertEqual(result["simulation"]["trades"], [])

    def test_zero_volume_day_is_not_treated_as_executable(self):
        stock, benchmark = histories()
        stock[1]["volume"] = 0
        result = self.evaluate(stock, benchmark)
        self.assertEqual(result["horizons"]["1"]["status"], "missing")
        self.assertEqual(result["simulation"]["status"], "pending_data")

    def test_large_price_discontinuity_keeps_simulation_pending_review(self):
        stock, benchmark = histories()
        stock[2].update(open=5, high=5.5, low=4.5, close=5)
        result = self.evaluate(stock, benchmark)["simulation"]
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(len(result["trades"]), 1)

    def test_price_discontinuity_is_excluded_from_return_statistics(self):
        stock, benchmark = histories()
        for daily in stock[2:]:
            daily.update(open=5, high=5.5, low=4.5, close=5)
        result = self.evaluate(stock, benchmark)
        self.assertEqual(result["horizons"]["1"]["status"], "available")
        self.assertEqual(result["horizons"]["10"]["status"], "needs_review")
        self.assertIsNone(result["horizons"]["10"]["return_pct"])
        self.assertIsNone(result["horizons"]["10"]["excess_pct"])
        self.assertAlmostEqual(result["max_adverse_pct"], -5)
        self.assertEqual(result["status"], "pending_data")
        self.assertIn("价格", result["quality_warning"])

    def test_exact_three_percent_entry_gap_is_allowed(self):
        stock, benchmark = histories()
        stock[1].update(open=10.3, high=10.8, low=10, close=10.3)
        result = self.evaluate(stock, benchmark)["simulation"]
        self.assertTrue(result["trades"])

    def test_fixed_budget_fees_and_time_exit(self):
        result = self.evaluate()["simulation"]
        self.assertEqual(result["status"], "closed")
        buy, sell = result["trades"]
        self.assertEqual(buy["date"], "2026-10-15")
        self.assertEqual(buy["quantity"], 900)
        self.assertEqual(buy["price"], 10.01)
        self.assertEqual(sell["date"], "2026-10-28")
        self.assertEqual(sell["reason"], "time_exit")
        self.assertAlmostEqual(result["cash"], 10000 + result["realized_pnl"])
        self.assertAlmostEqual(result["realized_pnl"],
                               (sell["price"] - buy["price"]) * 900 - buy["fee"] - sell["fee"])
        self.assertEqual(result["quantity"], 0)

    def test_policy_snapshot_is_used_instead_of_global_defaults(self):
        event = sample_event()
        event["policy"]["budget"] = 2000
        result = self.evaluate(event=event)["simulation"]
        self.assertEqual(result["trades"][0]["quantity"], 100)
        self.assertEqual(result["initial_cash"], 2000)

    def test_buy_day_cannot_sell_and_next_day_gap_stop(self):
        stock, benchmark = histories()
        stock[1].update(low=8, close=8.9)
        stock[2].update(open=8.5, high=9, low=8, close=8.6)
        trades = self.evaluate(stock, benchmark)["simulation"]["trades"]
        self.assertEqual(trades[1]["date"], "2026-10-16")
        self.assertLess(trades[1]["price"], 8.5)

    def test_same_bar_stop_and_target_uses_stop(self):
        stock, benchmark = histories()
        stock[2].update(high=13, low=8)
        sell = self.evaluate(stock, benchmark)["simulation"]["trades"][1]
        self.assertEqual(sell["reason"], "stop")
        self.assertLess(sell["price"], 9)

    def test_one_price_entry_and_large_gap_are_not_filled(self):
        for change in ({"open": 10, "close": 10, "high": 10, "low": 10},
                       {"open": 11, "close": 11, "high": 11.5, "low": 10.5},
                       {"open": 8.9, "close": 9, "high": 9.5, "low": 8.5}):
            with self.subTest(change=change):
                stock, benchmark = histories()
                stock[1].update(change)
                result = self.evaluate(stock, benchmark)["simulation"]
                self.assertEqual(result["status"], "unfilled")
                self.assertEqual(result["trades"], [])

    def test_missing_day_while_holding_cannot_invent_exit(self):
        stock, benchmark = histories()
        stock.pop(3)
        result = self.evaluate(stock, benchmark)["simulation"]
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual(len(result["trades"]), 1)

    def test_one_price_exit_waits_for_next_usable_day(self):
        stock, benchmark = histories()
        stock[10].update(open=10, high=10, low=10, close=10)
        result = self.evaluate(stock, benchmark)["simulation"]
        self.assertEqual(result["trades"][1]["date"], stock[11]["date"])

    def test_nonfinite_prices_and_invalid_policy_are_rejected(self):
        for field, value in (("price", float("nan")), ("stop_price", 0)):
            event = sample_event()
            event[field] = value
            with self.assertRaises(ValueError):
                self.evaluate(event=event)
        event = sample_event()
        event["policy"]["slippage"] = -1
        with self.assertRaises(ValueError):
            self.evaluate(event=event)

    def test_stale_data_is_explicit_and_not_a_completed_review(self):
        stock, benchmark = histories(2)
        result = self.evaluate(stock, benchmark)
        self.assertEqual(result["as_of_date"], "2026-10-15")
        self.assertEqual(result["status"], "tracking")
        self.assertEqual(result["horizons"]["10"]["status"], "pending")


if __name__ == "__main__":
    unittest.main()
