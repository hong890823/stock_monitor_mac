import unittest
from datetime import datetime
from unittest.mock import patch

from alert_repeat_guard import AlertRepeatGuard
from dip_rebound import DipReboundEngine, Sample
from intraday_tactics import IntradaySignalType, IntradayTactics
from limit_seal_events import SealTick, is_sealed
from quote_clock import QuoteClock, parse_quote_clock
from signal_confidence import SignalConfidence, SignalRecord, TickSnapshot


class ConfirmationBehaviorTests(unittest.TestCase):
    def test_one_round_confirms_on_first_evaluation(self):
        tactics = IntradayTactics()
        params = dict(
            code="sh600519", name="贵州茅台", price=1860.0,
            change_pct=3.0, volume_ratio=1.5, limit_pct=10.0,
            support=1750.0, resistance=1850.0, score=3, quantity=300,
        )

        signal = tactics.check_signal(confirmation_rounds=1, **params)

        self.assertIsNotNone(signal)
        self.assertEqual(signal.signal_type, IntradaySignalType.BREAKOUT)

    def test_cooldown_survives_a_condition_gap(self):
        tactics = IntradayTactics()
        broken = dict(
            code="sh600519", name="贵州茅台", price=1750.0,
            change_pct=-2.0, volume_ratio=1.0, limit_pct=10.0,
            support=1780.0, resistance=1850.0, score=3, quantity=100,
        )
        recovered = dict(broken, price=1800.0, change_pct=0.5)

        with patch("intraday_tactics.time.time", side_effect=[100.0, 111.0, 122.0, 701.0]):
            self.assertIsNotNone(tactics.check_signal(**broken))
            self.assertIsNone(tactics.check_signal(**recovered))
            self.assertIsNone(tactics.check_signal(**broken))
            self.assertIsNotNone(tactics.check_signal(**broken))

    def test_zero_rounds_suppresses_non_risk_candidate(self):
        tactics = IntradayTactics()
        params = dict(
            code="sh600519", name="贵州茅台", price=1785.0,
            change_pct=-3.5, volume_ratio=0.8, limit_pct=10.0,
            support=1780.0, resistance=1850.0, score=3, quantity=300,
            trade_permissions={"allow_reentry": False},
            trend={"major_trend": "UP"},
            high_position={"state": "SAFE"},
        )

        self.assertIsNone(tactics.check_signal(confirmation_rounds=0, **params))

    def test_zero_rounds_does_not_suppress_risk(self):
        tactics = IntradayTactics()
        params = dict(
            code="sh600519", name="贵州茅台", price=1750.0,
            change_pct=-2.0, volume_ratio=1.0, limit_pct=10.0,
            support=1780.0, resistance=1850.0, score=3, quantity=100,
        )

        signal = tactics.check_signal(confirmation_rounds=0, **params)

        self.assertIsNotNone(signal)
        self.assertEqual(signal.signal_type, IntradaySignalType.BREAK_SUPPORT)

    def test_releasing_wait_attempt_does_not_consume_action_budget(self):
        tactics = IntradayTactics()
        params = dict(
            code="sh600519", name="贵州茅台", price=1785.0,
            change_pct=-3.5, volume_ratio=0.8, limit_pct=10.0,
            support=1780.0, resistance=1850.0, score=3, quantity=300,
        )
        tactics.check_signal(**params)
        signal = tactics.check_signal(**params)

        tactics.release_signal_attempt("sh600519", signal.signal_type)

        state = tactics._ensure_state("sh600519")
        self.assertEqual(state["daily_count"][signal.signal_type], 0)
        self.assertEqual(state["shared_count"], 0)


class SignalConfidenceBehaviorTests(unittest.TestCase):
    def test_low_confidence_suppresses_signal(self):
        confidence = SignalConfidence()
        confidence._history["sh600519"] = [
            TickSnapshot(
                timestamp=float(index * 15),
                price=10.0 - index * 0.1,
                change_pct=-1.0,
                volume_ratio=1.0 + (index % 5),
                conditions={"放量突破": False},
            )
            for index in range(20)
        ]

        rounds = confidence.get_confirmation_rounds(
            "sh600519", IntradaySignalType.BREAKOUT, 8.0, {"放量突破": True},
        )

        self.assertEqual(rounds, 0)

    def test_trade_success_direction_is_signal_specific(self):
        sell = SignalConfidence()
        sell._signal_records["sh600519"] = [
            SignalRecord(IntradaySignalType.T_SELL.value, 0.0, 10.0, price_after_5m=9.0),
        ]
        buy = SignalConfidence()
        buy._signal_records["sh600519"] = [
            SignalRecord(IntradaySignalType.T_BUY.value, 0.0, 10.0, price_after_5m=11.0),
        ]

        self.assertEqual(
            sell._calculate_historical_success("sh600519", IntradaySignalType.T_SELL),
            1.0,
        )
        self.assertEqual(
            buy._calculate_historical_success("sh600519", IntradaySignalType.T_BUY),
            1.0,
        )


class QuoteClockTests(unittest.TestCase):
    def test_parses_quote_date_and_time(self):
        clock = parse_quote_clock({"date": "2026-10-22", "time": "14:35:15"})

        self.assertEqual(clock, QuoteClock("2026-10-22", datetime(2026, 10, 22, 14, 35, 15)))

    def test_accepts_datetime_in_date_field(self):
        clock = parse_quote_clock({"date": "2026-10-22 14:35:15", "time": ""})

        self.assertEqual(clock.at, datetime(2026, 10, 22, 14, 35, 15))

    def test_rejects_missing_or_invalid_clock(self):
        self.assertIsNone(parse_quote_clock({"date": "2026-10-22", "time": ""}))
        self.assertIsNone(parse_quote_clock({"date": "bad", "time": "14:35:15"}))


class AlertRepeatGuardTests(unittest.TestCase):
    def test_same_wait_signature_is_emitted_once_until_reset(self):
        guard = AlertRepeatGuard()

        self.assertTrue(guard.accept("sh600519", ("T_BUY", "等待15/30确认")))
        self.assertFalse(guard.accept("sh600519", ("T_BUY", "等待15/30确认")))
        guard.clear("sh600519")
        self.assertTrue(guard.accept("sh600519", ("T_BUY", "等待15/30确认")))


class IntradayCopyAndQuoteQualityTests(unittest.TestCase):
    def test_dip_observation_keeps_its_own_risk_copy(self):
        engine = DipReboundEngine()
        low = Sample(
            code="sz000001", name="测试股", price=9.65, change_pct=-3.5,
            cumulative_turnover=100.0, timestamp=100.0, stock=True,
        )
        first_watch = Sample(
            code="sz000001", name="测试股", price=9.76, change_pct=-2.4,
            cumulative_turnover=180.0, timestamp=210.0, stock=True,
        )
        second_watch = Sample(
            code="sz000001", name="测试股", price=9.76, change_pct=-2.4,
            cumulative_turnover=180.0, timestamp=240.0, stock=True,
        )
        engine.update([low], "2026-10-22")
        engine.update([first_watch], "2026-10-22")
        alert = engine.update([second_watch], "2026-10-22")[0]

        self.assertIn("尚未确认，谨防诱多", alert.body)
        self.assertNotIn("行情日期未确认", alert.body)

    def test_missing_order_book_fields_do_not_confirm_seal(self):
        self.assertFalse(is_sealed(SealTick(
            code="sz002185", name="华天科技", price=15.68, prev_close=14.25,
        )))


if __name__ == "__main__":
    unittest.main()
