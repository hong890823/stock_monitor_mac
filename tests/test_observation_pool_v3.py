#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from candidate_state_policy import CandidateState, CandidateStrategy
from capital_flow import CapitalFlowSnapshot
from capital_flow_policy import FundFlowTone
from observation_pool import ObservationPool


def legacy_payload():
    return {
        "version": 2,
        "records": {
            "sh600000": {
                "code": "sh600000",
                "name": "样本",
                "sources": ["strict"],
                "first_observed_date": "2026-10-20",
                "last_seen_date": "2026-10-22",
                "last_reviewed_date": None,
                "observation_days": 1,
                "status": "observing",
                "reason": "历史",
                "data_status": "已复核",
                "price": 10,
                "score": 1,
                "stop_price": None,
                "history": [],
            }
        },
    }


class ObservationPoolV3Tests(unittest.TestCase):
    @staticmethod
    def flow(day, net_ratio):
        date = f"2026-10-{day:02d}"
        return CapitalFlowSnapshot(
            key="sh600001", name="样本", kind="stock", quote_date=date,
            as_of=datetime.fromisoformat(f"{date}T15:00:00"),
            source="EastMoney.fflow.kline", scope="个股", session="daily",
            net_yi=net_ratio / 100, net_ratio_pct=net_ratio, turnover_yi=1.0,
        )

    def test_version_two_migrates_without_guessing_new_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pool.json"
            path.write_text(json.dumps(legacy_payload()), encoding="utf-8")
            pool = ObservationPool(path)
            self.assertIsNone(pool.load_error)
            record = pool.records["sh600000"]
            self.assertEqual(record["strategy"], CandidateStrategy.PRE_BREAKOUT.value)
            self.assertEqual(record["state"], CandidateState.OBSERVING.value)
            self.assertEqual(record["entry_destination"], "PRE_BREAKOUT / 启动前潜力")
            self.assertIsNone(record["seal_quality_score"])
            self.assertEqual(record["flow_window"], [])
            self.assertEqual(record["related_sectors"], [])
            self.assertEqual(record["risk_evidence"], [])

    def test_save_round_trip_uses_version_three(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pool.json"
            path.write_text(json.dumps(legacy_payload()), encoding="utf-8")
            pool = ObservationPool(path)
            pool._save(pool.records)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["version"], 3)
            self.assertFalse(payload["config"]["promotion_environment_gate_enabled"])
            loaded = ObservationPool(path)
            self.assertIsNone(loaded.load_error)
            self.assertEqual(loaded.records["sh600000"]["state"], "OBSERVING")

    def test_required_rounds_follow_limit_streak(self):
        self.assertEqual(ObservationPool.required_confirmation_rounds(2), 2)
        self.assertEqual(ObservationPool.required_confirmation_rounds(3), 3)

    def test_intraday_samples_confirm_after_two_time_separated_rounds(self):
        pool = ObservationPool()
        pool.records["sh600001"] = {
            "code": "sh600001", "name": "样本", "sources": ["strict"],
            "first_observed_date": "2026-10-20", "status": "observing",
            "strategy": CandidateStrategy.PRE_BREAKOUT.value,
            "state": CandidateState.OBSERVING.value,
            "observation_dates": [], "weak_dates": [], "history": [],
        }
        first = pool.observe_candidate_sample(
            "sh600001", datetime(2026, 10, 23, 10, 0),
            ordinary_gates_passed=True, price_trend_valid=True,
        )
        self.assertEqual(first["state"], CandidateState.NEAR_ENTRY.value)
        confirmed = pool.observe_candidate_sample(
            "sh600001", datetime(2026, 10, 23, 10, 20),
            ordinary_gates_passed=True, price_trend_valid=True,
        )
        self.assertEqual(confirmed["state"], CandidateState.BUY_CANDIDATE.value)
        self.assertEqual(confirmed["confirmation_rounds"], 2)

    def test_three_board_requires_three_samples_and_seal_resets(self):
        pool = ObservationPool()
        pool.records["sh600002"] = {
            "code": "sh600002", "name": "三板", "sources": ["limit"],
            "first_observed_date": "2026-10-20", "status": "observing",
            "strategy": CandidateStrategy.LIMIT_UP_CONTINUATION.value,
            "state": CandidateState.OBSERVING.value,
            "limit_streak": 3, "observation_dates": [], "weak_dates": [], "history": [],
        }
        current = pool.records["sh600002"]
        for minute in (0, 20, 40):
            current = pool.observe_candidate_sample(
                "sh600002", datetime(2026, 10, 23, 10, minute),
                ordinary_gates_passed=True, price_trend_valid=True,
                limit_streak=3,
            )
        self.assertEqual(current["state"], CandidateState.BUY_CANDIDATE.value)
        self.assertEqual(current["confirmation_rounds"], 3)

        sealed = pool.observe_candidate_sample(
            "sh600002", datetime(2026, 10, 23, 11, 0),
            ordinary_gates_passed=True, price_trend_valid=True,
            limit_streak=3, sealed=True,
        )
        self.assertEqual(sealed["state"], CandidateState.OBSERVING.value)
        self.assertEqual(sealed["confirmation_rounds"], 0)
        self.assertTrue(sealed["continuous_observation"])

    def test_environment_gate_off_records_evidence_without_blocking(self):
        pool = ObservationPool()
        pool.records["sh600003"] = {
            "code": "sh600003", "name": "样本", "sources": ["strict"],
            "first_observed_date": "2026-10-20", "status": "observing",
            "strategy": CandidateStrategy.PRE_BREAKOUT.value,
            "state": CandidateState.OBSERVING.value,
            "observation_dates": [], "weak_dates": [], "history": [],
        }
        result = pool.apply_promotion_environment(
            pool.records["sh600003"],
            {
                "shanghai_change_pct": -1.0,
                "limit_up_count": 10,
                "limit_down_count": 30,
            },
            enabled=False,
            at=datetime(2026, 10, 23, 15),
        )
        self.assertFalse(result["blocked"])
        self.assertTrue(result["evidence"])
        self.assertIsNone(result["reason"])

    def test_flow_window_keeps_five_dates_and_blocks_sustained_outflow(self):
        pool = ObservationPool()
        pool.records["sh600001"] = {
            "code": "sh600001", "name": "样本", "sources": ["strict"],
            "first_observed_date": "2026-10-20", "status": "observing",
            "strategy": CandidateStrategy.PRE_BREAKOUT.value,
            "state": CandidateState.OBSERVING.value,
            "observation_dates": [], "weak_dates": [], "history": [],
            "flow_window": [],
        }
        for day in (17, 18, 19, 20, 21, 22):
            result = pool.record_fund_flow_snapshot(
                "sh600001", self.flow(day, -4.0), change_pct=-1.0
            )
        self.assertEqual(len(result["flow_window"]), 5)
        self.assertEqual(result["flow_window"][0]["quote_date"], "2026-10-18")
        self.assertEqual(
            result["flow_window_assessment"]["tone"],
            FundFlowTone.SUSTAINED_OUTFLOW.value,
        )
        self.assertTrue(result["_flow_blocks_promotion"])


if __name__ == "__main__":
    unittest.main()
