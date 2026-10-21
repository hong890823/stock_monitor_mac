#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""市场成交额五档口径。"""

from __future__ import annotations

from math import isfinite


VERY_WEAK_TURNOVER_YI = 15_000.0
WEAK_TURNOVER_YI = 18_000.0
NEUTRAL_TURNOVER_YI = 20_000.0
STRONG_TURNOVER_YI = 25_000.0


def turnover_activity_label(value: object) -> str:
    try:
        turnover = float(value)
    except (TypeError, ValueError):
        return "活跃度未知"
    if not isfinite(turnover) or turnover <= 0:
        return "活跃度未知"
    if turnover < VERY_WEAK_TURNOVER_YI:
        return "很弱"
    if turnover < WEAK_TURNOVER_YI:
        return "一般弱"
    if turnover < NEUTRAL_TURNOVER_YI:
        return "中性偏弱"
    if turnover < STRONG_TURNOVER_YI:
        return "一般强"
    return "比较强"
