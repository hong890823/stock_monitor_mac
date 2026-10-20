import os
import sys
import unittest
from unittest.mock import Mock


sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from intraday_tactics import IntradaySignalType, IntradayTactics
from stock_menubar import StockMenuBar


class IntradayNotificationTests(unittest.TestCase):
    def test_shallow_break_notification_is_one_clear_action(self):
        signal = IntradayTactics().evaluate(
            code="sh600522",
            name="中天科技",
            price=30.0,
            change_pct=-1.2,
            volume_ratio=1.0,
            limit_pct=10.0,
            support=30.5,
            resistance=32.5,
            score=-1,
            quantity=2000,
            cost=32.0,
        )

        self.assertEqual(signal.signal_type, IntradaySignalType.BREAK_SUPPORT)
        body = signal.notification_body
        self.assertNotIn("\n", body)
        self.assertLessEqual(len(body), 36)
        self.assertIn("30.50", body)
        self.assertIn("反抽不站回", body)
        self.assertIn("减约600股", body)

    def test_deep_break_notification_keeps_both_core_branches(self):
        signal = IntradayTactics().evaluate(
            code="sh600522",
            name="中天科技",
            price=30.0,
            change_pct=-1.2,
            volume_ratio=1.0,
            limit_pct=10.0,
            support=30.5,
            resistance=32.5,
            score=-1,
            quantity=2000,
            cost=45.0,
            total_capital=200_000.0,
        )

        self.assertEqual(signal.signal_type, IntradaySignalType.BREAK_SUPPORT)
        body = signal.notification_body
        self.assertNotIn("\n", body)
        self.assertLessEqual(len(body), 36)
        self.assertIn("不割肉", body)
        self.assertIn("32.50", body)
        self.assertIn("站回", body)

    def test_portfolio_notification_guards_raw_intraday_copy_and_logs_detail(self):
        subject = object.__new__(StockMenuBar)
        subject._portfolio_signals = {}
        subject._notify = Mock()
        subject.log = Mock()
        result = {
            "code": "sh600522",
            "name": "中天科技",
            "price": 30.0,
            "signal": "买入观察",
            "intraday_signal": {
                "type": "跌破支撑",
                "title": "⚠️ 中天科技 跌破支撑",
                "body": "完整策略说明",
                "notification_body": "支撑30.50｜反抽不站回减约600股",
            },
        }

        subject._send_portfolio_notification(result)

        subject._notify.assert_any_call(
            "⚠️ 中天科技 跌破支撑",
            "现价 30.00",
            "风险观察：风险条件尚未经新鲜15分钟K线确认，今日不作卖出处置。",
        )
        logs = "\n".join(call.args[0] for call in subject.log.call_args_list)
        self.assertIn("支撑30.50｜反抽不站回减约600股", logs)
        self.assertIn("完整策略说明", logs)

    def test_portfolio_state_notification_cannot_bypass_guard(self):
        subject = object.__new__(StockMenuBar)
        subject._portfolio_signals = {}
        subject._notify = Mock()
        subject.log = Mock()

        subject._send_portfolio_notification({
            "code": "sh600522",
            "name": "中天科技",
            "price": 30.0,
            "signal": "冲高做T",
            "quantity": 300,
            "sellable_quantity": 300,
            "trade_date": "2026-10-20",
        })

        subject._notify.assert_called_once_with(
            "📦 中天科技·冲高做T",
            "现价 30.00",
            "仅观察：双周期条件或当日可卖批次不足，不做T、不执行减仓或波段操作。",
        )

    def test_portfolio_trend_weak_uses_next_checkpoint(self):
        subject = object.__new__(StockMenuBar)
        subject._portfolio_signals = {}
        subject._notify = Mock()
        subject.log = Mock()

        subject._send_portfolio_notification({
            "code": "sh600522",
            "name": "中天科技",
            "price": 30.0,
            "signal": "趋势转弱观察",
        })

        subject._notify.assert_called_once_with(
            "📦 中天科技·趋势转弱观察",
            "现价 30.00",
            "M20失守｜暂不加仓，等收回确认",
        )

    def test_leader_hold_notification_is_one_clear_action(self):
        """龙头封板：横幅按「事实｜动作」给一个动作，不带换行。"""
        signal = IntradayTactics().evaluate(
            code="sh600522",
            name="中天科技",
            price=28.30,
            change_pct=6.3,
            volume_ratio=1.5,
            limit_pct=10.0,
            support=26.16,
            resistance=38.48,
            score=3,
            quantity=1000,
            leader_hold=True,
            leader_note="龙头封板",
        )

        self.assertEqual(signal.signal_type, IntradaySignalType.T_HOLD_LEADER)
        body = signal.notification_body
        self.assertNotIn("\n", body)
        self.assertLessEqual(len(body), 36)
        self.assertIn("暂缓做T", body)


if __name__ == '__main__':
    unittest.main()
