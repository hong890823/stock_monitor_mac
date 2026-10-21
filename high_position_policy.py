#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""高位横盘、派发风险和二波状态的纯计算。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
import math
from typing import Any, Optional


class HighPositionState(str, Enum):
    MISSING = "数据不足"
    NOT_HIGH = "非高位"
    HIGH_PENDING = "高位待定"
    DISTRIBUTION_RISK = "高位派发风险"
    HIGH_CONSOLIDATION = "高位整理蓄势"
    SECOND_WAVE_CANDIDATE = "二波候选"
    SECOND_WAVE_CONFIRMED = "二波确认"


@dataclass(frozen=True)
class HighPositionInput:
    price: float
    price_location: str
    range_width_pct: Optional[float] = None
    atr: Optional[float] = None
    volume_ratio: Optional[float] = None
    close: Optional[float] = None
    open: Optional[float] = None
    bar_position: Optional[float] = None
    upper_shadow_atr: Optional[float] = None
    lower_pivots_rising: Optional[bool] = None
    down_volume_ratio: Optional[float] = None
    support_holds: Optional[bool] = None
    support: Optional[float] = None
    resistance: Optional[float] = None
    short_trend: str = "数据不足"
    market_ok: Optional[bool] = None
    sector_ok: Optional[bool] = None
    timeframe: Any = None


@dataclass(frozen=True)
class HighPositionAssessment:
    state: HighPositionState
    reasons: tuple[str, ...]
    blockers: tuple[str, ...]
    missing_data: tuple[str, ...]
    confidence: str
    breakout_price: Optional[float]
    invalidation_price: Optional[float]


def _finite(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def assess_high_position(value: HighPositionInput) -> HighPositionAssessment:
    missing = []
    if _finite(value.price) is None or _finite(value.price) <= 0:
        missing.append("现价缺失或无效")
    if value.price_location not in {"HIGH", "MID", "LOW"}:
        missing.append("价格位置缺失")
    if value.timeframe is None and value.price_location == "HIGH":
        missing.append("15/30分钟证据缺失")
    if missing:
        return HighPositionAssessment(
            HighPositionState.MISSING, (), (), tuple(missing), "低", None, None,
        )
    if value.price_location != "HIGH":
        return HighPositionAssessment(
            HighPositionState.NOT_HIGH, ("价格不在近60日高位",), (), (), "高", None, None,
        )

    reasons = []
    blockers = []
    volume = _finite(value.volume_ratio)
    close = _finite(value.close)
    open_price = _finite(value.open)
    bar_position = _finite(value.bar_position)
    range_width = _finite(value.range_width_pct)
    atr = _finite(value.atr)
    upper_shadow = _finite(value.upper_shadow_atr)
    down_volume = _finite(value.down_volume_ratio)

    stalled = volume is not None and volume >= 1.3 and (
        (close is not None and open_price is not None and close <= open_price)
        or (bar_position is not None and bar_position <= 0.4)
    )
    lower_pivots = value.lower_pivots_rising is False
    pressure = (
        (upper_shadow is not None and upper_shadow >= 0.8)
        or lower_pivots
        or (down_volume is not None and down_volume >= 1.2)
    )
    support_holds = value.support_holds is True

    if stalled and pressure and not support_holds:
        return HighPositionAssessment(
            HighPositionState.DISTRIBUTION_RISK,
            ("高位放量滞涨", "高点/低点或量能转弱", "支撑未能守住"),
            ("不允许买入或接回",), (), "高", value.resistance, value.support,
        )
    if stalled and pressure and support_holds:
        return HighPositionAssessment(
            HighPositionState.HIGH_PENDING,
            ("存在放量滞涨和结构转弱证据", "但支撑仍在，证据冲突"),
            ("只观察，不提前判断下杀或二波",), (), "中", value.resistance, value.support,
        )

    narrow = range_width is not None and (range_width <= 8.0 or (atr and range_width <= 4 * atr * 100 / max(value.price, 1)))
    shrink_pullback = volume is not None and volume <= 0.9
    rising_lows = value.lower_pivots_rising is True
    no_distribution = not stalled and (down_volume is None or down_volume < 1.0)
    preparation = narrow and rising_lows and support_holds and no_distribution
    if not preparation:
        return HighPositionAssessment(
            HighPositionState.HIGH_PENDING,
            ("处于高位但整理证据不完整",), ("只观察",), (), "中",
            value.resistance, value.support,
        )

    reasons.extend(("高位区间较窄", "回踩缩量", "低点逐步抬高", "关键支撑守住"))
    healthy_environment = value.market_ok is not False and value.sector_ok is not False
    short_up = value.short_trend in {"近期向上", "连续拉升", "修复反弹"}
    if not healthy_environment:
        blockers.append("市场或板块环境不支持二波")
    if not short_up:
        blockers.append("短期趋势尚未重新向上")

    breakout_price = value.resistance
    invalidation_price = value.support
    confirmed = (
        breakout_price is not None
        and value.price >= breakout_price * 1.005
        and volume is not None and volume >= 1.3
        and getattr(value.timeframe, "complete", False)
        and getattr(value.timeframe, "m15_confirmations", 0) >= 2
        and getattr(value.timeframe, "m30_aligned", False)
        and support_holds
    )
    if confirmed and not blockers:
        return HighPositionAssessment(
            HighPositionState.SECOND_WAVE_CONFIRMED, tuple(reasons + [
                "收盘突破区间高点至少0.5%", "量比达到1.3", "15/30分钟条件通过",
            ]), (), (), "高", breakout_price, breakout_price,
        )
    if (
        breakout_price is not None
        and value.price >= breakout_price * 1.005
        and short_up and healthy_environment
    ):
        return HighPositionAssessment(
            HighPositionState.HIGH_PENDING,
            tuple(reasons + ["已接近/突破区间高点，但分钟确认不足"]),
            tuple(blockers + ["二波确认缺少完整15/30分钟证据"]),
            (), "中", breakout_price, invalidation_price,
        )
    if short_up and healthy_environment and breakout_price and value.price < breakout_price and shrink_pullback:
        return HighPositionAssessment(
            HighPositionState.SECOND_WAVE_CANDIDATE, tuple(reasons + [
                "短期趋势转强，等待突破区间高点",
            ]), tuple(blockers), (), "中", breakout_price, invalidation_price,
        )
    return HighPositionAssessment(
        HighPositionState.HIGH_CONSOLIDATION, tuple(reasons), tuple(blockers),
        (), "中", breakout_price, invalidation_price,
    )


def high_position_to_dict(value: HighPositionAssessment) -> dict:
    result = asdict(value)
    result["state"] = value.state.value
    return result
