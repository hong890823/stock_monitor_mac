#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""板块技术面的口径测试。

核心是回答「个股弱是板块带的还是自己的」—— 只比「个股 vs 沪指」答不了这个：
跟着板块一起弱的是 β，连自己板块都跑输的才是 α。差值 = 个股 rs − 板块 rs
（两者都减过沪指，相减即为个股相对自己板块）。
"""

import unittest
from datetime import datetime, timedelta

import pandas as pd

from sector_technicals import (
    MIN_PROXY_ROWS,
    build_proxy_bars,
    build_sector_technicals,
    split_vs_own_sector,
    summarize,
)


_DAY_ZERO = datetime(2026, 1, 1)


def day_strings(n, offset=0):
    """n 个连续、合法的 YYYY-MM-DD（不用 2026-01-60 这种无效日期）。"""
    return [(_DAY_ZERO + timedelta(days=offset + i)).strftime("%Y-%m-%d") for i in range(n)]


def bars(closes, dates=None, volumes=None, name="a"):
    n = len(closes)
    dates = dates or day_strings(n)
    volumes = volumes or [1000.0] * n
    return pd.DataFrame({
        "date": dates,
        "open": closes,
        "high": [c * 1.01 for c in closes],
        "low": [c * 0.99 for c in closes],
        "close": closes,
        "volume": volumes,
    })


class BuildProxyBarsTests(unittest.TestCase):
    def test_equal_weight_not_price_weighted(self):
        """高价股不得主导指数 —— 先各自归一到 100 再平均。"""
        # 甲从 100 涨到 110（+10%），乙从 1 涨到 1.05（+5%）
        a = bars([100.0] * (MIN_PROXY_ROWS - 1) + [110.0])
        b = bars([1.0] * (MIN_PROXY_ROWS - 1) + [1.05])
        proxy = build_proxy_bars([a, b])
        self.assertIsNotNone(proxy)
        # 等权：(110 + 105)/2 = 107.5；若按价格加权会接近 110
        self.assertAlmostEqual(float(proxy["close"].iloc[-1]), 107.5, places=6)
        self.assertAlmostEqual(float(proxy["close"].iloc[0]), 100.0, places=6)

    def test_returns_none_when_too_few_constituents(self):
        self.assertIsNone(build_proxy_bars([]))
        self.assertIsNone(build_proxy_bars([bars([10.0] * 5)]))   # 行数不足

    def test_stale_constituent_is_dropped_not_fatal(self):
        """一根停在远古的日线不该让整条指数合成失败。

        2026-10-12 实测：汽车制造的 sh600093 返回 2021-11~2022-06 的旧区间，
        和其余 7 只日期交集 0 —— 严格内连接会把整个板块算成 None。
        """
        good = bars([10.0] * MIN_PROXY_ROWS)
        stale = bars([99.0] * MIN_PROXY_ROWS,
                     dates=day_strings(MIN_PROXY_ROWS, offset=-1500))
        proxy = build_proxy_bars([good, stale])
        self.assertIsNotNone(proxy)
        self.assertEqual(len(proxy), MIN_PROXY_ROWS)
        # 只用 good 的归一序列：全程 100（stale 被丢掉，没参与均值）
        self.assertAlmostEqual(float(proxy["close"].iloc[0]), 100.0)

    def test_missing_day_is_skipped_not_zero_filled(self):
        """某只停牌一天，那天只在其余成分股上取均值，不填 0 也不前值。"""
        dates = day_strings(MIN_PROXY_ROWS + 1)   # 61 个唯一日
        a = bars([10.0] * (MIN_PROXY_ROWS + 1), dates=dates)
        # 乙在最后一天停牌（少一根），但总行数仍 ≥60 才会被采纳
        b = bars([10.0] * MIN_PROXY_ROWS, dates=dates[:-1])
        proxy = build_proxy_bars([a, b])
        self.assertIsNotNone(proxy)
        self.assertEqual(len(proxy), MIN_PROXY_ROWS + 1)   # 并集，不丢那天
        self.assertAlmostEqual(float(proxy["close"].iloc[-1]), 100.0)  # 只有甲参与

    def test_volume_is_summed_not_averaged(self):
        a = bars([10.0] * MIN_PROXY_ROWS, volumes=[100.0] * MIN_PROXY_ROWS)
        b = bars([10.0] * MIN_PROXY_ROWS, volumes=[300.0] * MIN_PROXY_ROWS)
        proxy = build_proxy_bars([a, b])
        self.assertAlmostEqual(float(proxy["volume"].iloc[0]), 400.0)

    def test_ignores_too_short_history(self):
        """IPO 不久的成分股不该把整条指数截短。"""
        a = bars([10.0] * MIN_PROXY_ROWS)
        short = bars([99.0] * 10)
        proxy = build_proxy_bars([a, short])
        self.assertIsNotNone(proxy)
        self.assertEqual(len(proxy), MIN_PROXY_ROWS)

    def test_accepts_chinese_column_names(self):
        df = pd.DataFrame({
            "日期": day_strings(MIN_PROXY_ROWS),
            "开盘": [10.0] * MIN_PROXY_ROWS,
            "最高": [10.5] * MIN_PROXY_ROWS,
            "最低": [9.5] * MIN_PROXY_ROWS,
            "收盘": [10.0] * MIN_PROXY_ROWS,
            "成交量": [50.0] * MIN_PROXY_ROWS,
        })
        proxy = build_proxy_bars([df])
        self.assertIsNotNone(proxy)
        self.assertAlmostEqual(float(proxy["close"].iloc[0]), 100.0)


class SummarizeTests(unittest.TestCase):
    def test_extracts_same_fields_as_stock_analysis(self):
        """字段要和个股那套一致，否则「个股 vs 板块」没法直接相减。"""
        got = summarize({
            "price": 10.0, "ma20": 11.0, "price_change": -1.5, "score": -3,
            "relative_strength": -5.0, "volume_ratio": 0.8,
            "volatility": {"atr_pct": 3.2},
            "entry_structure": {"trend_weak": True, "ma20_slope": "下行", "state": "NO_STRUCTURE"},
        })
        self.assertFalse(got["above_ma20"])
        self.assertTrue(got["trend_weak"])
        self.assertAlmostEqual(got["relative_strength"], -5.0)
        self.assertAlmostEqual(got["atr_pct"], 3.2)
        self.assertEqual(got["ma20_slope"], "下行")

    def test_empty_result_is_safe(self):
        got = summarize(None)
        self.assertIsNone(got["above_ma20"])
        self.assertFalse(got["trend_weak"])

    def test_above_ma20_is_none_when_data_missing(self):
        self.assertIsNone(summarize({})["above_ma20"])


class SectorAggregationTests(unittest.TestCase):
    def _sectors(self):
        return {"by_sector": [
            {"name": "汽车制造", "count": 2, "change": -2.0, "weight_pct": 50.0,
             "stocks": ["甲", "乙"]},
            {"name": "玻璃行业", "count": 1, "change": -1.0, "weight_pct": 50.0,
             "stocks": ["丙"]},
        ]}

    def test_counts_broken_and_weak_sectors(self):
        tech = {
            "汽车制造": summarize({"price": 9, "ma20": 10, "relative_strength": -6.0,
                              "entry_structure": {"trend_weak": True}}),
            "玻璃行业": summarize({"price": 11, "ma20": 10, "relative_strength": 3.0,
                              "entry_structure": {"trend_weak": False}}),
        }
        out = build_sector_technicals(self._sectors(), tech)
        self.assertEqual(out["covered"], 2)
        self.assertEqual(out["below_ma20"], 1)
        self.assertEqual(out["trend_weak"], 1)
        self.assertEqual(out["rs_weak"], 1)
        self.assertEqual(out["rs_strong"], 1)

    def test_missing_sector_tech_is_reported_not_guessed(self):
        out = build_sector_technicals(self._sectors(), {})
        self.assertEqual(out["covered"], 0)
        self.assertEqual(sorted(out["missing"]), ["汽车制造", "玻璃行业"])
        self.assertEqual(out["below_ma20"], 0)


class SplitVsOwnSectorTests(unittest.TestCase):
    def test_lag_and_lead_are_relative_to_own_sector(self):
        """个股 − 自己板块：跟着板块一起弱是 β（inline），连板块都跑输才是 α（lag）。"""
        sector_tech = {
            "汽车制造": {"relative_strength": -6.0},
            "玻璃行业": {"relative_strength": 0.0},
        }
        stocks = [
            # 跟着板块一起弱 6pp → 差 0 → inline（β）
            {"code": "a", "name": "跟跌", "sector": "汽车制造", "rs": -6.0},
            # 比板块还弱 8pp → lag（α，个股自身问题）
            {"code": "b", "name": "超跌于板块", "sector": "汽车制造", "rs": -14.0},
            # 板块中性但它强 5pp → lead
            {"code": "c", "name": "强于板块", "sector": "玻璃行业", "rs": 5.0},
        ]
        out = split_vs_own_sector(stocks, sector_tech)
        self.assertEqual([i["name"] for i in out["lag"]], ["超跌于板块"])
        self.assertEqual([i["name"] for i in out["inline"]], ["跟跌"])
        self.assertEqual([i["name"] for i in out["lead"]], ["强于板块"])
        self.assertAlmostEqual(out["lag"][0]["vs"], -8.0)

    def test_stocks_without_sector_tech_are_skipped_not_zeroed(self):
        out = split_vs_own_sector(
            [{"code": "a", "name": "甲", "sector": "未知板块", "rs": -10.0}],
            {},
        )
        self.assertEqual(out["lag"] + out["inline"] + out["lead"], [])

    def test_none_rs_is_skipped(self):
        out = split_vs_own_sector(
            [{"code": "a", "name": "甲", "sector": "s", "rs": None}],
            {"s": {"relative_strength": -5.0}},
        )
        self.assertEqual(out["lag"] + out["inline"] + out["lead"], [])


if __name__ == "__main__":
    unittest.main()
