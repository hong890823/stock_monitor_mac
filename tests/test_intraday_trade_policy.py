import unittest

from intraday_bars import TimeframeEvidence
from intraday_trade_policy import (
    DecisionAction,
    PositionEligibility,
    evaluate_trade_action,
)
from trade_action_guard import guard_actionable_copy


def evidence(
    *, m15=2, aligned=True, risk=False, complete=True, risk_complete=True,
    risk_confirmations=2, risk_level="ELEVATED",
):
    return TimeframeEvidence(
        m15_confirmations=m15,
        m30_aligned=aligned,
        risk_confirmed=risk,
        complete=complete,
        risk_complete=risk_complete,
        risk_confirmations=risk_confirmations,
        risk_level=risk_level,
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

    def test_volume_absorption_reduces_only_one_lot(self):
        decision = evaluate_trade_action(
            "risk_exit",
            evidence(risk=True, risk_confirmations=2),
            PositionEligibility(total_quantity=300, sellable_quantity=300),
            invalidation_price=12.0,
            volume_context={"absorption_confirmed": True},
        )

        self.assertEqual(decision.action, DecisionAction.RISK_REDUCE)
        self.assertEqual(decision.max_sell_quantity, 100)

    def test_insufficient_stock_volume_blocks_buy(self):
        decision = evaluate_trade_action(
            "buy",
            evidence(),
            PositionEligibility(total_quantity=500, sellable_quantity=500, buy_capacity_quantity=100),
            invalidation_price=12.0,
            volume_context={"stock_volume_confirming": False},
        )

        self.assertEqual(decision.action, DecisionAction.WAIT)
        self.assertIn("量能", decision.message)
        guarded = guard_actionable_copy(
            "buy", decision.message,
            eligibility=PositionEligibility(
                total_quantity=500,
                sellable_quantity=500,
                buy_capacity_quantity=100,
            ),
            evidence=evidence(),
            trade_date="2026-10-20",
            invalidation_price=12.0,
        )
        self.assertEqual(guarded.level, "observation")

    def test_missing_volume_evidence_blocks_buy(self):
        decision = evaluate_trade_action(
            "buy",
            evidence(),
            PositionEligibility(total_quantity=500, sellable_quantity=500, buy_capacity_quantity=100),
            invalidation_price=12.0,
            volume_context={},
        )

        self.assertEqual(decision.action, DecisionAction.WAIT)
        self.assertIn("量能证据不足", decision.message)


if __name__ == "__main__":
    unittest.main()
