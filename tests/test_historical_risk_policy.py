import unittest
from datetime import datetime

from intraday_bars import TimeframeEvidence, build_timeframe_evidence
from intraday_trade_policy import (
    DecisionAction,
    PositionEligibility,
    evaluate_trade_action,
)


def intraday_bar(timestamp, open_, high, low, close):
    return {
        "timestamp": timestamp,
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
        "volume": 100,
    }


class HistoricalRiskPolicyTest(unittest.TestCase):
    def test_intraday_atr_raises_breach_threshold_above_fixed_floor(self):
        evidence = build_timeframe_evidence(
            [
                intraday_bar("2026-10-23 09:30:00", 35.0, 35.4, 34.2, 34.4),
                intraday_bar("2026-10-23 09:45:00", 34.4, 34.7, 33.9, 34.1),
            ],
            [],
            price=34.1,
            support=34.9,
            resistance=43.57,
            now=datetime(2026, 10, 23, 10, 0),
            quote_date="2026-10-23",
        )

        self.assertGreater(evidence.risk_threshold_pct, 0.005)
        self.assertLessEqual(evidence.risk_threshold_pct, 0.015)

    def test_single_risk_bar_is_observation_and_two_bars_only_reduce_one_lot(self):
        one_bar = TimeframeEvidence(
            m15_confirmations=1,
            m30_aligned=False,
            risk_confirmed=True,
            complete=True,
            risk_complete=True,
            risk_confirmations=1,
            risk_level="ELEVATED",
            last_completed_15m="2026-10-23 09:45:00",
            last_completed_30m=None,
        )
        two_bars = TimeframeEvidence(
            m15_confirmations=1,
            m30_aligned=False,
            risk_confirmed=True,
            complete=True,
            risk_complete=True,
            risk_confirmations=2,
            risk_level="ELEVATED",
            last_completed_15m="2026-10-23 10:00:00",
            last_completed_30m=None,
        )

        waiting = evaluate_trade_action(
            "risk_exit",
            one_bar,
            PositionEligibility(total_quantity=300, sellable_quantity=300),
            invalidation_price=32.32,
        )
        reduced = evaluate_trade_action(
            "risk_exit",
            two_bars,
            PositionEligibility(total_quantity=300, sellable_quantity=300),
            invalidation_price=32.32,
        )

        self.assertEqual(waiting.action, DecisionAction.WAIT)
        self.assertEqual(reduced.action, DecisionAction.RISK_REDUCE)
        self.assertEqual(reduced.max_sell_quantity, 100)

    def test_critical_history_and_three_bars_can_exit_remaining_position(self):
        evidence = TimeframeEvidence(
            m15_confirmations=1,
            m30_aligned=False,
            risk_confirmed=True,
            complete=True,
            risk_complete=True,
            risk_confirmations=3,
            risk_level="CRITICAL",
            last_completed_15m="2026-10-23 10:15:00",
            last_completed_30m=None,
        )

        decision = evaluate_trade_action(
            "risk_exit",
            evidence,
            PositionEligibility(total_quantity=300, sellable_quantity=200),
            invalidation_price=32.32,
        )

        self.assertEqual(decision.action, DecisionAction.RISK_EXIT)
        self.assertEqual(decision.max_sell_quantity, 200)

    def test_low_location_history_is_elevated_but_not_critical(self):
        evidence = build_timeframe_evidence(
            [
                intraday_bar("2026-10-23 09:30:00", 35.0, 35.1, 34.4, 34.5),
                intraday_bar("2026-10-23 09:45:00", 34.5, 34.6, 34.2, 34.3),
            ],
            [],
            price=34.3,
            support=34.9,
            resistance=43.57,
            now=datetime(2026, 10, 23, 10, 0),
            quote_date="2026-10-23",
            historical_context={
                "price_location": "LOW",
                "range_low": 32.32,
                "invalidation_price": 32.32,
                "major_trend": "MISSING",
                "closes_below_ma20": 10,
            },
        )

        self.assertGreaterEqual(evidence.risk_confirmations, 2)
        self.assertEqual(evidence.risk_level, "ELEVATED")

    def test_daily_systemic_risk_marks_intraday_history_critical(self):
        evidence = build_timeframe_evidence(
            [
                intraday_bar("2026-10-23 09:30:00", 35.0, 35.1, 34.4, 34.5),
                intraday_bar("2026-10-23 09:45:00", 34.5, 34.6, 34.2, 34.3),
            ],
            [],
            price=34.3,
            support=34.9,
            resistance=43.57,
            now=datetime(2026, 10, 23, 10, 0),
            quote_date="2026-10-23",
            historical_context={"systemic_risk": True},
        )

        self.assertEqual(evidence.risk_level, "CRITICAL")

    def test_daily_event_risk_marks_intraday_history_critical(self):
        evidence = build_timeframe_evidence(
            [
                intraday_bar("2026-10-23 09:30:00", 35.0, 35.1, 34.4, 34.5),
                intraday_bar("2026-10-23 09:45:00", 34.5, 34.6, 34.2, 34.3),
            ],
            [],
            price=34.3,
            support=34.9,
            resistance=43.57,
            now=datetime(2026, 10, 23, 10, 0),
            quote_date="2026-10-23",
            historical_context={"event_risk": True},
        )

        self.assertEqual(evidence.risk_level, "CRITICAL")


if __name__ == "__main__":
    unittest.main()
