from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from observation_pool import ObservationPool


def _hit(source="relaxed", name="测试股份"):
    return {"code": "sh600001", "name": name, "price": 10.0, "source": source}


def _bars(end="2026-10-14", kind="pullback"):
    end_date = datetime.fromisoformat(end)
    dates = []
    while len(dates) < 65:
        if end_date.weekday() < 5:
            dates.append(end_date.strftime("%Y-%m-%d"))
        end_date -= timedelta(days=1)
    dates.reverse()
    bars = []
    for index, date in enumerate(dates):
        close = 10 + index * 0.02 if kind == "pullback" else 10.0
        bars.append({"date": date, "open": close, "high": close + 0.3,
                     "low": close - 0.3, "close": close,
                     "volume": 60 if kind == "pullback" and index >= 60 else 100})
    if kind == "weak":
        for bar in bars[-2:]:
            bar.update(open=8, close=8, low=7.8, high=8.2)
    return bars


def _high_break_bars(end="2026-10-14"):
    bars = _bars(end, "neutral")
    for index in range(len(bars) - 6, len(bars)):
        progress = index - (len(bars) - 6)
        close = 18.4 + progress * 0.35
        bars[index].update(
            open=close - 0.1,
            high=close + 0.15,
            low=close - 0.15,
            close=close,
            volume=100 + progress * 10,
        )
    bars[-1].update(open=19.1, high=19.15, low=18.85, close=19.0, volume=260)
    return bars


class ObservationPoolTests(unittest.TestCase):
    def review(self, pool, date="2026-10-14", kind="pullback", hits=None, benchmark=True):
        bars = _bars(date, kind)
        return pool.review(hits or [], datetime.fromisoformat(date + "T18:00"),
                           histories={"sh600001": bars},
                           benchmark=_bars(date) if benchmark else [])

    def test_repeated_strict_hits_without_daily_data_never_promote(self):
        pool = ObservationPool()
        pool.review([_hit("strict")], datetime(2026, 10, 13, 18))
        pool.review([_hit("strict")], datetime(2026, 10, 14, 18))
        self.assertEqual(pool.records["sh600001"]["status"], "observing")
        self.assertEqual(pool.records["sh600001"]["observation_days"], 0)

    def test_entry_structure_stays_observing_until_strong_candidate(self):
        pool = ObservationPool()
        self.review(pool, "2026-10-13", hits=[_hit()])
        self.review(pool)
        record = pool.records["sh600001"]
        self.assertEqual(record["observation_days"], 2)
        self.assertEqual(record["status"], "observing")
        self.assertFalse(record["can_enter"])
        self.assertIn("M20 上行", record["reason"])
        self.assertIn("回踩", record["reason"])
        self.assertGreater(record["stop_price"], 0)

    def test_daily_ready_does_not_require_intraday_permissions(self):
        pool = ObservationPool()
        self.review(pool, "2026-10-13", hits=[_hit("strong")])
        self.review(pool)
        record = pool.records["sh600001"]
        self.assertEqual(record["status"], "ready")
        self.assertTrue(record["can_enter"])
        self.assertFalse(record["trade_permissions"]["allow_normal_buy"])
        self.assertEqual(record["continuation_hold"]["state"], "HOLD_CONTINUATION")

    def test_dynamic_risk_removes_sticky_normal_buy_permission(self):
        pool = ObservationPool()
        self.review(pool, "2026-10-13", hits=[_hit("strong")])
        pool.records["sh600001"]["trade_permissions"]["allow_normal_buy"] = True
        pool.review(
            [],
            datetime(2026, 10, 14, 18),
            histories={"sh600001": _high_break_bars()},
            benchmark=_bars("2026-10-14"),
        )
        self.assertFalse(
            pool.records["sh600001"]["trade_permissions"]["allow_normal_buy"]
        )

    def test_high_bias_ma5_break_risk_downgrades_candidate(self):
        pool = ObservationPool()
        bars = _high_break_bars()
        pool.review(
            [_hit("strong")],
            datetime(2026, 10, 14, 18),
            histories={"sh600001": bars},
            benchmark=_bars("2026-10-14"),
        )
        record = pool.records["sh600001"]
        self.assertFalse(record["can_enter"])
        self.assertEqual(record["strong_state"], "M5_BREAK_RISK")
        self.assertEqual(record["risk_line"], "MA5")
        self.assertGreater(record["risk_price"], record["price"])
        self.assertIn("风险", record["reason"])
        self.assertEqual(record["continuation_hold"]["state"], "BREAK_DEFENSE")

    def test_same_bar_on_weekend_and_same_day_does_not_count_twice(self):
        pool = ObservationPool()
        self.review(pool, "2026-10-16", hits=[_hit()])
        for at in (datetime(2026, 10, 16, 23), datetime(2026, 10, 17, 18), datetime(2026, 10, 18, 18)):
            pool.review([], at, histories={"sh600001": _bars("2026-10-16")},
                        benchmark=_bars("2026-10-16"))
        record = pool.records["sh600001"]
        self.assertEqual(record["observation_days"], 1)
        self.assertEqual(len(record["history"]), 1)
        self.assertEqual(record["status"], "observing")

    def test_incomplete_current_bar_cannot_promote(self):
        pool = ObservationPool()
        self.review(pool, "2026-10-13", hits=[_hit()])
        pool.review([], datetime(2026, 10, 14, 11),
                    histories={"sh600001": _bars()}, benchmark=_bars())
        self.assertEqual(pool.records["sh600001"]["observation_days"], 1)
        self.assertEqual(pool.records["sh600001"]["status"], "observing")

    def test_fetch_failure_does_not_eliminate_or_age_records(self):
        pool = ObservationPool()
        self.review(pool, "2026-10-13", hits=[_hit()])
        for day in (14, 15):
            pool.review([], datetime(2026, 10, day, 18), histories={})
        record = pool.records["sh600001"]
        self.assertEqual(record["observation_days"], 1)
        self.assertEqual(record["status"], "observing")
        self.assertIn("待复核", record["data_status"])

    def test_missing_benchmark_blocks_promotion(self):
        pool = ObservationPool()
        self.review(pool, "2026-10-13", hits=[_hit()])
        self.review(pool, benchmark=False)
        self.assertEqual(pool.records["sh600001"]["status"], "observing")
        self.assertIn("大盘", pool.records["sh600001"]["reason"])

    def test_missing_benchmark_does_not_expire_a_valid_structure_on_day_five(self):
        pool = ObservationPool()
        self.review(pool, "2026-10-12", hits=[_hit()])
        self.review(pool, "2026-10-16", benchmark=False)
        record = pool.records["sh600001"]
        self.assertEqual(record["status"], "observing")
        self.assertIn("待复核", record["data_status"])

    def test_terminal_record_requires_five_new_bars_and_a_fresh_hit_to_reenter(self):
        pool = ObservationPool()
        self.review(pool, "2026-10-12", "neutral", hits=[_hit()])
        self.review(pool, "2026-10-16", "neutral")
        self.review(pool, "2026-10-16", hits=[_hit("strict")])
        self.assertEqual(pool.records["sh600001"]["status"], "expired")
        self.review(pool, "2026-10-19", hits=[_hit("strict")])
        self.assertEqual(pool.records["sh600001"]["status"], "expired")
        self.review(pool, "2026-10-23", hits=[_hit("strict")])
        record = pool.records["sh600001"]
        self.assertEqual(record["status"], "observing")
        self.assertEqual(record["observation_days"], 1)
        self.assertEqual(record["previous_rounds"][0]["status"], "expired")

    def test_report_contains_daily_decisions_and_prices(self):
        pool = ObservationPool()
        self.review(pool, "2026-10-13", hits=[_hit()])
        self.review(pool)
        report = pool.report()
        self.assertIn("逐日复核", report)
        self.assertIn("2026-10-13", report)
        self.assertIn("2026-10-14", report)
        self.assertIn("首个完整交易日", report)

    def test_two_complete_weak_days_eliminate_and_keep_reason(self):
        pool = ObservationPool()
        self.review(pool, "2026-10-13", "weak", hits=[_hit()])
        self.review(pool, "2026-10-14", "weak")
        record = pool.records["sh600001"]
        self.assertEqual(record["status"], "eliminated")
        self.assertIn("趋势", record["reason"])
        self.assertIn("淘汰", "\n".join(pool.menu_lines()))

    def test_five_unconfirmed_bars_expire_even_if_original_rule_still_hits(self):
        pool = ObservationPool()
        self.review(pool, "2026-10-12", "neutral", hits=[_hit("strict")])
        self.review(pool, "2026-10-16", "neutral", hits=[_hit("strict")])
        record = pool.records["sh600001"]
        self.assertEqual(record["observation_days"], 5)
        self.assertEqual(record["status"], "expired")
        self.assertIn("5", record["reason"])

    def test_ready_record_is_downgraded_when_structure_disappears(self):
        pool = ObservationPool()
        self.review(pool, "2026-10-13", hits=[_hit()])
        self.review(pool)
        self.review(pool, "2026-10-15", "neutral")
        self.assertEqual(pool.records["sh600001"]["status"], "observing")

    def test_st_name_eliminates_without_waiting_for_history(self):
        pool = ObservationPool()
        self.review(pool, "2026-10-13", hits=[_hit()])
        pool.review([], datetime(2026, 10, 14, 18), names={"sh600001": "*ST测试"})
        self.assertEqual(pool.records["sh600001"]["status"], "eliminated")
        self.assertIn("ST", pool.records["sh600001"]["reason"])

    def test_nonfinite_or_invalid_bar_is_pending_not_a_signal(self):
        for bad_price in (float("inf"), float("nan"), -1):
            with self.subTest(price=bad_price):
                pool = ObservationPool()
                bars = _bars()
                bars[-1]["close"] = bad_price
                pool.review([_hit()], datetime(2026, 10, 14, 18),
                            histories={"sh600001": bars}, benchmark=_bars())
                self.assertEqual(pool.records["sh600001"]["observation_days"], 0)
                self.assertIn("待复核", pool.records["sh600001"]["data_status"])

    def test_state_roundtrip_keeps_sources_and_does_not_double_count(self):
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "pool.json"
            pool = ObservationPool(path)
            self.review(pool, "2026-10-13", hits=[_hit(), _hit("strict")])
            restored = ObservationPool(path)
            self.review(restored, "2026-10-13")
            record = restored.records["sh600001"]
            self.assertEqual(record["observation_days"], 1)
            self.assertEqual(set(record["sources"]), {"strict", "relaxed"})
            self.assertEqual(len(record["history"]), 1)

    def test_failed_atomic_write_preserves_previous_file_and_surfaces_error(self):
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "pool.json"
            pool = ObservationPool(path)
            self.review(pool, "2026-10-13", hits=[_hit()])
            original = path.read_text()
            with patch("observation_pool.os.replace", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    self.review(pool)
            self.assertEqual(path.read_text(), original)
            self.assertEqual(pool.records["sh600001"]["observation_days"], 1)

    def test_corrupt_state_is_reported_without_overwriting_it(self):
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "pool.json"
            path.write_text('{"records":')
            pool = ObservationPool(path)
            self.assertTrue(pool.load_error)
            with self.assertRaises(ValueError):
                pool.review([_hit()], datetime(2026, 10, 14, 18))
            self.assertEqual(path.read_text(), '{"records":')
