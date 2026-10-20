from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from capital_flow import CapitalFlowSnapshot
from observation_alerts import ObservationEntryTracker, is_entry_session
from observation_pool import ObservationPool
from tests.test_observation_pool import _bars, _hit


def observed_pool(path=None):
    pool = ObservationPool(path)
    pool.review([_hit()], datetime(2026, 10, 12, 18),
                histories={"sh600001": _bars("2026-10-12")}, benchmark=_bars("2026-10-12"))
    pool.review([], datetime(2026, 10, 13, 18),
                histories={"sh600001": _bars("2026-10-13")}, benchmark=_bars("2026-10-13"))
    return pool


def quote(at, price=11.15):
    return {"name": "测试股份", "price": price, "open": min(11.22, price + 0.05),
            "high": max(11.3, price + 0.05), "low": min(11.08, price - 0.05),
            "prev_close": 11.28, "volume": 5,
            "date": at.strftime("%Y-%m-%d"), "time": at.strftime("%H:%M:%S")}


def flow(at, net=0.03):
    return CapitalFlowSnapshot(key="sh600001", name="测试股份", kind="stock",
                               quote_date=at.strftime("%Y-%m-%d"), as_of=at,
                               source="fixture", scope="个股", session="intraday", net_yi=net)


class ObservationEntryTests(unittest.TestCase):
    def setUp(self):
        self.pool = observed_pool()
        self.tracker = ObservationEntryTracker()
        self.now = datetime(2026, 10, 14, 10)
        self.bars = _bars("2026-10-13")

    def check(self, minute=0, **overrides):
        now = self.now + timedelta(minutes=minute)
        kwargs = dict(record=self.pool.records["sh600001"], bars=self.bars,
                      quote=quote(now, 11.15 + minute * 0.005), benchmark_bars=self.bars,
                      benchmark_quote=quote(now, 11.3), flow=flow(now), now=now)
        kwargs.update(overrides)
        return self.tracker.evaluate(**kwargs)

    def test_three_distinct_fresh_samples_with_support_trigger_alert(self):
        self.assertIsNone(self.check(0))
        self.assertIsNone(self.check(1))
        alert = self.check(2)
        self.assertIsNotNone(alert)
        self.assertIn("回踩企稳", alert["title"])
        self.assertEqual(alert["subtitle"], "测试股份 现价11.16")
        self.assertNotIn("sh600001", alert["subtitle"])
        self.assertIn("资金", alert["body"])
        self.assertIn("失效", alert["body"])
        self.assertEqual(alert["code"], "sh600001")
        self.assertAlmostEqual(alert["price"], 11.16)

    def test_limit_and_near_limit_prices_block_even_with_valid_structure(self):
        for code, price in (("sh600001", 12.41), ("sh600001", 12.35),
                            ("sz300001", 13.54), ("sh688001", 13.54),
                            ("bj920001", 14.66)):
            with self.subTest(code=code, price=price):
                self.tracker = ObservationEntryTracker()
                candidate = dict(self.pool.records["sh600001"], code=code)
                with patch('observation_alerts.evaluate', return_value=SimpleNamespace(
                        can_buy=True, trend_weak=False, stop_price=10)):
                    for minute in (0, 1, 2):
                        now = self.now + timedelta(minutes=minute)
                        self.assertIsNone(self.check(
                            minute, record=candidate, quote=quote(now, price),
                            flow=replace(flow(now), key=code),
                        ))

    def test_limit_price_resets_confirmation_and_pullback_can_alert_later(self):
        self.assertIsNone(self.check(0))
        self.assertIsNone(self.check(1))
        with patch('observation_alerts.evaluate', return_value=SimpleNamespace(
                can_buy=True, trend_weak=False, stop_price=10)):
            self.assertIsNone(self.check(2, quote=quote(self.now + timedelta(minutes=2), 12.41)))
        self.assertNotIn('sh600001', self.tracker._samples)
        self.assertIsNone(self.check(3))
        self.assertIsNone(self.check(4))
        self.assertIsNotNone(self.check(5))

    def test_pool_labels_daily_candidates_and_historical_prices_explicitly(self):
        text = '\n'.join(self.pool.menu_lines(at=self.now))
        self.assertNotIn('可介入候选', text)
        self.assertIn('日线候选', text)
        self.assertIn('复核收盘价', text)
        self.assertIn('涨停附近', text)

    def test_saved_alert_price_is_not_presented_as_current_quote(self):
        self.check(0)
        self.check(1)
        self.pool.record_entry_alert(self.check(2))
        text = '\n'.join(self.pool.menu_lines(at=self.now + timedelta(minutes=3)))
        self.assertIn('今日提醒记录', text)
        self.assertIn('非当前买点', text)
        self.assertIn('当时触发价11.16', text)
        self.assertNotIn('现价11.16', text)

    def test_reusing_one_flow_or_quote_snapshot_never_counts_as_three_samples(self):
        for minute in (0, 1, 2):
            self.assertIsNone(self.check(minute, flow=flow(self.now)))
        self.tracker = ObservationEntryTracker()
        for minute in (0, 1, 2):
            self.assertIsNone(self.check(minute, quote=quote(self.now)))

    def test_single_day_observation_and_terminal_records_do_not_alert(self):
        for patch_record in ({"observation_days": 1}, {"status": "expired"}, {"data_status": "待复核"}):
            record = dict(self.pool.records["sh600001"], **patch_record)
            for minute in (0, 1, 2):
                self.assertIsNone(self.check(minute, record=record))

    def test_missing_stale_negative_and_wrong_stock_flow_block_alerts(self):
        for minute in (0, 1, 2):
            self.assertIsNone(self.check(minute, flow=None))
        for invalid in (flow(self.now - timedelta(minutes=10)), flow(self.now, -0.1),
                        replace(flow(self.now), key="sz000001"), replace(flow(self.now), net_yi=float("nan"))):
            self.tracker = ObservationEntryTracker()
            for minute in (0, 1, 2):
                self.assertIsNone(self.check(minute, flow=invalid))

    def test_flow_outflow_resets_confirmation_streak(self):
        self.check(0)
        self.check(1)
        self.assertIsNone(self.check(2, flow=flow(self.now + timedelta(minutes=2), -0.1)))
        self.assertIsNone(self.check(3))
        self.assertIsNone(self.check(4))
        self.assertIsNotNone(self.check(5))

    def test_price_still_making_new_lows_does_not_trigger(self):
        for minute in (0, 1, 2, 3):
            self.assertIsNone(self.check(minute, quote=quote(self.now + timedelta(minutes=minute), 11.22 - minute * 0.02)))

    def test_price_far_above_ma20_or_below_stop_does_not_trigger(self):
        for price in (12.5, 9.0):
            self.tracker = ObservationEntryTracker()
            for minute in (0, 1, 2):
                self.assertIsNone(self.check(minute, quote=quote(self.now + timedelta(minutes=minute), price)))

    def test_weak_market_or_previous_day_quote_does_not_trigger(self):
        for minute in (0, 1, 2):
            now = self.now + timedelta(minutes=minute)
            self.assertIsNone(self.check(minute, benchmark_quote=quote(now, 9)))
            self.assertIsNone(self.check(minute, quote=quote(now - timedelta(days=1))))

    def test_missed_updates_reset_streak(self):
        self.check(0)
        self.check(1)
        self.assertIsNone(self.check(8))
        self.assertIsNone(self.check(9))
        self.assertIsNotNone(self.check(10))

    def test_only_trading_sessions_after_opening_minutes_are_eligible(self):
        for stamp in ("2026-10-14T09:31", "2026-10-14T11:40", "2026-10-14T12:59",
                      "2026-10-14T15:00", "2026-10-17T10:00"):
            self.assertFalse(is_entry_session(datetime.fromisoformat(stamp)))
        for stamp in ("2026-10-14T09:35", "2026-10-14T10:00", "2026-10-14T13:00"):
            self.assertTrue(is_entry_session(datetime.fromisoformat(stamp)))

    def test_successful_delivery_is_persisted_and_not_repeated_after_restart(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "pool.json"
            self.pool = observed_pool(path)
            self.check(0)
            self.check(1)
            alert = self.check(2)
            self.pool.record_entry_alert(alert)
            self.pool = ObservationPool(path)
            self.tracker = ObservationEntryTracker()
            for minute in (3, 4, 5):
                self.assertIsNone(self.check(minute))
            self.assertIn("最近买点提醒", self.pool.report())

    def test_next_day_requires_new_confirmation_and_can_notify_again(self):
        self.check(0)
        self.check(1)
        alert = self.check(2)
        self.pool.record_entry_alert(alert)
        self.tracker.acknowledge(alert)
        self.pool.review([], datetime(2026, 10, 14, 18),
                         histories={"sh600001": _bars()}, benchmark=_bars())
        self.now = datetime(2026, 10, 15, 10)
        self.bars = _bars()
        self.assertIsNone(self.check(0))
        self.assertIsNone(self.check(1))
        self.assertIsNotNone(self.check(2))

    def test_afternoon_volume_scaling_uses_120_minutes_at_1300(self):
        self.now = datetime(2026, 10, 14, 13)
        for minute in (0, 1, 2):
            current_quote = quote(self.now + timedelta(minutes=minute))
            current_quote["volume"] = 42
            alert = self.check(minute, quote=current_quote)
        self.assertIsNone(alert)
