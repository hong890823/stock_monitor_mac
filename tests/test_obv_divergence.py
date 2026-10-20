#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OBV 量价背离判定测试。

锁住九条关键行为：
1. 量价同步（价创新高且 OBV 抬升）不加分；
2. 顶背离（价创新高但 OBV 高点下移）-1，且对照数字可核对；
3. 底背离（价创新低但 OBV 低点抬升）+1；
4. 进行中高点 + OBV 同步 → 量价同步；
5. 进行中高点 + OBV 未跟上 → 顶背离（用户核对的实时场景）；
6. 未创阶段新高/新低 → NEUTRAL，不加分；
7. 顶底背离同时出现 → CONFLICT，净 0；
8. 数据不足 → INSUFFICIENT；
9. 平盘日 OBV 不变（经典定义）。
"""

import unittest

from obv_divergence import (
    DivergenceState,
    compute_obv,
    evaluate,
)


def _pad(series, target=32, fill=None):
    """把序列前置填充到 target 根（不足时用 fill 或首值）。"""
    if len(series) >= target:
        return list(series)
    head = (fill if fill is not None else series[0])
    return [head] * (target - len(series)) + list(series)


class TestComputeObv(unittest.TestCase):
    def test_flat_day_keeps_obv(self):
        # 涨加量、跌减量、平盘不变（经典定义）
        closes = [10.0, 10.5, 10.5, 10.2]
        volumes = [0.0, 100.0, 50.0, 80.0]
        obv = compute_obv(closes, volumes)
        self.assertEqual(obv, [0.0, 100.0, 100.0, 20.0])


class TestDivergence(unittest.TestCase):
    def test_sync_when_price_and_obv_both_make_new_high(self):
        # 单边放量上涨：价与 OBV 同步新高
        n = 32
        closes = [10 + 0.15 * i for i in range(n)]
        highs = [c + 0.05 for c in closes]
        lows = [c - 0.05 for c in closes]
        volumes = [100 + 5 * i for i in range(n)]
        result = evaluate(closes, highs, lows, volumes)
        self.assertEqual(result.state, DivergenceState.SYNC)
        self.assertEqual(result.score_delta, 0)
        self.assertTrue(result.obv_confirmed_new_high)

    def test_top_divergence_scores_minus_one(self):
        # 两波上涨，第二波价创新高但缩量（OBV 高点下移）
        closes = _pad([
            10, 10.5, 11, 11.5, 12, 12.5, 13, 13.2, 13.0, 12.5,
            12.0, 11.5, 11.2, 11.0, 11.1, 11.3, 11.5,
            11.8, 12.2, 12.6, 13.1, 13.6, 14.1, 14.5, 14.8,
            15.0, 15.05, 15.1, 15.08, 15.12, 15.11, 15.13,
        ])
        volumes = _pad([
            200, 300, 500, 800, 1200, 1500, 1800, 900, 1000, 1100,
            1300, 1600, 1400, 800, 500, 450, 400,
            350, 320, 300, 280, 260, 240, 220, 200,
            180, 170, 160, 150, 140, 130, 120,
        ], fill=200)
        highs = [c + 0.05 for c in closes]
        lows = [c - 0.05 for c in closes]
        result = evaluate(closes, highs, lows, volumes)
        self.assertEqual(result.state, DivergenceState.TOP)
        self.assertEqual(result.score_delta, -1)
        self.assertFalse(result.obv_confirmed_new_high)
        # 对照数字必须能核对：后高 > 前高，但后点 OBV < 前点 OBV
        self.assertIsNotNone(result.prior)
        self.assertIsNotNone(result.current)
        self.assertGreater(result.current.price, result.prior.price)
        self.assertLess(result.current.obv, result.prior.obv)

    def test_bottom_divergence_scores_plus_one(self):
        # 两波下跌，第二波创新低但 OBV 低点抬升（出逃放缓）
        closes = _pad([
            15, 14.5, 14, 13.5, 13, 12.5, 12, 11.8, 12.0, 12.5,
            13.0, 13.5, 13.8, 14.0, 13.9, 13.7, 13.5,
            13.2, 12.8, 12.4, 11.9, 11.4, 10.9, 10.5, 10.2,
            10.0, 9.95, 9.9, 9.92, 9.88, 9.89, 9.87,
        ])
        volumes = _pad([
            200, 300, 500, 800, 1200, 1500, 1800, 900, 1000, 1100,
            1300, 1600, 1400, 800, 500, 450, 400,
            350, 320, 300, 280, 260, 240, 220, 200,
            180, 170, 160, 150, 140, 130, 120,
        ], fill=200)
        highs = [c + 0.05 for c in closes]
        lows = [c - 0.05 for c in closes]
        result = evaluate(closes, highs, lows, volumes)
        self.assertEqual(result.state, DivergenceState.BOTTOM)
        self.assertEqual(result.score_delta, 1)
        self.assertIsNotNone(result.prior)
        self.assertIsNotNone(result.current)
        self.assertLess(result.current.price, result.prior.price)
        self.assertGreater(result.current.obv, result.prior.obv)

    def test_forming_high_with_obv_confirm_is_sync(self):
        # 进行中高点（末根创窗口新高）+ OBV 同步新高 → 量价同步
        n = 32
        closes = [10 + 0.15 * i for i in range(n)]
        highs = [c + 0.05 for c in closes]
        lows = [c - 0.05 for c in closes]
        volumes = [100 + 5 * i for i in range(n)]
        result = evaluate(closes, highs, lows, volumes, current_price=closes[-1] + 0.2)
        self.assertEqual(result.state, DivergenceState.SYNC)
        self.assertTrue(result.forming)
        self.assertEqual(result.score_delta, 0)

    def test_forming_high_without_obv_confirm_is_top_divergence(self):
        # 实时场景：股价创本轮新高但 OBV 未跟上 → 顶背离（forming）
        closes = _pad([
            10, 10.5, 11, 11.5, 12, 12.5, 13, 13.2, 13.0, 12.5,
            12.0, 11.5, 11.2, 11.0, 11.1, 11.3, 11.5,
            11.8, 12.2, 12.6, 13.1, 13.6, 14.1, 14.5, 14.8,
            15.0, 15.05, 15.1, 15.08, 15.12, 15.11, 15.13,
        ])
        volumes = _pad([
            200, 300, 500, 800, 1200, 1500, 1800, 900, 1000, 1100,
            1300, 1600, 1400, 800, 500, 450, 400,
            350, 320, 300, 280, 260, 240, 220, 200,
            180, 170, 160, 150, 140, 130, 120,
        ], fill=200)
        highs = [c + 0.05 for c in closes]
        lows = [c - 0.05 for c in closes]
        result = evaluate(closes, highs, lows, volumes, current_price=15.4)
        self.assertEqual(result.state, DivergenceState.TOP)
        self.assertTrue(result.forming)
        self.assertEqual(result.score_delta, -1)
        self.assertIn("高点进行中", result.reason)

    def test_no_new_high_is_neutral(self):
        # 收敛三角：高点渐低、低点渐高，未创阶段新高/新低 → 不构成背离前提
        closes = [
            10.0, 10.3, 10.7, 11.1, 11.5, 11.8, 12.0, 11.7, 11.3, 10.9,
            10.6, 10.3, 10.1, 10.0, 10.2, 10.5, 10.8, 11.0, 11.2, 11.4,
            11.5, 11.3, 11.1, 10.9, 10.8, 10.7, 10.6, 10.5, 10.6, 10.7,
            10.8, 10.75,
        ]
        highs = [c + 0.05 for c in closes]
        lows = [c - 0.05 for c in closes]
        volumes = [100] * 32
        result = evaluate(closes, highs, lows, volumes)
        self.assertEqual(result.state, DivergenceState.NEUTRAL)
        self.assertEqual(result.score_delta, 0)
        self.assertIn("未创阶段新高/新低", result.reason)

    def test_conflict_when_top_and_bottom_both_diverge(self):
        # 宽幅震荡：高点抬升 OBV 走低 + 低点下移 OBV 抬升 → 信号打架
        closes = _pad([
            10, 11, 12, 13, 12, 11, 10, 9, 8, 9, 10,
            11, 12, 13, 14, 13, 12, 11, 10, 9, 8, 7,
            8, 9, 10, 11, 12, 13, 14, 15, 14, 13,
        ], target=32, fill=10)
        # 高位放量下跌、低位缩量上涨 → OBV 与价背向
        volumes = _pad([
            200, 400, 600, 800, 700, 600, 500, 400, 300, 200, 150,
            100, 80, 60, 40, 30, 25, 20, 15, 12, 10, 8,
            6, 5, 4, 3, 3, 2, 2, 1, 1, 1,
        ], target=32, fill=50)
        highs = [c + 0.05 for c in closes]
        lows = [c - 0.05 for c in closes]
        result = evaluate(closes, highs, lows, volumes)
        self.assertEqual(result.state, DivergenceState.CONFLICT)
        self.assertEqual(result.score_delta, 0)

    def test_insufficient_data(self):
        closes = [10.0] * 10
        result = evaluate(closes, closes, closes, [1.0] * 10)
        self.assertEqual(result.state, DivergenceState.INSUFFICIENT)
        self.assertEqual(result.score_delta, 0)


if __name__ == "__main__":
    unittest.main()
