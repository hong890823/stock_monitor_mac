import unittest

from indicator_sort import order_relaxed_hits, order_strict_hits


def hit(code, rsi, relaxed_score):
    return {"code": code, "name": code, "rsi": rsi, "relaxed_score": relaxed_score}


class IndicatorSortTests(unittest.TestCase):
    def test_strict_sorted_by_rsi_ascending(self):
        ordered = order_strict_hits([hit("a", 25.0, 3), hit("b", 12.0, 3), hit("c", 18.0, 3)])

        self.assertEqual(["b", "c", "a"], [item["code"] for item in ordered])

    def test_relaxed_sorted_by_score_desc_then_rsi_ascending(self):
        ordered = order_relaxed_hits([hit("a", 31.0, 2), hit("b", 44.0, 3), hit("c", 28.0, 2)])

        self.assertEqual(["b", "c", "a"], [item["code"] for item in ordered])

    def test_relaxed_puts_nan_rsi_last_within_same_score(self):
        ordered = order_relaxed_hits(
            [hit("a", float("nan"), 2), hit("b", 40.0, 2), hit("c", 44.0, 3)]
        )

        self.assertEqual(["c", "b", "a"], [item["code"] for item in ordered])

    def test_missing_rsi_is_treated_as_last(self):
        ordered = order_relaxed_hits([{"code": "a", "relaxed_score": 2}, hit("b", 40.0, 2)])

        self.assertEqual(["b", "a"], [item["code"] for item in ordered])

    def test_score_three_outranks_lower_rsi(self):
        ordered = order_relaxed_hits([hit("a", 12.0, 2), hit("b", 44.0, 3)])

        self.assertEqual(["b", "a"], [item["code"] for item in ordered])

    def test_risk_flag_is_sorted_after_other_relaxed_hits(self):
        safe = hit("safe", 44.0, 3)
        risky = hit("risky", 20.0, 3)
        risky["limit_up_shrink_acceleration"] = True

        ordered = order_relaxed_hits([risky, safe])

        self.assertEqual(["safe", "risky"], [item["code"] for item in ordered])


if __name__ == "__main__":
    unittest.main()
