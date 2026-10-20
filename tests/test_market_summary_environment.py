import unittest
from unittest import mock

from stock_menubar import StockMenuBar, market_rebound_guidance
from turnover_tracker import Snapshot, Source


class MarketSummaryEnvironmentTests(unittest.TestCase):
    def test_rebound_guidance_avoids_absolute_deleveraging_language(self):
        guidance = market_rebound_guidance(-1.0)

        self.assertIn('收回支撑', guidance)
        self.assertIn('量能承接', guidance)
        self.assertIn('控制风险', guidance)
        self.assertNotIn('任何反弹都是减仓窗口', guidance)

    def test_rebound_guidance_keeps_strong_market_message(self):
        guidance = market_rebound_guidance(1.0)

        self.assertIn('量能继续配合', guidance)
        self.assertIn('避免追高', guidance)

    def test_summary_keeps_yi_units_and_divergence_conclusion(self):
        app = StockMenuBar.__new__(StockMenuBar)
        quote = dict(price=3200, high=3300, low=3150, open=3190,
                     prev_close=3190, change_pct=0.3, date='2026-10-13')
        app.stock_data = {code: dict(quote) for code in ('sh000001', 'sz399001', 'sz399006')}
        app.sector_data = {'_all': [{'name': '行业甲', 'change': 1.0}], 'top': [], 'bottom': []}
        app._limit_stocks = {}
        app._turnover_snapshot = Snapshot('10:30', 60, 9000, 8000, 18000, 22000, Source.LOCAL)
        app.turnover_tracker = mock.Mock()
        app.turnover_tracker.recent_full_day_turnovers.return_value = [16000, 17000]
        app.get_concept_summary_data = mock.Mock(return_value={'sectors': [], 'rising': 80, 'falling': 20})
        app.get_kline_data = mock.Mock(return_value=[])
        app._market_turnover_lines = mock.Mock(return_value=['全天预估22,000亿'])

        report = app.generate_market_summary()

        self.assertIn('全天预估：22,000亿', report)
        self.assertIn('环境判断：放量分歧', report)
        conclusion = next(line for line in report.splitlines() if line.startswith('📌 核心结论：'))
        self.assertIn('放量分歧', conclusion)
