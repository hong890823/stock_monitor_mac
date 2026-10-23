#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import unittest
from datetime import datetime

from candidate_state_policy import (
    CandidateReviewInput,
    CandidateState,
    CandidateStrategy,
    advance_candidate,
    required_confirmation_rounds,
    route_source,
)


def review(at, ordinary=True, trend_weak=False, **overrides):
    values = dict(
        at=at,
        ordinary_gates_passed=ordinary,
        trend_weak=trend_weak,
        price_trend_valid=True,
        fresh_sample=True,
        sealed=False,
        limit_streak=1,
        environment_blocked=False,
        cumulative_volume_increase=False,
    )
    values.update(overrides)
    return CandidateReviewInput(**values)


def record(**overrides):
    values = {
        "state": CandidateState.OBSERVING.value,
        "strategy": CandidateStrategy.PRE_BREAKOUT.value,
        "confirmation_rounds": 0,
        "confirmation_started_at": None,
        "last_confirmation_at": None,
        "observation_dates": [],
        "weak_dates": [],
        "continuous_observation": False,
        "limit_up_blocked": False,
    }
    values.update(overrides)
    return values


class CandidateStatePolicyTests(unittest.TestCase):
    def test_rounds_follow_limit_streak(self):
        self.assertEqual(required_confirmation_rounds(1), 2)
        self.assertEqual(required_confirmation_rounds(2), 2)
        self.assertEqual(required_confirmation_rounds(3), 3)
        self.assertEqual(required_confirmation_rounds(9), 3)

    def test_source_routes_to_three_strategy_tracks(self):
        self.assertEqual(route_source("strict"), CandidateStrategy.PRE_BREAKOUT)
        self.assertEqual(route_source("five_waiting"), CandidateStrategy.PRE_BREAKOUT)
        self.assertEqual(route_source("strong"), CandidateStrategy.LEADER)
        self.assertEqual(
            route_source("limit", limit_candidate=True),
            CandidateStrategy.LIMIT_UP_CONTINUATION,
        )

    def test_two_round_confirmation_uses_time_separated_samples(self):
        state = advance_candidate(
            record(),
            review(datetime(2026, 10, 23, 10, 0)),
        )
        self.assertEqual(state.record["confirmation_rounds"], 1)
        self.assertEqual(state.record["state"], CandidateState.NEAR_ENTRY.value)

        too_close = advance_candidate(
            state.record,
            review(datetime(2026, 10, 23, 10, 10)),
        )
        self.assertEqual(too_close.record["confirmation_rounds"], 1)
        self.assertTrue(too_close.blockers)

        confirmed = advance_candidate(
            too_close.record,
            review(datetime(2026, 10, 23, 10, 15)),
        )
        self.assertEqual(confirmed.record["confirmation_rounds"], 2)
        self.assertEqual(confirmed.record["state"], CandidateState.BUY_CANDIDATE.value)

    def test_confirmation_window_expires_after_45_minutes(self):
        started = record(
            state=CandidateState.NEAR_ENTRY.value,
            confirmation_rounds=1,
            confirmation_started_at=datetime(2026, 10, 23, 10, 0).isoformat(),
            last_confirmation_at=datetime(2026, 10, 23, 10, 0).isoformat(),
        )
        state = advance_candidate(
            started,
            review(datetime(2026, 10, 23, 11, 1)),
        )
        self.assertEqual(state.record["confirmation_rounds"], 1)
        self.assertEqual(state.record["state"], CandidateState.NEAR_ENTRY.value)
        self.assertEqual(
            state.record["confirmation_started_at"],
            datetime(2026, 10, 23, 11, 1).isoformat(),
        )

    def test_three_board_candidate_requires_three_rounds(self):
        current = record()
        for minute in (0, 20, 40):
            current = advance_candidate(
                current,
                review(datetime(2026, 10, 23, 10, minute), limit_streak=3),
            ).record
        self.assertEqual(current["confirmation_rounds"], 3)
        self.assertEqual(current["state"], CandidateState.BUY_CANDIDATE.value)

    def test_cumulative_volume_alone_cannot_confirm(self):
        state = advance_candidate(
            record(),
            review(
                datetime(2026, 10, 23, 10, 0),
                price_trend_valid=False,
                cumulative_volume_increase=True,
            ),
        )
        self.assertEqual(state.record["confirmation_rounds"], 0)
        self.assertEqual(state.record["state"], CandidateState.OBSERVING.value)
        self.assertTrue(any("价格" in reason for reason in state.blockers))

    def test_sealed_candidate_observes_and_reseal_resets_confirmation(self):
        pending = record(
            state=CandidateState.NEAR_ENTRY.value,
            confirmation_rounds=1,
            confirmation_started_at=datetime(2026, 10, 23, 10, 0).isoformat(),
            last_confirmation_at=datetime(2026, 10, 23, 10, 0).isoformat(),
        )
        state = advance_candidate(
            pending,
            review(datetime(2026, 10, 23, 10, 30), sealed=True),
        )
        self.assertEqual(state.record["state"], CandidateState.OBSERVING.value)
        self.assertEqual(state.record["confirmation_rounds"], 0)
        self.assertTrue(state.record["continuous_observation"])
        self.assertTrue(state.record["limit_up_blocked"])
        self.assertEqual(state.blockers, ())

    def test_three_distinct_weak_days_eliminate(self):
        current = record()
        for day in (21, 22, 23):
            current = advance_candidate(
                current,
                review(datetime(2026, 10, day, 15), trend_weak=True),
            ).record
        self.assertEqual(current["state"], CandidateState.ELIMINATED.value)

    def test_ten_unconfirmed_observation_days_eliminate(self):
        current = record(
            observation_dates=[f"2026-10-{day:02d}" for day in range(11, 21)]
        )
        state = advance_candidate(
            current,
            review(datetime(2026, 10, 21, 15), ordinary=False),
        )
        self.assertEqual(state.record["state"], CandidateState.ELIMINATED.value)

    def test_missing_or_non_fresh_sample_does_not_mutate(self):
        original = record()
        state = advance_candidate(
            original,
            review(datetime(2026, 10, 23, 15), fresh_sample=False),
        )
        self.assertEqual(state.record, original)
        self.assertTrue(state.blockers)


if __name__ == "__main__":
    unittest.main()
