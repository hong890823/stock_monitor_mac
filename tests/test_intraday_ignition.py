#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""盘中点火（量能突增 + 平台突破）测试。"""

import unittest

from intraday_ignition import IgnitionSample, IgnitionTracker

T0 = 1_000_000.0
STEP = 30.0            # 采样间隔
BASE_STEP_VOL = 3000   # 基线速率：3000 股 / 30 秒
SURGE_STEP_VOL = 12000  # 点火速率：4 倍于基线


def sample(ts, price, cum_volume, code="sz002185", name="华天科技"):
    return IgnitionSample(
        code=code, name=name, price=price,
        cumulative_volume=cum_volume, timestamp=ts,
    )


class IgnitionTrackerTests(unittest.TestCase):
    def _tracker(self):
        # 短窗 5 分钟、基线 20 分钟，量比 3 倍——与默认同比例，测试跑得快
        return IgnitionTracker(short_window=300, baseline_window=1200, volume_ratio=3.0)

    def _feed_quiet_baseline(self, tracker, day, platform_top=None):
        """喂满基线窗的安静盘面；platform_top 在窗内中段垫一个平台上沿。"""
        cum = 0.0
        ts = T0
        for i in range(60):  # 30 分钟
            price = platform_top if (platform_top is not None and i == 50) else 14.10
            tracker.update(sample(ts, price, cum), day)
            cum += BASE_STEP_VOL
            ts += STEP
        return ts, cum

    def _ignite(self, tracker, day, ts, cum, price, steps=20, code="sz002185"):
        """持续点火速率喂若干笔，返回首个 alert（可能 None）。"""
        for _ in range(steps):
            cum += SURGE_STEP_VOL
            alert = tracker.update(sample(ts, price, cum, code=code), day)
            ts += STEP
            if alert is not None:
                return alert, ts, cum
        return None, ts, cum

    def test_ignition_fires_on_volume_surge_and_platform_break(self):
        tracker = self._tracker()
        day = "2026-10-22"
        ts, cum = self._feed_quiet_baseline(tracker, day, platform_top=14.15)
        alert, _, _ = self._ignite(tracker, day, ts, cum, price=14.40)
        self.assertIsNotNone(alert)
        self.assertGreaterEqual(alert.volume_ratio, 3.0)
        self.assertAlmostEqual(alert.platform_top, 14.15)
        self.assertEqual(alert.name, "华天科技")

    def test_fires_only_once_per_code_per_day(self):
        tracker = self._tracker()
        day = "2026-10-22"
        ts, cum = self._feed_quiet_baseline(tracker, day)
        fired = 0
        for _ in range(40):
            cum += SURGE_STEP_VOL
            if tracker.update(sample(ts, 14.40, cum), day) is not None:
                fired += 1
            ts += STEP
        self.assertEqual(fired, 1)

    def test_volume_surge_without_platform_break_stays_silent(self):
        tracker = self._tracker()
        day = "2026-10-22"
        ts, cum = self._feed_quiet_baseline(tracker, day, platform_top=14.20)
        alert, _, _ = self._ignite(tracker, day, ts, cum, price=14.18)
        self.assertIsNone(alert)

    def test_platform_break_without_volume_surge_stays_silent(self):
        tracker = self._tracker()
        day = "2026-10-22"
        ts, cum = self._feed_quiet_baseline(tracker, day, platform_top=14.15)
        # 价格破平台但量能维持基线速率
        for _ in range(20):
            cum += BASE_STEP_VOL
            self.assertIsNone(tracker.update(sample(ts, 14.40, cum), day))
            ts += STEP

    def test_baseline_window_must_fill_before_judging(self):
        tracker = self._tracker()
        day = "2026-10-22"
        # 只喂 6 分钟就要点火——基线不足，不判
        ts, cum = T0, 0.0
        for _ in range(12):
            cum += SURGE_STEP_VOL
            self.assertIsNone(tracker.update(sample(ts, 14.40, cum), day))
            ts += STEP

    def test_new_day_allows_next_ignition(self):
        tracker = self._tracker()
        ts, cum = self._feed_quiet_baseline(tracker, "2026-10-22")
        alert, ts, cum = self._ignite(tracker, "2026-10-22", ts, cum, price=14.40)
        self.assertIsNotNone(alert)
        # 跨日重置：再点火可以再报（累计量也随交易日清零）
        ts2, cum2 = self._feed_quiet_baseline(tracker, "2026-10-23")
        alert2, _, _ = self._ignite(tracker, "2026-10-23", ts2, cum2, price=15.00)
        self.assertIsNotNone(alert2)

    def test_codes_are_tracked_independently(self):
        tracker = self._tracker()
        day = "2026-10-22"
        ts, cum = self._feed_quiet_baseline(tracker, day)
        alert1, ts, cum = self._ignite(tracker, day, ts, cum, price=14.40, code="sz002185")
        alert2, _, _ = self._ignite(tracker, day, ts, cum, price=20.00, code="sz000100")
        # 另一只票没有基线，不报——说明状态确实按 code 隔离
        self.assertIsNotNone(alert1)
        self.assertIsNone(alert2)


if __name__ == "__main__":
    unittest.main()
