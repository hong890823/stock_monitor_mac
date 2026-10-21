#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from datetime import datetime, timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from high_position_policy import HighPositionAssessment, HighPositionState
from observation_pool import ObservationPool
from trend_assessment import (
    MajorTrend,
    ShortTrend,
    TrendAssessment,
    TrendConflict,
    TrendEvidence,
)


def bars(kind="down", count=400):
    end = datetime(2026, 10, 14)
    dates = []
    while len(dates) < count:
        if end.weekday() < 5:
            dates.append(end.strftime("%Y-%m-%d"))
        end -= timedelta(days=1)
    dates.reverse()
    result = []
    for index, date in enumerate(dates):
        if kind == "down":
            close = 30 - index * 0.04
        elif kind == "bottom":
            close = 10 + max(0, index - (count - 25)) * 0.03
        else:
            close = 10 + index * 0.01
        result.append({
            "date": date,
            "open": close - 0.05,
            "high": close + 0.2,
            "low": close - 0.2,
            "close": close,
            "volume": 1000,
        })
    return result


def trend(major, short, conflict):
    return TrendAssessment(
        short_trend=short,
        major_trend=major,
        conflict=conflict,
        reasons=("统一趋势证据",),
        missing_data=(),
        evidence=TrendEvidence(),
        summary="统一趋势评估",
    )


class ObservationTrendGateTests(unittest.TestCase):
    def test_major_down_candidate_is_not_ready(self):
        pool = ObservationPool()

        pool.review(
            [{"code": "sh600000", "name": "示例", "source": "strict"}],
            datetime(2026, 10, 14, 18),
            histories={"sh600000": bars("down")},
            benchmark=bars("up"),
        )

        record = pool.records["sh600000"]
        self.assertEqual(record["status"], "observing")
        self.assertFalse(record["can_enter"])
        self.assertIn("大趋势向下", record["reason"])

    def test_bottoming_candidate_is_bottom_watch_not_normal_buy(self):
        bottoming = trend(
            MajorTrend.BOTTOMING,
            ShortTrend.REBOUND,
            TrendConflict.BOTTOM_REVERSAL_WATCH,
        )
        not_high = HighPositionAssessment(
            HighPositionState.NOT_HIGH,
            ("价格不在近60日高位",),
            (),
            (),
            "高",
            None,
            None,
        )
        pool = ObservationPool()

        with patch("observation_pool.assess_trend", return_value=bottoming), patch(
            "observation_pool.assess_high_position", return_value=not_high
        ):
            pool.review(
                [{"code": "sh600000", "name": "示例", "source": "strict"}],
                datetime(2026, 10, 14, 18),
                histories={"sh600000": bars("bottom")},
                benchmark=bars("up"),
            )

        record = pool.records["sh600000"]
        self.assertEqual(record["trend_mode"], "BOTTOM_REVERSAL_WATCH")
        self.assertFalse(record["can_enter"])


if __name__ == "__main__":
    unittest.main()
