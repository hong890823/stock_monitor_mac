#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""量比计算统一口径。

今日累计成交量 ÷ 20日均量，盘中按已交易时间折算。
避免上午10点只走了1/8交易时间就直接除，把正常节奏算成0.1x。

来源：Android 版 VolumeRatio.kt
"""

from typing import List, Dict, Optional

# ─── Constants ────────────────────────────────────────────────────────────────

TRADING_MINUTES = 240          # Full trading day (excluding lunch break)
MIN_SCALE_MINUTES = 5          # Minimum minutes for scaling; prevents crazy ratios at open
EXPAND_THRESHOLD = 1.3         # 放量阈值
SHRINK_THRESHOLD = 0.8         # 缩量阈值
MIN_BARS = 5                   # Minimum trading days required for 20-day average


# ─── Helpers ──────────────────────────────────────────────────────────────────

def trading_minutes_elapsed(hour: int, minute: int) -> int:
    """Compute how many trading minutes have elapsed since 9:30.

    Returns 0 before 9:30, 240 after 15:00, and properly accounts
    for the lunch break (11:30-13:00).
    """
    if hour < 9 or (hour == 9 and minute < 30):
        return 0

    if hour >= 15:
        return 240

    total = (hour * 60 + minute) - (9 * 60 + 30)   # minutes since 9:30

    if total <= 120:        # 9:30 – 11:30
        return total
    elif total < 150:       # 11:30 – 13:00  (lunch break)
        return 120
    else:                   # 13:00 – 15:00
        return total - 30   # subtract 90-min lunch window minus 60 already counted


# ─── VolumeRatio ──────────────────────────────────────────────────────────────

class VolumeRatio:
    """Unified volume-ratio calculator."""

    # ------------------------------------------------------------------
    @staticmethod
    def is_intraday(elapsed_trading_minutes: int) -> bool:
        """Return True if the market is currently in an intraday session."""
        return 1 <= elapsed_trading_minutes < TRADING_MINUTES

    # ------------------------------------------------------------------
    @staticmethod
    def of(
        today_volume: float,
        avg_volume_20: float,
        elapsed_trading_minutes: int,
    ) -> Optional[float]:
        """Calculate the volume ratio.

        During intraday trading the volume is scaled up to a full day
        equivalent before dividing, so the ratio is comparable to
        end-of-day values even early in the session.

        Returns None when the ratio cannot be computed.
        """
        if elapsed_trading_minutes <= 0:
            return None

        if today_volume <= 0 or avg_volume_20 <= 0:
            return None

        if 1 <= elapsed_trading_minutes < TRADING_MINUTES:
            scale = TRADING_MINUTES / max(elapsed_trading_minutes, MIN_SCALE_MINUTES)
            scaled_volume = today_volume * scale
        else:
            scaled_volume = today_volume

        return scaled_volume / avg_volume_20

    # ------------------------------------------------------------------
    @staticmethod
    def average_volume_excluding_today(
        bars: List[Dict],
        today: Optional[str] = None,
    ) -> Optional[float]:
        """Mean volume of the last 20 bars, excluding today's bar if present.

        Parameters
        ----------
        bars : list of dicts
            Each dict must contain ``'date'`` (str) and ``'volume'`` (numeric).
            Bars should be sorted ascending by date.
        today : str, optional
            If the last bar's ``'date'`` matches *today*, it is excluded.

        Returns None when fewer than MIN_BARS valid bars remain.
        """
        if not bars:
            return None

        work = list(bars)
        if today and work and work[-1].get("date") == today:
            work = work[:-1]

        return VolumeRatio.average_volume(work)

    # ------------------------------------------------------------------
    @staticmethod
    def average_volume(bars: List[Dict]) -> Optional[float]:
        """Mean volume of the last 20 bars (volume > 0 only).

        Parameters
        ----------
        bars : list of dicts
            Each dict must contain ``'volume'`` (numeric).

        Returns None when fewer than MIN_BARS valid bars remain.
        """
        if not bars:
            return None

        tail = bars[-20:]
        volumes = [b["volume"] for b in tail if b.get("volume", 0) > 0]

        if len(volumes) < MIN_BARS:
            return None

        return sum(volumes) / len(volumes)
