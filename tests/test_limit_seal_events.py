#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""持仓封板/炸板/回封事件测试。"""

import unittest

from limit_seal_events import SealEventTracker, SealTick, is_sealed


def tick(price=15.68, prev_close=14.25, ask1_price=0.0, ask1_vol=0.0, bid1_vol=500_000,
         code="sz002185"):
    return SealTick(
        code=code, name="华天科技", price=price, prev_close=prev_close,
        ask1_price=ask1_price, ask1_vol=ask1_vol, bid1_vol=bid1_vol,
    )


class IsSealedTests(unittest.TestCase):
    def test_sealed_when_at_limit_and_sell1_empty(self):
        self.assertTrue(is_sealed(tick()))

    def test_touch_without_seal_is_not_sealed(self):
        # 触板未封：卖一还挂着货
        self.assertFalse(is_sealed(tick(ask1_price=15.68, ask1_vol=300)))

    def test_below_limit_is_not_sealed(self):
        self.assertFalse(is_sealed(tick(price=15.60)))

    def test_sentinel_ask_price_does_not_confirm_seal(self):
        self.assertFalse(is_sealed(tick(ask1_price=-1.0, ask1_vol=0.0)))


class SealEventTrackerTests(unittest.TestCase):
    def test_seal_then_blowout_then_reseal_emits_each_transition_once(self):
        tracker = SealEventTracker()
        day = "2026-10-22"
        # 未封：无事件
        self.assertEqual(tracker.update(tick(price=15.00), day), [])
        # 封板：出一次「封板」
        events = tracker.update(tick(), day)
        self.assertEqual([e.kind for e in events], ["封板"])
        self.assertEqual(events[0].seal_volume, 500_000)
        # 连续封着：不再报
        self.assertEqual(tracker.update(tick(), day), [])
        # 炸板
        events = tracker.update(tick(price=15.60), day)
        self.assertEqual([e.kind for e in events], ["炸板"])
        self.assertEqual(events[0].blowout_count, 1)
        self.assertEqual(tracker.update(tick(price=15.60), day), [])
        # 回封
        events = tracker.update(tick(), day)
        self.assertEqual([e.kind for e in events], ["回封"])
        self.assertEqual(events[0].blowout_count, 1)

    def test_second_blowout_after_reseal_counts_up(self):
        tracker = SealEventTracker()
        day = "2026-10-22"
        tracker.update(tick(price=15.00), day)
        tracker.update(tick(), day)
        tracker.update(tick(price=15.60), day)
        tracker.update(tick(), day)
        events = tracker.update(tick(price=15.55), day)
        self.assertEqual([e.kind for e in events], ["炸板"])
        self.assertEqual(events[0].blowout_count, 2)

    def test_touch_unsealed_does_not_create_blowout(self):
        # 触板未封不是「炸板」——本来就没封上。
        tracker = SealEventTracker()
        day = "2026-10-22"
        tracker.update(tick(price=15.00), day)
        self.assertEqual(tracker.update(tick(ask1_price=15.68, ask1_vol=100), day), [])
        self.assertEqual(tracker.update(tick(price=15.60), day), [])

    def test_new_day_resets_state(self):
        tracker = SealEventTracker()
        tracker.update(tick(price=15.00), "2026-10-22")
        tracker.update(tick(), "2026-10-22")
        tracker.update(tick(price=15.60), "2026-10-22")
        # 跨日：从「已炸板」清零，再封板报「封板」而不是「回封」
        events = tracker.update(tick(), "2026-10-23")
        self.assertEqual([e.kind for e in events], ["封板"])

    def test_daily_event_cap_blocks_further_events(self):
        tracker = SealEventTracker(max_events_per_day=2)
        day = "2026-10-22"
        tracker.update(tick(price=15.00), day)
        tracker.update(tick(), day)               # 封板 1
        tracker.update(tick(price=15.60), day)    # 炸板 2
        self.assertEqual(tracker.update(tick(), day), [])  # 回封被上限吞掉


if __name__ == "__main__":
    unittest.main()
