#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from types import SimpleNamespace
import unittest

from high_position_policy import HighPositionState
from intraday_bars import TimeframeEvidence
from intraday_tactics import IntradaySignalType, IntradayTactics
from intraday_trade_policy import (
    DecisionAction,
    PositionEligibility,
    evaluate_trade_action,
)
from trend_assessment import (
    MajorTrend,
    ShortTrend,
    TradePermissions,
    TrendAssessment,
    TrendConflict,
    TrendEvidence,
    build_trade_permissions,
)


def trend(major=MajorTrend.UP, short=ShortTrend.UP, conflict=TrendConflict.ALIGNED_UP):
    return TrendAssessment(
        short_trend=short,
        major_trend=major,
        conflict=conflict,
        reasons=(),
        missing_data=(),
        evidence=TrendEvidence(),
        summary="",
    )


def risk_only():
    return TradePermissions(risk_only=True, blockers=("大趋势向下",))


def timeframe_evidence(*, risk=False):
    return SimpleNamespace(
        complete=True,
        m15_confirmations=2,
        m30_aligned=True,
        clearance_confirmed=True,
        risk_complete=True,
        risk_confirmed=risk,
        risk_confirmations=3 if risk else 0,
        risk_level="CRITICAL" if risk else "NORMAL",
        last_bar_rejected=False,
    )


def intraday_params(**overrides):
    params = dict(
        code="sh600000",
        name="示例",
        price=10.0,
        change_pct=-3.5,
        volume_ratio=0.8,
        limit_pct=10.0,
        support=10.5,
        resistance=12.0,
        score=3,
        quantity=0,
        ma20=10.8,
        ma20_prev=11.0,
    )
    params.update(overrides)
    return params


class IntradayTrendGateTests(unittest.TestCase):
    def test_menu_policy_passes_trend_gate_to_trade_decision(self):
        from stock_menubar import StockMenuBar

        class Ledger:
            @staticmethod
            def position_summary(_code, _trade_date):
                return {
                    "quantity": 500,
                    "sellable_quantity": 500,
                    "today_locked_quantity": 0,
                }

        class Tactics:
            def defer_signal(self, *_args, **_kwargs):
                return None

        app = StockMenuBar.__new__(StockMenuBar)
        app._trade_ledger = Ledger()
        app._intraday_bar_fetcher = lambda *_args, **_kwargs: []
        app._intraday_tactics = Tactics()
        app.log = lambda *_args, **_kwargs: None
        original = IntradayTactics().evaluate(
            **intraday_params(
                quantity=500,
                support=9.9,
                ma20=None,
                ma20_prev=None,
            ),
            trade_permissions=risk_only(),
            trend=trend(MajorTrend.DOWN),
        )

        result = app._apply_intraday_trade_policy(
            original,
            "sh600000",
            "示例",
            10.0,
            9.9,
            12.0,
            trade_date="2026-10-21",
            permissions=risk_only(),
            trend=trend(MajorTrend.DOWN),
            high_position=HighPositionState.DISTRIBUTION_RISK,
        )

        self.assertEqual(result.signal_type, IntradaySignalType.TRADE_WAIT)
        self.assertIn("大趋势向下", result.body)

    def test_major_down_suppresses_normal_buy_and_reentry_signals(self):
        tactics = IntradayTactics()
        blocked = risk_only()

        pullback = tactics.evaluate(
            **intraday_params(),
            trade_permissions=blocked,
            trend=trend(MajorTrend.DOWN, ShortTrend.REBOUND, TrendConflict.DOWN_MAJOR_REBOUND),
        )
        reentry = tactics.evaluate(
            **intraday_params(quantity=500, change_pct=-3.5),
            trade_permissions=blocked,
            trend=trend(MajorTrend.DOWN, ShortTrend.REBOUND, TrendConflict.DOWN_MAJOR_REBOUND),
        )

        self.assertNotIn(
            getattr(pullback, "signal_type", None), {IntradaySignalType.PULLBACK}
        )
        self.assertNotIn(
            getattr(reentry, "signal_type", None), {IntradaySignalType.T_BUY}
        )

    def test_down_major_rebound_copy_says_rebound_not_uptrend(self):
        signal = IntradayTactics().evaluate(
            **intraday_params(price=12.2, change_pct=2.0, volume_ratio=1.5),
            trade_permissions=risk_only(),
            trend=trend(MajorTrend.DOWN, ShortTrend.REBOUND, TrendConflict.DOWN_MAJOR_REBOUND),
        )

        self.assertIsNotNone(signal)
        self.assertIn("下跌趋势中的反弹", signal.body)
        self.assertNotIn("上涨趋势确认", signal.body)
        self.assertNotIn("可买", signal.body)

    def test_risk_exit_is_not_suppressed_by_trend_gate(self):
        decision = evaluate_trade_action(
            "risk_exit",
            timeframe_evidence(risk=True),
            PositionEligibility(total_quantity=500, sellable_quantity=500),
            invalidation_price=9.5,
            permissions=risk_only(),
            trend=trend(MajorTrend.DOWN),
            high_position=HighPositionState.DISTRIBUTION_RISK,
        )

        self.assertEqual(decision.action, DecisionAction.RISK_EXIT)

    def test_high_consolidation_blocks_reentry_and_t(self):
        permissions = build_trade_permissions(
            trend(),
            True,
            HighPositionState.HIGH_CONSOLIDATION,
            True,
            True,
            timeframe_evidence(),
            "do_t",
            100000.0,
            5.0,
        )

        self.assertFalse(permissions.allow_reentry)
        self.assertFalse(permissions.allow_t)


if __name__ == "__main__":
    unittest.main()
