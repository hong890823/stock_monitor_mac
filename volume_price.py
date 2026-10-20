#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""量价配合信号分析。

同一组量价组合在不同位置上的含义可以相反：
- 低位的「放量滞涨」多半是底部换手、吸筹初期
- 高位同样的形态则基本是借着量能出货

来源：Android 版 VolumePriceSignal.kt + VolumePrice.kt
"""

from enum import Enum
from typing import Dict, Optional

# ── 枚举 ──────────────────────────────────────────────

class Tone(Enum):
    """信号基调：决定 UI 颜色。"""
    UP = "UP"              # 红色，资金流入确认
    CAUTION = "CAUTION"    # 橙色，需要警惕
    NEUTRAL = "NEUTRAL"    # 灰色，中性提示


class VolumePriceSignal(Enum):
    """量价信号枚举。

    每个成员携带 (label, tone, icon) 三元组。
    """

    def __init__(self, label: str, tone: Tone, icon: str):
        self.label = label
        self.tone = tone
        self.icon = icon

    # ── 放量 ──
    EXPAND_UP          = ("放量上涨",       Tone.UP,      "🔥")
    EXPAND_UP_HIGH     = ("高位放量上涨",   Tone.CAUTION, "⚠️")
    EXPAND_FLAT        = ("放量滞涨",       Tone.NEUTRAL, "")
    EXPAND_FLAT_LOW    = ("低位放量滞涨",   Tone.NEUTRAL, "")
    EXPAND_FLAT_REBOUND = ("反弹遇阻滞涨",  Tone.NEUTRAL, "")
    EXPAND_FLAT_HIGH   = ("高位放量滞涨",   Tone.CAUTION, "⚠️")

    # ── 缩量 ──
    SHRINK_UP          = ("缩量上涨",       Tone.NEUTRAL, "")
    SHRINK_UP_LOW      = ("缩量弱反弹",     Tone.NEUTRAL, "")
    SHRINK_UP_HIGH     = ("缩量加速",       Tone.CAUTION, "⚠️")

    # ── 下跌 ──
    EXPAND_DOWN        = ("放量下跌",       Tone.NEUTRAL, "")
    EXPAND_CRASH       = ("放量急跌",       Tone.CAUTION, "⚠️")
    SHRINK_DOWN        = ("缩量阴跌",       Tone.NEUTRAL, "")
    SHRINK_CRASH       = ("缩量急跌",       Tone.CAUTION, "⚠️")

    # ── 无信号 ──
    NEUTRAL            = ("",               Tone.NEUTRAL, "")


# ── 量价分析 ──────────────────────────────────────────

class VolumePrice:
    """量价配合信号生成器。

    核心思想：同一组量价组合在不同价格位置上的含义可以相反。
    """

    FLAT_PCT: float = 0.5      # 涨跌幅在 ±0.5% 以内视为"持平"
    CRASH_PCT: float = 3.0     # 跌幅 ≥ 3% 视为"急跌"
    REBOUND_MIN: float = 0.10  # 从区间低点反弹 ≥ 10% 视为"已反弹"

    @staticmethod
    def of(
        volume_ratio: Optional[float],
        change_pct: float,
        price_context: Optional[Dict] = None,
    ) -> VolumePriceSignal:
        """根据量比、涨跌幅和位置上下文返回量价信号。

        Args:
            volume_ratio: 量比（今日成交量 / 近期均量）。None 表示缺失。
            change_pct:   涨跌幅百分比，例如 2.5 表示 +2.5%。
            price_context: 位置上下文字典，可选键：
                - 'location': 'LOW' / 'MID' / 'HIGH'
                - 'rebound_from_low': float, 例如 0.15 表示从低点反弹了 15%

        Returns:
            VolumePriceSignal 枚举成员。
        """
        if price_context is None:
            price_context = {}

        location: str = price_context.get("location", "MID")
        rebound: float = price_context.get("rebound_from_low", 0.0)

        is_high = location == "HIGH"
        is_low = location == "LOW"
        has_rebounded = rebound >= VolumePrice.REBOUND_MIN

        # ── 缺失或无效数据 → 无信号 ──
        if volume_ratio is None or abs(change_pct) < 0.01:
            return VolumePriceSignal.NEUTRAL

        # ── 判断量能状态 ──
        is_expand = volume_ratio >= 1.3
        is_shrink = volume_ratio <= 0.8

        # ── 涨 ──
        if change_pct > VolumePrice.FLAT_PCT:
            if is_expand:
                return (
                    VolumePriceSignal.EXPAND_UP_HIGH
                    if is_high
                    else VolumePriceSignal.EXPAND_UP
                )
            elif is_shrink:
                if is_high:
                    return VolumePriceSignal.SHRINK_UP_HIGH
                elif is_low:
                    return VolumePriceSignal.SHRINK_UP_LOW
                else:
                    return VolumePriceSignal.SHRINK_UP
            else:
                # 量能中性 + 上涨：按位置细分滞涨信号
                if is_low and not has_rebounded:
                    return VolumePriceSignal.EXPAND_FLAT_LOW
                elif is_low and has_rebounded:
                    return VolumePriceSignal.EXPAND_FLAT_REBOUND
                elif is_high:
                    return VolumePriceSignal.EXPAND_FLAT_HIGH
                else:
                    return VolumePriceSignal.EXPAND_FLAT

        # ── 跌 ──
        if change_pct < -VolumePrice.FLAT_PCT:
            is_crash = change_pct <= -VolumePrice.CRASH_PCT
            if is_expand:
                return (
                    VolumePriceSignal.EXPAND_CRASH
                    if is_crash
                    else VolumePriceSignal.EXPAND_DOWN
                )
            elif is_shrink:
                return (
                    VolumePriceSignal.SHRINK_CRASH
                    if is_crash
                    else VolumePriceSignal.SHRINK_DOWN
                )
            else:
                return VolumePriceSignal.NEUTRAL

        # ── 持平（涨跌幅在 ±FLAT_PCT 以内）──
        if is_expand:
            if is_low and not has_rebounded:
                return VolumePriceSignal.EXPAND_FLAT_LOW
            elif is_low and has_rebounded:
                return VolumePriceSignal.EXPAND_FLAT_REBOUND
            elif is_high:
                return VolumePriceSignal.EXPAND_FLAT_HIGH
            else:
                return VolumePriceSignal.EXPAND_FLAT

        return VolumePriceSignal.NEUTRAL


# ── 辅助函数 ──────────────────────────────────────────

def tag(signal: VolumePriceSignal) -> str:
    """返回信号的显示字符串，例如 '🔥放量上涨'、'⚠️高位放量滞涨'。

    对 NEUTRAL 信号返回空字符串。
    """
    if signal == VolumePriceSignal.NEUTRAL:
        return ""
    if signal.icon:
        return f"{signal.icon}{signal.label}"
    return signal.label
