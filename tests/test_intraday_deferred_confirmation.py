import unittest

from intraday_tactics import IntradaySignalType, IntradayTactics


class DeferredConfirmationTest(unittest.TestCase):
    def setUp(self):
        self.tactics = IntradayTactics()
        self.candidate = dict(
            code="sh600519", name="贵州茅台", price=1785.0,
            change_pct=-3.5, volume_ratio=0.8, limit_pct=10.0,
            support=1780.0, resistance=1850.0, score=3, quantity=300,
        )
        self.not_candidate = dict(self.candidate, price=1850.0, change_pct=0.0)

    def test_waited_buy_can_fire_after_timeframe_confirmation(self):
        self.assertIsNone(self.tactics.check_signal(**self.candidate))
        first = self.tactics.check_signal(**self.candidate)
        self.assertIsNotNone(first)

        self.tactics.defer_signal("sh600519", first.signal_type, retry_after_seconds=0)

        self.assertIsNone(self.tactics.check_signal(**self.candidate))
        second = self.tactics.check_signal(**self.candidate)
        self.assertIsNotNone(second)
        self.assertEqual(second.signal_type, IntradaySignalType.T_BUY)

    def test_condition_disappearance_rearms_deferred_signal(self):
        self.assertIsNone(self.tactics.check_signal(**self.candidate))
        first = self.tactics.check_signal(**self.candidate)
        self.tactics.defer_signal("sh600519", first.signal_type, retry_after_seconds=0)

        self.assertIsNone(self.tactics.check_signal(**self.not_candidate))
        self.assertIsNone(self.tactics.check_signal(**self.candidate))
        second = self.tactics.check_signal(**self.candidate)
        self.assertIsNotNone(second)


if __name__ == "__main__":
    unittest.main()
