import unittest

from market_evidence import (
    EvidenceCard,
    build_decision_review,
    format_evidence_cards,
    summarize_t_filter_outcomes,
    update_t_filter_event_outcome,
)


class EvidenceCardTests(unittest.TestCase):
    def test_complete_card_keeps_source_and_scope(self):
        lines = format_evidence_cards([
            EvidenceCard(
                label="全天预估成交额",
                value="22,000亿",
                source="TurnoverTracker",
                quote_date="2026-10-14",
                scope="沪深两市",
            )
        ])
        self.assertEqual(len(lines), 1)
        self.assertIn("全天预估成交额：22,000亿", lines[0])
        self.assertIn("来源 TurnoverTracker", lines[0])
        self.assertIn("范围 沪深两市", lines[0])

    def test_unavailable_and_stale_cards_are_explicit(self):
        lines = format_evidence_cards([
            EvidenceCard("北向净流向", None, available=False),
            EvidenceCard("板块宽度", "68%", fresh=False),
        ])
        self.assertIn("北向净流向：暂缺", lines[0])
        self.assertIn("板块宽度：68%（已过期）", lines[1])


class DecisionReviewTests(unittest.TestCase):
    def test_strong_market_has_support_and_invalidation(self):
        review = build_decision_review({
            "predicted_turnover_yi": 22000,
            "market_change_pct": 1.2,
            "industry_breadth": 0.72,
            "concept_breadth": 0.68,
            "index_near_high": True,
            "persistence_days": 3,
            "persistence_average_yi": 18500,
            "status": "强势延续",
        }, {})
        self.assertTrue(review["supporting"])
        self.assertTrue(review["invalidators"])
        self.assertFalse(any("一定" in item for item in review["supporting"]))

    def test_missing_data_is_not_converted_to_counter_evidence(self):
        review = build_decision_review({"status": "数据不足"}, {})
        self.assertFalse(review["counter"])
        self.assertTrue(review["missing"])


class TFilterOutcomeTests(unittest.TestCase):
    def test_observations_capture_day_one_and_factual_change(self):
        event = {"date": "2026-10-14", "trigger_price": 100.0}
        update_t_filter_event_outcome(event, 103.0, "2026-10-14 10:00")
        update_t_filter_event_outcome(event, 105.0, "2026-10-15 15:00")
        self.assertEqual(event["day_1_price"], 105.0)
        self.assertAlmostEqual(event["current_change_pct"], 5.0)
        self.assertEqual(event["outcome_status"], "观察中")

    def test_summary_does_not_claim_strategy_success(self):
        events = [
            {"trigger_price": 100, "current_price": 104, "outcome_status": "观察中"},
            {"trigger_price": 100, "current_price": 98, "outcome_status": "观察中"},
        ]
        summary = summarize_t_filter_outcomes(events)
        self.assertIn("观察中2条", summary)
        self.assertNotIn("策略有效", summary)


if __name__ == "__main__":
    unittest.main()
