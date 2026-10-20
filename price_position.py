#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""价格位置上下文：近 60 日区间位置 + 从区间低点反弹幅度。

同一组量价组合在不同位置上的含义可以相反：
- 低位的「放量滞涨」多半是底部换手、吸筹初期
- 高位同样的形态则基本是借着量能出货

来源：Android 版 PricePosition.kt
"""

from dataclasses import dataclass
from enum import Enum
from typing import List, Optional


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

WINDOW = 60          # trading days
LOW_MAX = 1 / 3      # position ≤ 1/3  → possible LOW
HIGH_MIN = 2 / 3     # position ≥ 2/3  → possible HIGH
HIGH_GAIN = 0.20     # need ≥20 % cumulative gain to confirm HIGH
LOW_GAIN = 0.0       # cumulative gain ≤ 0 % to confirm LOW
MIN_BARS = 20        # minimum bars required


# ---------------------------------------------------------------------------
# Enums / data classes
# ---------------------------------------------------------------------------

class PriceLocation(Enum):
    """价格在区间中的位置。"""
    LOW = "低位"
    MID = "中位"
    HIGH = "高位"


@dataclass(frozen=True)
class PriceContext:
    """价格位置 + 从区间低点的反弹幅度。"""
    location: PriceLocation
    rebound_from_low: float   # e.g. 0.25 = 从低点涨了 25 %


@dataclass(frozen=True)
class PriceWindow:
    """一个滚动窗口的价格区间。"""
    low: float
    high: float
    base_close: float         # 窗口第一天的收盘价

    # -- ratios ---------------------------------------------------------------

    def gain_ratio(self, price: float) -> float:
        """从窗口起始价到当前价的累计涨幅。"""
        if self.base_close <= 0:
            return 0.0
        return price / self.base_close - 1

    def rebound_ratio(self, price: float) -> float:
        """从区间最低价到当前价的反弹幅度。"""
        if self.low <= 0:
            return 0.0
        return price / self.low - 1

    # -- location helpers -----------------------------------------------------

    def location_of(self, price: float) -> Optional[PriceLocation]:
        """判断 *price* 在窗口区间中的位置。

        Returns:
            PriceLocation or None — price ≤ 0 或 high ≤ low 时返回 None。
        """
        if price <= 0 or self.high <= self.low:
            return None

        position = (price - self.low) / (self.high - self.low)
        gain = self.gain_ratio(price)

        if position >= HIGH_MIN and gain >= HIGH_GAIN:
            return PriceLocation.HIGH
        if position <= LOW_MAX and gain <= LOW_GAIN:
            return PriceLocation.LOW
        return PriceLocation.MID

    def context_of(self, price: float) -> Optional[PriceContext]:
        """返回完整的价格上下文（位置 + 反弹幅度），或 None。"""
        loc = self.location_of(price)
        if loc is None:
            return None
        return PriceContext(location=loc, rebound_from_low=self.rebound_ratio(price))


# ---------------------------------------------------------------------------
# PricePosition — main entry point
# ---------------------------------------------------------------------------

class PricePosition:
    """静态工厂：从日 K 线数据构建 PriceWindow。"""

    @staticmethod
    def of(bars: List[dict], today: Optional[str] = None) -> Optional[PriceWindow]:
        """从 K 线列表中提取最近 WINDOW 天的价格窗口。

        Args:
            bars: 日 K 线数据，每条形如
                  ``{'date': '2025-01-02', 'high': 12.5, 'low': 12.0, 'close': 12.3}``
                  **必须按日期升序排列**。
            today: 可选的日期字符串 (YYYY-MM-DD)。若提供且最后一条 bar 的
                   date 与之相等，则排除该条（当天盘中数据可能不完整）。

        Returns:
            PriceWindow or None — 数据不足或无效时返回 None。
        """
        if not bars:
            return None

        # 复制列表，避免修改调用方数据
        data = list(bars)

        # 排除当天（如果指定）
        if today is not None and data and data[-1].get("date") == today:
            data = data[:-1]

        # 取最近 WINDOW 条
        if len(data) > WINDOW:
            data = data[-WINDOW:]

        if len(data) < MIN_BARS:
            return None

        # 计算区间
        lo = float("inf")
        hi = float("-inf")
        base_close = data[0].get("close", 0)

        for bar in data:
            high = bar.get("high", 0)
            low = bar.get("low", 0)
            if high > hi:
                hi = high
            if low < lo:
                lo = low

        if hi <= lo or base_close <= 0:
            return None

        return PriceWindow(low=lo, high=hi, base_close=base_close)
