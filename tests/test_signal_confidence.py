import unittest

from intraday_tactics import IntradaySignalType
from signal_confidence import SignalConfidence, TickSnapshot


class TestSignalConfidence(unittest.TestCase):
    def test_high_turnover_caution_uses_upward_momentum(self):
        evaluator = SignalConfidence()
        history = [
            TickSnapshot(
                timestamp=float(index),
                price=10.0 + index * 0.1,
                change_pct=1.0,
                volume_ratio=1.5,
            )
            for index in range(4)
        ]

        momentum = evaluator._calculate_momentum(
            history,
            IntradaySignalType.T_HOLD_CAUTION,
            current_price=10.3,
        )

        self.assertEqual(momentum, 1.0)


if __name__ == "__main__":
    unittest.main()
