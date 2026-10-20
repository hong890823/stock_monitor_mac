import unittest

from limit_up_acceleration import evaluate_limit_up_acceleration


class LimitUpAccelerationTests(unittest.TestCase):
    def test_limit_up_with_shrinking_volume_is_flagged(self):
        result = evaluate_limit_up_acceleration(
            "sh600001",
            closes=[10.0] * 21 + [11.0],
            volumes=[100.0] * 20 + [100.0, 70.0],
        )

        self.assertTrue(result["limit_up"])
        self.assertTrue(result["shrinking"])
        self.assertTrue(result["flagged"])
        self.assertEqual(result["tag"], "⚠️涨停缩量加速·不追高")

    def test_non_limit_up_shrink_is_not_flagged(self):
        result = evaluate_limit_up_acceleration(
            "sh600001",
            closes=[10.0] * 20 + [10.2, 10.5],
            volumes=[100.0] * 20 + [100.0, 70.0],
        )

        self.assertFalse(result["limit_up"])
        self.assertFalse(result["flagged"])

    def test_missing_history_is_safe(self):
        result = evaluate_limit_up_acceleration(
            "sh600001", closes=[10.0], volumes=[100.0]
        )

        self.assertFalse(result["flagged"])
        self.assertEqual(result["tag"], "")


if __name__ == "__main__":
    unittest.main()
