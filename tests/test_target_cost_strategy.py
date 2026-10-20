import unittest

from trade_calculator import plan_target_cost
from trade_calculator import format_target_cost_report


def _technical(**overrides):
    technical = {
        "score": 3,
        "support": 18.0,
        "resistance": 22.0,
        "volume_ratio": 0.8,
        "price_change": 0.5,
        "entry_structure": {
            "can_buy": True,
            "trend_weak": False,
            "state_label": "回踩支撑",
            "stop_price": 17.4,
        },
    }
    technical.update(overrides)
    return technical


class TargetCostStrategyTests(unittest.TestCase):
    def test_builds_three_lot_rounded_buy_tiers_that_reach_target_cost(self):
        result = plan_target_cost(
            holding_shares=1000,
            average_cost=20,
            target_cost=18,
            current_price=16,
            technical=_technical(
                support=15.8,
                entry_structure={
                    "can_buy": True,
                    "trend_weak": False,
                    "state_label": "回踩支撑",
                    "stop_price": 15.0,
                    "ma20": 16.2,
                },
                volatility={"atr": 0.45, "boll_mid": 16.4, "boll_lower": 15.4},
            ),
            capital_constraints={
                "total_capital": 200000,
                "available_capital": 100000,
                "max_position_pct": 30,
            },
        )

        self.assertEqual(len(result["buy_ladder"]), 3)
        self.assertTrue(result["target_reached"])
        triggers = [item["trigger_price"] for item in result["buy_ladder"]]
        self.assertEqual(triggers, sorted(triggers, reverse=True))
        self.assertTrue(all(item["shares"] > 0 and item["shares"] % 100 == 0
                            for item in result["buy_ladder"]))
        self.assertLessEqual(result["projected_average_cost"], 18.0001)

    def test_reports_unreachable_target_when_position_cap_blocks_adds(self):
        result = plan_target_cost(
            holding_shares=1000,
            average_cost=20,
            target_cost=18,
            current_price=16,
            technical=_technical(
                support=15.8,
                entry_structure={
                    "can_buy": True,
                    "trend_weak": False,
                    "state_label": "回踩支撑",
                    "stop_price": 15.0,
                },
            ),
            capital_constraints={
                "total_capital": 50000,
                "available_capital": 100000,
                "max_position_pct": 20,
            },
        )

        self.assertEqual(result["buy_ladder"], [])
        self.assertFalse(result["target_reached"])
        self.assertGreater(result["projected_average_cost"], 18)
        self.assertEqual(result["action"], "资金/仓位受限")
        self.assertIn("仓位上限", result["strategy"])

    def test_partial_capital_cap_returns_restricted_ladder_without_overpromising(self):
        result = plan_target_cost(
            holding_shares=1000,
            average_cost=20,
            target_cost=18,
            current_price=16,
            technical=_technical(
                support=15.8,
                entry_structure={
                    "can_buy": True,
                    "trend_weak": False,
                    "state_label": "回踩支撑",
                    "stop_price": 15.0,
                },
            ),
            capital_constraints={
                "total_capital": 200000,
                "available_capital": 10000,
                "max_position_pct": 30,
            },
        )

        self.assertGreater(len(result["buy_ladder"]), 0)
        self.assertFalse(result["target_reached"])
        self.assertEqual(result["action"], "受限梯度补仓")
        self.assertIn("资金上限", result["strategy"])
        self.assertIn("预计平均成本", result["strategy"])

    def test_estimates_available_capital_from_holding_cost_records(self):
        result = plan_target_cost(
            holding_shares=1000,
            average_cost=20,
            target_cost=18,
            current_price=16,
            technical=_technical(
                support=15.8,
                entry_structure={
                    "can_buy": True,
                    "trend_weak": False,
                    "state_label": "回踩支撑",
                    "stop_price": 15.0,
                },
            ),
            capital_constraints={
                "total_capital": 100000,
                "available_capital": 0,
                "max_position_pct": 50,
                "positions": [
                    {"code": "sh600001", "quantity": 1000, "cost": 20},
                    {"code": "sz000002", "quantity": 2000, "cost": 10},
                ],
            },
        )

        self.assertEqual(result["capital_limits"]["available_capital"], 60000)
        self.assertEqual(
            result["capital_limits"]["available_capital_source"],
            "按持仓成本记录估算",
        )
        self.assertTrue(result["target_reached"])
        report = format_target_cost_report(result, "样例")
        self.assertIn("可用资金口径：按持仓成本记录估算", report)

    def test_calculates_buy_quantity_needed_below_target(self):
        result = plan_target_cost(
            holding_shares=1000,
            average_cost=20,
            target_cost=18,
            current_price=16,
            technical=_technical(
                support=15.8,
                entry_structure={
                    "can_buy": True,
                    "trend_weak": False,
                    "state_label": "回踩支撑",
                    "stop_price": 15.0,
                },
            ),
        )

        self.assertAlmostEqual(result["required_buy_shares"], 1000)
        self.assertEqual(result["action"], "分批买入")
        self.assertIn("1000", result["strategy"])

    def test_weak_trend_blocks_averaging_down_even_when_price_is_below_target(self):
        result = plan_target_cost(
            holding_shares=1000,
            average_cost=20,
            target_cost=18,
            current_price=16,
            technical=_technical(
                score=-3,
                support=17.0,
                entry_structure={
                    "can_buy": False,
                    "trend_weak": True,
                    "state_label": "趋势转弱观察",
                    "stop_price": 16.5,
                },
            ),
        )

        self.assertEqual(result["action"], "等待确认")
        self.assertIn("不建议补仓摊平", result["strategy"])

    def test_current_price_above_target_does_not_chase(self):
        result = plan_target_cost(
            holding_shares=1000,
            average_cost=20,
            target_cost=18,
            current_price=19,
            technical=_technical(),
        )

        self.assertEqual(result["required_buy_shares"], 0)
        self.assertEqual(result["action"], "等待回落")
        self.assertIn("不追高", result["strategy"])

    def test_rally_above_resistance_prefers_t_sell_then_buyback(self):
        result = plan_target_cost(
            holding_shares=1000,
            average_cost=20,
            target_cost=18,
            current_price=23,
            technical=_technical(score=4, support=20, resistance=22),
            sellable_quantity=300,
        )

        self.assertEqual(result["action"], "冲高做T")
        self.assertIn("先分批卖出", result["strategy"])
        self.assertIn("回踩接回", result["strategy"])

    def test_rally_t_plan_has_sell_ladder_and_conditional_buyback(self):
        result = plan_target_cost(
            holding_shares=1000,
            average_cost=20,
            target_cost=18,
            current_price=23,
            technical=_technical(
                score=4,
                support=20,
                resistance=22,
                volume_ratio=0.8,
                entry_structure={
                    "can_buy": True,
                    "trend_weak": False,
                    "state_label": "冲高",
                    "stop_price": 19,
                    "ma20": 20.5,
                },
                volatility={"atr": 1.0},
            ),
            sellable_quantity=600,
        )

        t_plan = result["t_plan"]
        self.assertEqual(t_plan["total_sell_shares"], 200)
        self.assertEqual(len(t_plan["sell_batches"]), 2)
        self.assertEqual(sum(item["shares"] for item in t_plan["sell_batches"]), 200)
        self.assertIn("量比不高于0.90", t_plan["buyback_condition"])
        self.assertIn("跌破19.00", t_plan["invalidation_rule"])
        self.assertGreater(t_plan["estimated_realized_pnl"], 0)

    def test_buy_fee_input_is_not_mistaken_for_t_sell_fee(self):
        result = plan_target_cost(
            holding_shares=1000,
            average_cost=20,
            target_cost=18,
            current_price=23,
            technical=_technical(
                score=4,
                support=20,
                resistance=22,
                entry_structure={
                    "can_buy": True,
                    "trend_weak": False,
                    "state_label": "冲高",
                    "stop_price": 19,
                    "ma20": 20.5,
                },
                volatility={"atr": 1.0},
            ),
            sellable_quantity=600,
            fees=100,
        )

        t_plan = result["t_plan"]
        self.assertEqual(t_plan["estimated_sell_fees"], 0)
        self.assertEqual(t_plan["estimated_realized_pnl"], 700)
        self.assertIn("卖出费用未单独输入", t_plan["fee_note"])

    def test_report_separates_t_profit_from_book_average_cost(self):
        result = plan_target_cost(
            holding_shares=1000,
            average_cost=20,
            target_cost=18,
            current_price=23,
            technical=_technical(
                score=4,
                support=20,
                resistance=22,
                volume_ratio=0.8,
                entry_structure={
                    "can_buy": True,
                    "trend_weak": False,
                    "state_label": "冲高",
                    "stop_price": 19,
                    "ma20": 20.5,
                },
                volatility={"atr": 1.0},
            ),
            sellable_quantity=600,
        )

        report = format_target_cost_report(result, "样例")
        self.assertIn("冲高做T执行计划", report)
        self.assertIn("卖出梯度", report)
        self.assertIn("接回条件", report)
        self.assertIn("账面平均成本", report)
        self.assertIn("做T后实际净成本", report)
        self.assertIn("卖出费用未单独输入", report)

    def test_t_plus_one_locked_position_is_not_advised_to_sell(self):
        result = plan_target_cost(
            holding_shares=1000,
            average_cost=20,
            target_cost=18,
            current_price=23,
            technical=_technical(score=4, support=20, resistance=22),
            sellable_quantity=0,
        )

        self.assertEqual(result["action"], "冲高观察")
        self.assertIn("当日无可卖仓位", result["strategy"])

    def test_report_identifies_target_as_average_holding_cost(self):
        result = plan_target_cost(
            holding_shares=1000,
            average_cost=20,
            target_cost=18,
            current_price=16,
            technical=_technical(
                support=15.8,
                entry_structure={
                    "can_buy": True,
                    "trend_weak": False,
                    "state_label": "回踩支撑",
                    "stop_price": 15.0,
                },
            ),
        )

        report = format_target_cost_report(result, "样例")
        self.assertIn("目标持仓成本：18.00元", report)
        self.assertIn("不自动下单", report)
        self.assertIn("梯度补仓计划", report)
        self.assertIn("第1档", report)
        self.assertIn("成交后平均成本", report)
        self.assertIn("资金/仓位约束", report)

    def test_rejects_target_not_below_average_cost(self):
        with self.assertRaisesRegex(ValueError, "低于当前平均成本"):
            plan_target_cost(
                holding_shares=1000,
                average_cost=20,
                target_cost=20,
                current_price=19,
                technical=_technical(),
            )


if __name__ == "__main__":
    unittest.main()
