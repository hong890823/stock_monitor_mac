import unittest

from intraday_bars import TimeframeEvidence
from intraday_trade_policy import PositionEligibility
from ai_analysis import QA_SYSTEM_PROMPT, SYSTEM_PROMPT
from dip_rebound import DipReboundEngine, Sample
from portfolio_analysis import format_portfolio_report
from portfolio_daily_report import build_cautions
from trade_action_guard import guard_actionable_copy


def evidence(*, risk=False):
    return TimeframeEvidence(
        m15_confirmations=2,
        m30_aligned=True,
        risk_confirmed=risk,
        complete=True,
        risk_complete=True,
        risk_confirmations=3 if risk else 0,
        risk_level="CRITICAL" if risk else "NORMAL",
        last_completed_15m="2026-10-20 10:30:00",
        last_completed_30m="2026-10-20 10:30:00",
        m15_distance_pct=0.004,
        m30_distance_pct=0.004,
        clearance_confirmed=True,
    )


class AllIntradayActionSurfacesTest(unittest.TestCase):
    def test_dip_rebound_cannot_issue_direct_buy_or_t_action(self):
        guarded = guard_actionable_copy(
            "buy",
            "可补仓做T",
            PositionEligibility(),
            None,
            trade_date=None,
        )

        self.assertEqual(guarded.level, "observation")
        self.assertNotIn("可补仓", guarded.text)
        self.assertNotIn("做T减仓", guarded.text)

    def test_actionable_copy_requires_guarded_evidence(self):
        guarded = guard_actionable_copy(
            "t_sell",
            "做T减仓",
            PositionEligibility(sellable_quantity=500),
            None,
            trade_date="2026-10-20",
        )

        self.assertEqual(guarded.level, "observation")
        self.assertNotIn("做T减仓", guarded.text)

    def test_valid_dual_timeframe_buy_passes_guard(self):
        guarded = guard_actionable_copy(
            "buy",
            "小仓买入",
            PositionEligibility(buy_capacity_quantity=100),
            evidence(),
            trade_date="2026-10-20",
            invalidation_price=12.0,
            volume_context={
                "stock_volume_confirming": True,
                "market_expanding": True,
            },
        )

        self.assertEqual(guarded.level, "actionable")
        self.assertEqual(guarded.max_quantity, 100)

    def test_missing_sellable_risk_becomes_next_day_fact(self):
        guarded = guard_actionable_copy(
            "risk_exit",
            "清仓离场",
            PositionEligibility(today_locked_quantity=100),
            evidence(risk=True),
            trade_date="2026-10-20",
        )

        self.assertEqual(guarded.level, "next_day")
        self.assertIn("风险事实", guarded.text)
        self.assertIn("下一交易日", guarded.text)

    def test_portfolio_report_action_is_guarded(self):
        result = {
            "name": "北方铜业",
            "code": "sz000737",
            "quantity": 500,
            "cost": 12.5,
            "price": 12.13,
            "position_pct": 10.0,
            "signal": "冲高做T",
            "score": 0,
            "support": 12.03,
            "resistance": 13.0,
            "volume_ratio": 1.2,
            "action": "乖离偏大，可分批做T，回踩再接回",
            "action_core": "乖离偏大，可分批做T",
            "situation_notes": [],
            "data_age_days": 0,
        }

        report = format_portfolio_report([result], {"total_capital": 100000})

        self.assertNotIn("可分批做T", report)
        self.assertNotIn("回踩再接回", report)
        self.assertIn("仅观察", report)

    def test_portfolio_report_raw_intraday_signal_is_guarded(self):
        result = {
            "name": "北方铜业",
            "code": "sz000737",
            "quantity": 500,
            "cost": 12.5,
            "price": 12.13,
            "position_pct": 10.0,
            "signal": "买入观察",
            "score": 0,
            "support": 12.03,
            "resistance": 13.0,
            "volume_ratio": 1.2,
            "action": "等待确认后分批观察",
            "action_core": "等待确认",
            "situation_notes": [],
            "data_age_days": 0,
            "trade_date": "2026-10-20",
            "intraday_signal": {
                "type": "回踩接回",
                "title": "📉 北方铜业 回踩接回",
                "body": "站回支撑后可小仓补，创新低取消",
                "notification_body": "小仓补",
            },
        }

        report = format_portfolio_report([result], {"total_capital": 100000})

        self.assertNotIn("小仓补", report)
        self.assertIn("仅观察：15/30分钟条件未达到稳健买点", report)

    def test_daily_report_cautions_do_not_command_deep_loss_actions(self):
        cautions = build_cautions({
            "n_holdings": 2,
            "trend": {"deep_loss": 2},
        })

        text = "\n".join(cautions)
        self.assertNotIn("小仓补", text)
        self.assertNotIn("逻辑坏则反弹离场", text)
        self.assertIn("不直接补仓或离场", text)

    def test_dip_rebound_alert_is_guarded(self):
        engine = DipReboundEngine()
        alert = engine._build_alert(
            Sample(
                code="sz002185",
                name="华天科技",
                price=18.0,
                change_pct=-1.2,
                volume_ratio=1.5,
                cumulative_turnover=100.0,
                timestamp=1_000_000.0,
                stock=True,
            ),
            day_low=-3.5,
            recovered=2.3,
            leg=1.5,
        )

        self.assertNotIn("补仓/做T窗口", alert.body)
        self.assertIn("仅观察", alert.body)

    def test_ai_prompts_cannot_override_local_action_guard(self):
        for prompt in (SYSTEM_PROMPT, QA_SYSTEM_PROMPT):
            self.assertIn("本地动作守卫", prompt)
            self.assertIn("不得直接下达", prompt)


if __name__ == "__main__":
    unittest.main()
