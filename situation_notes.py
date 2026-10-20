#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""今日情境分句：把指标读数翻成「这只股现在发生了什么」。

**只产出观察，不产出动作** —— 动作仍由各报告的决策分支给。
两者职责分开，避免情境分句悄悄改写决策结论。

为什么要这个（2026-10-12）：
- 持仓报告里 12 只挤进同一句模板、只换止损位数字；
- 个股技术面报告里两只差异很大的股，10 行里 6 行一字不差，
  全是「含义：DIF在DEA下方，动能偏弱」这类**指标词典释义**。
底层数据一直有区分度（相对强弱跨 17pp、波动率差 3.5 倍、量价从「放量上涨」
到「缩量急跌」都有），是输出没用上这些字段。

放在独立模块而不是 volume_price / stock_analysis / portfolio_analysis：
stock_analysis 是底层技术分析，portfolio_analysis 在它之上，两者都要用它，
放任何一边都会造成反向依赖。
"""

from __future__ import annotations

from typing import List, Optional

from volume_price import VolumePriceSignal

# 相对强弱的分档阈值，与 stock_analysis.signals 同口径
RS_STRONG_PP = 2.0
RS_WEAK_PP = -2.0

# 情境分句的取舍阈值：波动率高/低、浮亏逼近深套
ATR_PCT_HIGH = 5.0
ATR_PCT_LOW = 2.2
NEAR_DEEP_RATIO = 0.7

# 量价信号 → 一句话观察。按「今日发生了什么」分，而不是按涨跌分。
# 注意不要以「今日」开头 —— 调用方的行标签通常已经写了「今日：」。
_VP_NOTES = {
    VolumePriceSignal.EXPAND_UP: "放量上涨，承接转强",
    VolumePriceSignal.EXPAND_UP_HIGH: "高位放量上涨，警惕借量出货",
    VolumePriceSignal.EXPAND_FLAT: "放量滞涨，买盘接不动抛压",
    VolumePriceSignal.EXPAND_FLAT_LOW: "低位放量滞涨，多是底部换手，不算出货",
    VolumePriceSignal.EXPAND_FLAT_REBOUND: "反弹遇阻滞涨，上方抛压仍在",
    VolumePriceSignal.EXPAND_FLAT_HIGH: "高位放量滞涨，警惕借量出货",
    VolumePriceSignal.SHRINK_UP: "缩量上涨，反弹缺量能配合，不作企稳依据",
    VolumePriceSignal.SHRINK_UP_LOW: "缩量弱反弹，量能不足",
    VolumePriceSignal.SHRINK_UP_HIGH: "高位缩量加速，追高风险大",
    VolumePriceSignal.EXPAND_DOWN: "放量下跌，抛压仍在释放，不接刀",
    VolumePriceSignal.EXPAND_CRASH: "放量急跌，抛压集中释放，等承接出现",
    VolumePriceSignal.SHRINK_DOWN: "缩量阴跌，抛压未放大、买盘也缺",
    VolumePriceSignal.SHRINK_CRASH: "缩量急跌，恐慌盘有限但未见承接",
}


def _safe_float(value, default=0.0):
    try:
        if value is None:
            return default
        result = float(value)
        return default if result != result else result
    except (TypeError, ValueError):
        return default


def situation_notes(
    *,
    vp_signal: Optional[VolumePriceSignal] = None,
    relative_strength: Optional[float] = None,
    atr_pct: Optional[float] = None,
    floating_pct: Optional[float] = None,
    deep_threshold: Optional[float] = None,
    location: Optional[str] = None,
) -> List[str]:
    """今日情境分句，最多 3 条，按区分度排序。

    持仓报告多传 floating_pct / deep_threshold（会用到浮亏口径）；
    个股技术面分析是「无持仓假设」，不传这两个即可。
    """
    notes = []

    # ── 量价：最能区分「今日发生了什么」，优先级最高 ──
    if vp_signal is not None:
        note = _VP_NOTES.get(vp_signal)
        if note:
            notes.append(note)

    # ── 相对强弱：区分「跟跌大盘」和「超跌于大盘」──
    if relative_strength is not None:
        rs = _safe_float(relative_strength)
        if rs <= -8:
            notes.append(f"近20日跑输大盘{abs(rs):.1f}pp，明显偏弱，先分清是超跌还是基本面")
        elif rs <= RS_WEAK_PP:
            notes.append(f"近20日跑输大盘{abs(rs):.1f}pp")
        elif rs >= 8:
            notes.append(f"近20日强于大盘{rs:.1f}pp，回调属强势整理")
        elif rs >= RS_STRONG_PP:
            notes.append(f"近20日强于大盘{rs:.1f}pp")

    # ── 浮亏逼近深套（真深套另有两套剧本，不在这里重复）──
    if (floating_pct is not None and deep_threshold is not None
            and floating_pct > deep_threshold
            and floating_pct <= deep_threshold * NEAR_DEEP_RATIO):
        notes.append(f"浮亏{floating_pct:.0f}%逼近深套阈值{deep_threshold:.0f}%")

    # ── 波动率：决定止损宽度和单笔仓位 ──
    if atr_pct is not None:
        atr = _safe_float(atr_pct)
        if atr >= ATR_PCT_HIGH:
            notes.append(f"波动率{atr:.1f}%偏高，止损须放宽、单笔要小")
        elif atr <= ATR_PCT_LOW:
            notes.append(f"波动率{atr:.1f}%偏低，走势钝化")

    # ── 位置：低位是常态不写（12 只里 9 只都在低位，写了不区分）──
    if location and str(location).upper() == "HIGH":
        notes.append("处20日高位")

    return notes[:3]
