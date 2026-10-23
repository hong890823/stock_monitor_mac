import unittest
from datetime import datetime

from intraday_bars import TimeframeEvidence, build_timeframe_evidence
from intraday_trade_policy import DecisionAction, PositionEligibility, evaluate_trade_action


def evidence(
    *, m15=2, aligned=True, clearance=True, rejected=False, risk=False,
    risk_confirmations=3, risk_level="CRITICAL",
):
    return TimeframeEvidence(
        m15_confirmations=m15,
        m30_aligned=aligned,
        risk_confirmed=risk,
        complete=True,
        risk_complete=True,
        risk_confirmations=risk_confirmations if risk else 0,
        risk_level=risk_level if risk else "NORMAL",
        last_completed_15m="2026-10-20 10:30:00",
        last_completed_30m="2026-10-20 10:30:00",
        m15_distance_pct=0.004 if clearance else 0.001,
        m30_distance_pct=0.004 if clearance else 0.001,
        last_bar_rejected=rejected,
        clearance_confirmed=clearance,
    )


class IntradayEvidenceQualityTest(unittest.TestCase):
    def setUp(self):
        self.eligibility = PositionEligibility(
            total_quantity=500,
            sellable_quantity=500,
            buy_capacity_quantity=100,
        )

    def test_barely_above_support_is_not_actionable_buy(self):
        decision = evaluate_trade_action(
            "buy", evidence(clearance=False), self.eligibility, 12.0,
        )

        self.assertEqual(decision.action, DecisionAction.WAIT)
        self.assertIn("站稳", decision.message)

    def test_upper_wick_rejection_is_not_actionable_buy(self):
        decision = evaluate_trade_action(
            "buy", evidence(rejected=True), self.eligibility, 12.0,
        )

        self.assertEqual(decision.action, DecisionAction.WAIT)
        self.assertIn("上影", decision.message)

    def test_actionable_buy_keeps_03_percent_clearance(self):
        decision = evaluate_trade_action(
            "buy", evidence(), self.eligibility, 12.0,
            volume_context={
                "stock_volume_confirming": True,
                "market_expanding": True,
            },
        )

        self.assertEqual(decision.action, DecisionAction.BUY_SMALL)

    def test_single_15m_risk_bar_waits_for_confirmation(self):
        decision = evaluate_trade_action(
            "risk_exit",
            evidence(
                m15=1, aligned=False, risk=True,
                risk_confirmations=1, risk_level="ELEVATED",
            ),
            PositionEligibility(total_quantity=500, sellable_quantity=500),
            12.0,
        )

        self.assertEqual(decision.action, DecisionAction.WAIT)

    def test_bar_evidence_marks_clearance_and_rejection(self):
        bars15 = [
            {
                "timestamp": "2026-10-20 10:15:00",
                "open": 12.05, "high": 12.20, "low": 12.03,
                "close": 12.18, "volume": 100,
            },
            {
                "timestamp": "2026-10-20 10:30:00",
                "open": 12.18, "high": 12.45, "low": 12.15,
                "close": 12.25, "volume": 100,
            },
        ]
        bars30 = [
            {
                "timestamp": "2026-10-20 10:00:00",
                "open": 12.00, "high": 12.15, "low": 11.95,
                "close": 12.10, "volume": 200,
            },
            {
                "timestamp": "2026-10-20 10:30:00",
                "open": 12.15, "high": 12.22, "low": 12.08,
                "close": 12.25, "volume": 200,
            },
        ]

        result = build_timeframe_evidence(
            bars15,
            bars30,
            price=12.25,
            support=12.0,
            resistance=13.0,
            now=datetime(2026, 10, 20, 10, 31),
            quote_date="2026-10-20",
        )

        self.assertTrue(result.clearance_confirmed)
        self.assertTrue(result.last_bar_rejected)
        self.assertGreaterEqual(result.m15_distance_pct, 0.003)

    def test_buy_confirmations_do_not_cross_trade_days(self):
        bars15 = [
            {"timestamp": "2026-10-19 14:45:00", "open": 12.0, "high": 12.1, "low": 11.9, "close": 12.05, "volume": 100},
            {"timestamp": "2026-10-19 15:00:00", "open": 12.05, "high": 12.1, "low": 11.9, "close": 12.05, "volume": 100},
            {"timestamp": "2026-10-20 10:30:00", "open": 12.05, "high": 12.1, "low": 11.9, "close": 12.05, "volume": 100},
        ]
        bars30 = [
            {"timestamp": "2026-10-20 10:00:00", "open": 12.0, "high": 12.1, "low": 11.9, "close": 12.05, "volume": 100},
            {"timestamp": "2026-10-20 10:30:00", "open": 12.05, "high": 12.1, "low": 11.9, "close": 12.05, "volume": 100},
        ]

        result = build_timeframe_evidence(
            bars15, bars30,
            price=12.05, support=12.0, resistance=13.0,
            now=datetime(2026, 10, 20, 10, 31),
            quote_date="2026-10-20",
        )

        self.assertEqual(result.m15_confirmations, 1)

    def test_risk_confirmations_do_not_cross_trade_days(self):
        bars15 = [
            {"timestamp": "2026-10-19 14:45:00", "open": 11.9, "high": 11.9, "low": 11.7, "close": 11.8, "volume": 100},
            {"timestamp": "2026-10-19 15:00:00", "open": 11.8, "high": 11.8, "low": 11.7, "close": 11.75, "volume": 100},
            {"timestamp": "2026-10-20 10:30:00", "open": 11.75, "high": 11.8, "low": 11.6, "close": 11.7, "volume": 100},
        ]

        result = build_timeframe_evidence(
            bars15, [],
            price=11.7, support=12.0, resistance=13.0,
            now=datetime(2026, 10, 20, 10, 31),
            quote_date="2026-10-20",
        )

        self.assertTrue(result.risk_complete)
        self.assertEqual(result.risk_confirmations, 1)


if __name__ == "__main__":
    unittest.main()
