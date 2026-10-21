import json
import tempfile
import unittest
from pathlib import Path

from market_cycle_policy import MarketCyclePhase
from market_cycle_state import MarketCycleStateStore


class MarketCycleStateTests(unittest.TestCase):
    def test_finalized_days_advance_same_phase_age_and_reset_on_switch(self):
        store = MarketCycleStateStore(":memory:")

        first = store.observe(
            MarketCyclePhase.UP, "2026-10-12", finalized=True,
            evidence=("指数上涨且板块扩散",),
        )
        second = store.observe(
            MarketCyclePhase.UP, "2026-10-13", finalized=True,
            evidence=("指数继续上涨",),
        )
        switched = store.observe(
            MarketCyclePhase.DIVERGENCE, "2026-10-14", finalized=True,
            evidence=("放量滞涨",),
        )

        self.assertEqual(first.phase_age_days, 1)
        self.assertEqual(second.phase_age_days, 2)
        self.assertEqual(second.phase_start_date, "2026-10-12")
        self.assertEqual(switched.phase_age_days, 1)
        self.assertEqual(switched.phase_start_date, "2026-10-14")
        self.assertEqual(switched.divergence_age_days, 1)
        self.assertEqual(switched.down_age_days, 0)
        self.assertEqual(switched.last_evidence, ("放量滞涨",))
        self.assertEqual(switched.phase_switch_evidence, ("放量滞涨",))

    def test_intraday_observation_does_not_advance_or_switch_phase(self):
        store = MarketCycleStateStore(":memory:")
        store.observe(MarketCyclePhase.UP, "2026-10-13", finalized=True)

        result = store.observe(
            MarketCyclePhase.DOWN, "2026-10-14", finalized=False,
            evidence=("盘中跌破支撑",),
        )

        self.assertEqual(result.phase, MarketCyclePhase.UP)
        self.assertEqual(result.phase_age_days, 1)
        self.assertEqual(result.last_evidence, ("盘中跌破支撑",))

    def test_store_round_trip_keeps_state_and_intraday_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market_cycle_state.json"
            store = MarketCycleStateStore(str(path))
            store.observe(
                MarketCyclePhase.DOWN, "2026-10-14", finalized=True,
                evidence=("低点下移",),
            )
            store.observe(
                MarketCyclePhase.DOWN, "2026-10-15", finalized=False,
                evidence=("盘中支撑承压",),
            )

            restored = MarketCycleStateStore(str(path))
            result = restored.observe(
                MarketCyclePhase.DOWN, "2026-10-15", finalized=False,
                evidence=("盘中支撑承压",),
            )

            self.assertEqual(result.phase, MarketCyclePhase.DOWN)
            self.assertEqual(result.phase_age_days, 1)
            self.assertEqual(result.down_age_days, 1)
            self.assertEqual(result.last_evidence, ("盘中支撑承压",))
            self.assertEqual(result.phase_switch_evidence, ("低点下移",))
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["phase"], "DOWN")
            self.assertEqual(payload["last_advanced_date"], "2026-10-14")


if __name__ == "__main__":
    unittest.main()
