from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import pandas as pd

from portfolio_addition_service import PortfolioAdditionService
from portfolio_addition_store import PortfolioAdditionStore
from portfolio_daily_report import format_daily_report
from stock_menubar import StockMenuBar


class FakeStockMenuBar:
    _run_portfolio_analysis = StockMenuBar._run_portfolio_analysis

    def __init__(self):
        self.portfolio = {
            "total_capital": 200000,
            "available_capital": 50000,
            "max_position_pct": 20,
            "positions": [{
                "code": "sh600001", "name": "甲", "quantity": 1000, "cost": 10,
            }],
        }
        self.stock_data = {}
        self.risk_results = []
        self.notifications = []
        self._portfolio_analysis_lock = threading.Lock()
        self.addition_service = Mock()
        self.portfolio_analysis_file = "/tmp/portfolio-addition-test.txt"

    def run_portfolio_analysis(self):
        self._portfolio_analysis_lock.acquire()
        self._run_portfolio_analysis("slot")

    def get_stock_data(self, code):
        return {"code": code, "price": 12.5, "date": "2026-10-15"}

    def get_stock_analysis_history(self, code, data_length=240):
        return pd.DataFrame([{
            "date": "2026-10-15", "open": 12, "high": 13,
            "low": 11, "close": 12.5, "volume": 1000,
        }])

    def _send_portfolio_notification(self, result):
        return None

    def _fundamental_health_for(self, *args, **kwargs):
        return None

    def _leader_hold_for(self, code):
        """替身：本用例不关心板块龙头，恒不抑制做T。"""
        return False, ""

    def _update_daily_report(self, results, slot):
        assessment = self._last_addition_assessment or {}
        addition = assessment.get("recommendation")
        if addition is None and assessment.get("error"):
            addition = assessment
        evaluations = assessment.get("evaluations") or ()
        self.daily_report = format_daily_report(
            {}, results, {}, {"generated_at": "15:02"},
            addition=addition, evaluations=evaluations,
        )

    def _save_portfolio_state(self, slot):
        return None

    def log(self, message, level):
        return None


class PortfolioAdditionServiceTests(unittest.TestCase):
    def test_prepare_inputs_fetches_missing_fundamentals_and_filters_watchlist(self):
        technical = {
            "sh600001": {
                "price": 12.5,
                "entry_can_buy": True,
                "entry_structure": {
                    "trend_assessment": {"major_trend": "筑底阶段"},
                    "high_position": {"state": "非高位"},
                    "trade_permissions": {"allow_probe": True},
                },
            },
            "sh600002": {"price": 20.0},
        }
        history = pd.DataFrame([{
            "date": "2026-10-15", "open": 12, "high": 13,
            "low": 11, "close": 12.5, "volume": 1000,
        }])
        history_fetcher = Mock(return_value=history)
        result = Mock(
            score=80,
            verdict="健康",
            quarters=[Mock(revenue_yoy=10.0, profit_yoy=12.0)],
            risks=[],
            notes=["基本面稳定"],
            deduct_ratio=0.9,
            gross_margin=30.0,
            net_margin=15.0,
            ocf_to_profit=1.1,
            roe=12.0,
            current_pe=15.0,
            current_pb=2.0,
            pe_percentile=25.0,
            pb_percentile=25.0,
            fair_low=10.0,
            fair_high=14.0,
        )
        fundamentals_fetcher = Mock(return_value=(result, "report"))
        with tempfile.TemporaryDirectory() as directory:
            store = PortfolioAdditionStore(Path(directory) / "cache.json")
            service = PortfolioAdditionService(
                history_fetcher=history_fetcher,
                store=store,
                fundamentals_fetcher=fundamentals_fetcher,
                health_grader=lambda _result, analyzed_at: Mock(
                    level="SOUND", analyzed_at=analyzed_at
                ),
            )
            inputs = service.prepare_inputs({
                "total_capital": 200000,
                "available_capital": 50000,
                "max_position_pct": 20,
                "initial_entry_pct": 7,
                "positions": [
                    {"code": "sh600001", "name": "甲", "quantity": 1000, "cost": 10},
                    {"code": "sh600002", "name": "观察", "quantity": 0, "cost": 0},
                ],
            }, technical, datetime(2026, 10, 15, 15))

            self.assertEqual([item.code for item in inputs], ["sh600001"])
            self.assertEqual(inputs[0].fundamental.health_level, "SOUND")
            self.assertEqual(inputs[0].fundamental.quarter_trends, ((10.0, 12.0),))
            self.assertEqual(inputs[0].missing, ())
            self.assertEqual(inputs[0].initial_entry_pct, 7.0)
            self.assertEqual(
                inputs[0].technical.get("trend_assessment", {}).get("major_trend"),
                "筑底阶段",
            )
            history_fetcher.assert_called_once_with("sh600001", data_length=1250)
            fundamentals_fetcher.assert_called_once_with("sh600001", "甲")

    def test_fundamental_refresh_failure_keeps_old_snapshot_and_reports_missing(self):
        old = Mock()
        technical = {"sh600001": {"price": 12.5}}
        history = pd.DataFrame()
        with tempfile.TemporaryDirectory() as directory:
            store = PortfolioAdditionStore(Path(directory) / "cache.json")
            service = PortfolioAdditionService(
                history_fetcher=Mock(return_value=history),
                store=store,
                fundamentals_fetcher=Mock(side_effect=RuntimeError("network down")),
                health_grader=Mock(),
            )
            service.store = Mock()
            service.store.load.return_value = old
            service.store.is_fresh.return_value = False
            inputs = service.prepare_inputs([{
                "code": "sh600001", "name": "甲", "quantity": 1000,
                "cost": 10, "total_capital": 200000,
                "available_capital": 50000, "max_position_pct": 20,
            }], technical, datetime(2026, 10, 15, 15))

            self.assertIs(inputs[0].fundamental, old)
            self.assertTrue(any("基本面更新失败" in item for item in inputs[0].missing))


class StockMenuBarAdditionIntegrationTests(unittest.TestCase):
    @patch("stock_menubar.format_portfolio_report", return_value="raw")
    @patch("stock_menubar.evaluate_position")
    @patch("stock_menubar.analyze_history")
    @patch("stock_menubar.select_addition_candidate")
    @patch("stock_menubar.assess_holding")
    def test_addition_failure_keeps_existing_portfolio_report(
        self, assess, select, analyze, evaluate, _raw_report
    ):
        app = FakeStockMenuBar()
        analyze.return_value = {"price": 12.5, "entry_stop_price": 11.5}
        evaluate.return_value = {"code": "sh600001", "risk": "none"}
        app.addition_service.prepare_inputs = Mock(
            side_effect=RuntimeError("network down")
        )
        app.run_portfolio_analysis()
        self.assertTrue(app.risk_results)
        self.assertIn("加仓评估暂不可用", app.daily_report)
        self.assertIn("不影响原风险结论", app.daily_report)
        self.assertEqual(app.notifications, [])
        assess.assert_not_called()
        select.assert_not_called()

    @patch("stock_menubar.format_portfolio_report", return_value="raw")
    @patch("stock_menubar.evaluate_position")
    @patch("stock_menubar.analyze_history")
    @patch("stock_menubar.select_addition_candidate")
    @patch("stock_menubar.assess_holding")
    def test_success_passes_single_recommendation_to_report(
        self, assess, select, analyze, evaluate, _raw_report
    ):
        app = FakeStockMenuBar()
        technical = {"price": 12.5, "entry_stop_price": 11.5}
        analyze.return_value = technical
        evaluate.return_value = {"code": "sh600001", "risk": "none"}
        prepared = Mock(code="sh600001")
        app.addition_service.prepare_inputs.return_value = [prepared]
        assessment = Mock(code="sh600001", name="甲", data_date="2026-10-15")
        assess.return_value = assessment
        recommendation = SimpleNamespace(
            code="sh600001", name="甲", selected=True,
            composite_score=82, technical_score=78, fundamental_score=88,
            yearly_score=92, monthly_score=86, weekly_score=80,
            shares=300, amount=3000, stop_price=11.5,
            position_pct_before=6.25, position_pct_after=8.125,
            reasons=("趋势良好",), missing=(), price=12.5,
            data_date="2026-10-15",
        )
        select.return_value = recommendation
        app.run_portfolio_analysis()
        self.assertIs(app._last_addition_assessment["recommendation"], recommendation)
        self.assertIn("甲（sh600001）", app.daily_report)
        self.assertIn("300股", app.daily_report)
        self.assertEqual(app.notifications, [])


if __name__ == "__main__":
    unittest.main()
