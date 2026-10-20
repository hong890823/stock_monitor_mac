import unittest

from intraday_bars import TimeframeEvidence
from intraday_trade_policy import (
    DecisionAction,
    PositionEligibility,
    evaluate_trade_action,
)


def evidence(*, m15=2, aligned=True, risk=False, complete=True, risk_complete=True):
    return TimeframeEvidence(
        m15_confirmations=m15,
        m30_aligned=aligned,
        risk_confirmed=risk,
        complete=complete,
        risk_complete=risk_complete,
        last_completed_15m="2026-10-20 10:15:00",
        last_completed_30m="2026-10-20 10:00:00",
        m15_distance_pct=0.004,
        m30_distance_pct=0.004,
        clearance_confirmed=True,
    )


class IntradayTradePolicyTest(unittest.TestCase):
    def test_buy_requires_two_15m_and_30m_alignment(self):
        decision = evaluate_trade_action(
            "buy",
            evidence(),
            PositionEligibility(total_quantity=500, sellable_quantity=500, buy_capacity_quantity=100),
            invalidation_price=12.0,
        )

        self.assertEqual(decision.action, DecisionAction.BUY_SMALL)
        self.assertIn("15/30分钟", decision.message)

    def test_unconfirmed_buy_is_observation_only(self):
        decision = evaluate_trade_action(
            "buy",
            evidence(m15=1),
            PositionEligibility(total_quantity=500, sellable_quantity=500, buy_capacity_quantity=100),
            invalidation_price=12.0,
        )

        self.assertEqual(decision.action, DecisionAction.WAIT)
        self.assertIn("仅观察", decision.message)

    def test_risk_exit_ignores_30m_filter_but_respects_t1(self):
        decision = evaluate_trade_action(
            "risk_exit",
            evidence(m15=1, aligned=False, risk=True),
            PositionEligibility(total_quantity=100, sellable_quantity=0, today_locked_quantity=100),
            invalidation_price=12.0,
        )

        self.assertEqual(decision.action, DecisionAction.NEXT_DAY_REVIEW)
        self.assertIn("T+1", decision.message)
        self.assertIn("下一交易日", decision.message)

    def test_t_sell_is_capped_by_sellable_quantity(self):
        decision = evaluate_trade_action(
            "t_sell",
            evidence(),
            PositionEligibility(total_quantity=600, sellable_quantity=500, today_locked_quantity=100),
            invalidation_price=12.0,
        )

        self.assertEqual(decision.action, DecisionAction.T_SELL)
        self.assertLessEqual(decision.max_sell_quantity, 500)
        self.assertIn("原有可卖", decision.message)

    def test_missing_invalidation_price_does_not_issue_buy(self):
        decision = evaluate_trade_action(
            "buy",
            evidence(),
            PositionEligibility(total_quantity=500, sellable_quantity=500, buy_capacity_quantity=100),
            invalidation_price=0,
        )

        self.assertEqual(decision.action, DecisionAction.WAIT)
        self.assertIn("失效价", decision.message)


if __name__ == "__main__":
    unittest.main()
