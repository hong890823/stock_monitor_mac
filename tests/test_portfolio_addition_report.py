import unittest

from portfolio_addition import AdditionRecommendation
from portfolio_daily_report import format_addition_section, format_daily_report


def _recommendation(**changes):
    values = dict(
        code="sh600001", name="甲", selected=True,
        composite_score=82, technical_score=78, fundamental_score=88,
        yearly_score=92, monthly_score=86, weekly_score=80,
        shares=300, amount=3000, stop_price=9.5,
        position_pct_before=12.0, position_pct_after=14.2,
        reasons=("年K上行", "基本面未见恶化"), missing=(),
    )
    values.update(changes)
    return AdditionRecommendation(**values)


class PortfolioAdditionReportTests(unittest.TestCase):
    def test_report_shows_one_addition_candidate(self):
        text = format_daily_report(
            {}, [], {}, {"generated_at": "15:02"}, addition=_recommendation()
        )
        self.assertIn("四、加仓机会", text)
        self.assertIn("甲（sh600001）", text)
        self.assertIn("300股", text)
        self.assertIn("3,000元", text)
        self.assertIn("综合82.0", text)
        self.assertIn("技术78.0", text)
        self.assertIn("基本面88.0", text)
        self.assertIn("现价10.00", text)
        self.assertIn("止损9.50", text)
        self.assertIn("12.0% → 14.2%", text)
        self.assertIn("年K上行", text)
        self.assertIn("基本面未见恶化", text)
        self.assertIn("三、持仓整体操作建议", text)
        self.assertIn("四、加仓机会", text)
        self.assertIn("五、优先处理", text)
        self.assertIn("六、逐股明细", text)
        self.assertNotIn("★ 今日结论", text)
        self.assertIn("判断依据", text)

    def test_safe_portfolio_allows_only_selected_addition(self):
        text = format_daily_report(
            {
                "n_holdings": 2,
                "trend": {"below_ma20": 0, "risk_break": 0, "deep_loss": 0},
                "conclusion": "组合风险可控。",
            },
            [], {}, {"generated_at": "15:02"}, addition=_recommendation(),
        )

        self.assertIn("整体操作：维持现有仓位，可对通过评估的单只持仓小仓加仓", text)
        self.assertIn("新增仅限报告推荐的单只和参考股数", text)

    def test_portfolio_risk_overrides_selected_addition(self):
        text = format_daily_report(
            {
                "n_holdings": 4,
                "trend": {"below_ma20": 4, "risk_break": 3, "deep_loss": 2},
                "conclusion": "组合同步走弱。",
            },
            [], {}, {"generated_at": "15:02"}, addition=_recommendation(),
        )

        self.assertIn("整体操作：降低总仓位，暂停新增资金", text)
        self.assertIn("加仓机会仅供风险持仓处置完成后复核", text)
        self.assertNotIn("可对通过评估的单只持仓小仓加仓", text)

    def test_report_does_not_force_candidate(self):
        evaluations = [
            _recommendation(
                selected=False, shares=0, amount=0,
                reasons=("综合分低于70",), missing=("完整Y周期不足3根",),
            ),
        ]
        text = format_daily_report(
            {}, [], {}, {"generated_at": "15:02"},
            addition=None, evaluations=evaluations,
        )
        self.assertIn("当前不建议加仓", text)
        self.assertIn("综合分低于70", text)
        self.assertIn("完整Y周期不足3根", text)

    def test_error_keeps_safe_placeholder(self):
        section = format_addition_section(
            {"error": "network down"}, [], {"quote_date": "2026-10-15"}
        )
        text = "\n".join(section)
        self.assertIn("加仓评估暂不可用", text)
        self.assertIn("不影响原风险结论", text)
        self.assertNotIn("network down", text)


if __name__ == "__main__":
    unittest.main()
