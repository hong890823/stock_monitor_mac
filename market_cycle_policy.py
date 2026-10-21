#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""行情节奏先验。

这里输出的是不同行情环境下的经验参考窗口，不把天数当成确定性预测。
结构证据优先于天数：破位、放量滞涨和板块扩散恶化可以提前触发风险提醒。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum
from math import isfinite
from statistics import median
from typing import Iterable, Optional

from market_turnover_tiers import (
    NEUTRAL_TURNOVER_YI,
    STRONG_TURNOVER_YI,
    VERY_WEAK_TURNOVER_YI,
    WEAK_TURNOVER_YI,
    turnover_activity_label,
)


class MarketCyclePhase(Enum):
    UP = "上涨"
    DIVERGENCE = "分歧"
    DOWN = "下跌"
    RANGE = "箱体震荡"
    UNKNOWN = "阶段未知"


class MarketCycleRegime(Enum):
    VERY_WEAK = "很弱"
    WEAK = "一般弱"
    NEUTRAL = "中性偏弱"
    STRONG = "一般强"
    VERY_STRONG = "比较强"
    UNKNOWN = "活跃度未知"


@dataclass(frozen=True)
class MarketCycleInput:
    predicted_turnover_yi: object = None
    turnover_history_yi: Iterable[object] = ()
    turnover_history_dates: Iterable[object] = ()
    turnover_history_as_of_date: object = None
    index_today_pct: object = None
    index_recent_pct: object = None
    industry_breadth: object = None
    concept_breadth: object = None
    phase_hint: object = None
    phase_age_days: object = None
    divergence_age_days: object = None
    down_age_days: object = None


@dataclass(frozen=True)
class MarketCycleAssessment:
    phase: MarketCyclePhase
    regime: MarketCycleRegime
    confidence: str
    up_days_range: Optional[tuple[int, int]]
    divergence_days_range: Optional[tuple[int, int]]
    down_days_range: Optional[tuple[int, int]]
    phase_age_days: Optional[int]
    divergence_age_days: Optional[int]
    down_age_days: Optional[int]
    turnover_ratio: Optional[float]
    turnover_baseline_yi: Optional[float]
    turnover_baseline_sample_count: int
    turnover_baseline_latest_date: Optional[str]
    turnover_baseline_gap_count: int
    turnover_baseline_stale: bool
    turnover_baseline_ready: bool
    reasons: tuple[str, ...]

    @property
    def is_prior(self) -> bool:
        return self.phase is not MarketCyclePhase.UNKNOWN


def _number(value: object) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def _breadth(value: object) -> Optional[float]:
    number = _number(value)
    if number is None or not 0.0 <= number <= 1.0:
        return None
    return number


def _age(value: object) -> Optional[int]:
    number = _number(value)
    if number is None or number < 0:
        return None
    return int(number)


def _date_text(value: object) -> Optional[str]:
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except (TypeError, ValueError):
        return None


def _phase_from_hint(value: object) -> Optional[MarketCyclePhase]:
    text = str(value or "").strip().lower()
    aliases = {
        "up": MarketCyclePhase.UP,
        "上涨": MarketCyclePhase.UP,
        "拉升": MarketCyclePhase.UP,
        "divergence": MarketCyclePhase.DIVERGENCE,
        "分歧": MarketCyclePhase.DIVERGENCE,
        "放量分歧": MarketCyclePhase.DIVERGENCE,
        "down": MarketCyclePhase.DOWN,
        "下跌": MarketCyclePhase.DOWN,
        "调整": MarketCyclePhase.DOWN,
        "range": MarketCyclePhase.RANGE,
        "箱体": MarketCyclePhase.RANGE,
        "震荡": MarketCyclePhase.RANGE,
    }
    return aliases.get(text)


def _turnover_context(
    turnover: Optional[float],
    history: Iterable[object],
    history_dates: Iterable[object],
    as_of_date: object,
) -> tuple[
    Optional[float],
    Optional[float],
    int,
    Optional[str],
    int,
    bool,
    bool,
]:
    raw_values = list(history or ())
    raw_dates = list(history_dates or ())
    records: list[tuple[Optional[str], float]] = []
    for index, raw_value in enumerate(raw_values):
        number = _number(raw_value)
        if number is None or number <= 0:
            continue
        raw_date = raw_dates[index] if index < len(raw_dates) else None
        records.append((_date_text(raw_date), number))

    records = records[-20:]
    valid_values = [value for _, value in records]
    sample_count = len(valid_values)
    baseline = median(valid_values) if valid_values else None
    dated_records = [(day, value) for day, value in records if day]
    latest_date = dated_records[-1][0] if dated_records else None
    gap_count = 0
    for previous, current in zip(dated_records, dated_records[1:]):
        previous_day = date.fromisoformat(previous[0])
        current_day = date.fromisoformat(current[0])
        if (current_day - previous_day).days > 4:
            gap_count += 1

    as_of = _date_text(as_of_date)
    stale = bool(
        latest_date
        and as_of
        and (date.fromisoformat(as_of) - date.fromisoformat(latest_date)).days > 7
    )
    ready = sample_count >= 5
    dates_complete = len(dated_records) == sample_count
    comparable = ready and dates_complete and not stale and gap_count == 0
    ratio = (
        turnover / baseline
        if (
            turnover is not None
            and turnover > 0
            and baseline is not None
            and baseline > 0
            and comparable
        )
        else None
    )
    return ratio, baseline, sample_count, latest_date, gap_count, stale, ready


def _infer_phase(
    index_today: Optional[float],
    index_recent: Optional[float],
    breadth: Optional[float],
    turnover: Optional[float],
    turnover_ratio: Optional[float],
) -> MarketCyclePhase:
    if index_today is None:
        return MarketCyclePhase.UNKNOWN
    high_activity = (
        turnover is not None and turnover >= NEUTRAL_TURNOVER_YI
    ) or (turnover_ratio is not None and turnover_ratio >= 1.25)
    if index_today > 0 and (breadth is None or breadth >= 0.55):
        return MarketCyclePhase.UP
    if index_today <= 0 and high_activity and breadth is not None and breadth < 0.55:
        return MarketCyclePhase.DIVERGENCE
    if index_today < 0 or (index_recent is not None and index_recent < 0):
        return MarketCyclePhase.DOWN
    return MarketCyclePhase.RANGE


def assess_market_cycle(value: MarketCycleInput) -> MarketCycleAssessment:
    """计算动态参考窗口；缺失数据时只降低置信度，不猜成强行情。"""
    turnover = _number(value.predicted_turnover_yi)
    if turnover is not None and turnover <= 0:
        turnover = None
    history = list(value.turnover_history_yi or ())
    (
        turnover_ratio,
        turnover_baseline,
        baseline_count,
        baseline_latest_date,
        baseline_gap_count,
        baseline_stale,
        baseline_ready,
    ) = _turnover_context(
        turnover,
        history,
        value.turnover_history_dates,
        value.turnover_history_as_of_date,
    )
    index_today = _number(value.index_today_pct)
    index_recent = _number(value.index_recent_pct)
    industry = _breadth(value.industry_breadth)
    concept = _breadth(value.concept_breadth)
    breadth_values = [item for item in (industry, concept) if item is not None]
    breadth = sum(breadth_values) / len(breadth_values) if breadth_values else None
    hinted_phase = _phase_from_hint(value.phase_hint)
    phase = hinted_phase or _infer_phase(
        index_today, index_recent, breadth, turnover, turnover_ratio
    )
    phase_age = _age(value.phase_age_days)
    divergence_age = _age(value.divergence_age_days)
    down_age = _age(value.down_age_days)

    regime_label = turnover_activity_label(turnover)
    regime = next(
        (item for item in MarketCycleRegime if item.value == regime_label),
        MarketCycleRegime.UNKNOWN,
    )

    reasons: list[str] = []
    if turnover is not None:
        reasons.append(f"全天成交额量级{turnover:,.0f}亿，量能活跃度={regime.value}")
    else:
        reasons.append("全天成交额未取到，量能活跃度未知")
    if baseline_ready and turnover_baseline is not None:
        ratio_text = f"{turnover_ratio:.2f}倍" if turnover_ratio is not None else "暂缺"
        reasons.append(
            f"历史基准使用{baseline_count}个完整收盘日，中位数{turnover_baseline:,.0f}亿，相对量能{ratio_text}"
        )
    elif history:
        reasons.append(
            f"历史成交额基准完整收盘日不足5个，当前仅{baseline_count}个，不据此推导强弱"
        )
    if baseline_stale:
        reasons.append(f"历史基准过旧，最新完整收盘日为{baseline_latest_date}")
    if baseline_gap_count:
        reasons.append(f"历史成交额基准存在{baseline_gap_count}处日期缺口，样本连续性不足")

    up_range: Optional[tuple[int, int]] = None
    divergence_range: Optional[tuple[int, int]] = None
    down_range: Optional[tuple[int, int]] = None
    if phase is MarketCyclePhase.UNKNOWN:
        confidence = "低"
        reasons.append("缺少指数方向或明确阶段，暂不输出天数窗口")
    else:
        if regime is MarketCycleRegime.VERY_WEAK:
            up_range = (1, 2)
            divergence_range = (1, 2)
        elif regime is MarketCycleRegime.WEAK:
            up_range = (2, 3)
            divergence_range = (2, 3)
        elif regime is MarketCycleRegime.NEUTRAL:
            up_range = (2, 4)
            divergence_range = (2, 3)
        else:
            up_range = (4, 5)
            divergence_range = (3, 4)
        down_range = (5, 8)

        risk_signal = (
            index_today is not None and index_today < 0
        ) or (breadth is not None and breadth < 0.50)
        activity_score = 0
        if turnover is not None and turnover >= STRONG_TURNOVER_YI:
            activity_score += 35
        elif turnover is not None and turnover >= NEUTRAL_TURNOVER_YI:
            activity_score += 25
        elif turnover is not None and turnover >= WEAK_TURNOVER_YI:
            activity_score += 15
        elif turnover is not None and turnover >= VERY_WEAK_TURNOVER_YI:
            activity_score += 5
        if turnover_ratio is not None and turnover_ratio >= 1.25:
            activity_score += 25
        elif turnover_ratio is not None and turnover_ratio >= 1.10:
            activity_score += 15
        if breadth is not None:
            activity_score += 20 if breadth >= 0.60 else 10 if breadth >= 0.50 else 0
        if index_today is not None and index_today >= 0:
            activity_score += 20

        if phase is MarketCyclePhase.UP and activity_score >= 75:
            up_range = (up_range[0], min(7, up_range[1] + 2))
            divergence_range = (divergence_range[0], min(5, divergence_range[1] + 1))
            reasons.append("指数、扩散和活跃度同向，上涨窗口可适度延长")
        elif phase is MarketCyclePhase.UP and activity_score <= 40:
            up_range = (max(1, up_range[0] - 1), max(1, up_range[1] - 1))
            divergence_range = (max(1, divergence_range[0] - 1), max(1, divergence_range[1] - 1))
            reasons.append("活跃度或扩散偏弱，上涨窗口提前收窄")
        if phase is MarketCyclePhase.DIVERGENCE:
            if risk_signal:
                divergence_range = (1, 2)
                reasons.append("指数或板块扩散转弱，分歧窗口提前收窄")
            elif activity_score >= 70:
                divergence_range = (divergence_range[0], min(5, divergence_range[1] + 2))
                reasons.append("高活跃度下仍有承接，分歧窗口可适度延长")

        current_age = phase_age
        if phase is MarketCyclePhase.DIVERGENCE:
            current_age = divergence_age if divergence_age is not None else phase_age
        elif phase is MarketCyclePhase.DOWN:
            current_age = down_age if down_age is not None else phase_age

        active_range = {
            MarketCyclePhase.UP: up_range,
            MarketCyclePhase.DIVERGENCE: divergence_range,
            MarketCyclePhase.DOWN: down_range,
        }.get(phase)
        if current_age is not None:
            reasons.append(f"当前{phase.value}第{current_age}天")
            if active_range and current_age > active_range[1]:
                reasons.append(
                    f"{phase.value}已持续{current_age}天，超过{active_range[0]}–{active_range[1]}天参考区间"
                )
                reasons.append("已超过参考窗口，不能只按天数判断")

        confidence = "高" if turnover is not None and breadth is not None and index_today is not None else "中"
        if activity_score < 35 or phase is MarketCyclePhase.RANGE:
            confidence = "低" if activity_score < 20 else "中"
        if not baseline_ready or baseline_stale or baseline_gap_count:
            confidence = "低"

    assessment_phase_age = phase_age
    if phase is MarketCyclePhase.DIVERGENCE:
        assessment_phase_age = divergence_age if divergence_age is not None else phase_age
    elif phase is MarketCyclePhase.DOWN:
        assessment_phase_age = down_age if down_age is not None else phase_age

    return MarketCycleAssessment(
        phase=phase,
        regime=regime,
        confidence=confidence,
        up_days_range=up_range,
        divergence_days_range=divergence_range,
        down_days_range=down_range,
        phase_age_days=assessment_phase_age,
        divergence_age_days=divergence_age if phase is MarketCyclePhase.DIVERGENCE else None,
        down_age_days=down_age if phase is MarketCyclePhase.DOWN else None,
        turnover_ratio=turnover_ratio,
        turnover_baseline_yi=turnover_baseline,
        turnover_baseline_sample_count=baseline_count,
        turnover_baseline_latest_date=baseline_latest_date,
        turnover_baseline_gap_count=baseline_gap_count,
        turnover_baseline_stale=baseline_stale,
        turnover_baseline_ready=baseline_ready,
        reasons=tuple(reasons),
    )


def _range_text(value: Optional[tuple[int, int]]) -> str:
    return f"{value[0]}–{value[1]}天" if value else "暂不估计"


def format_market_cycle_assessment(value: MarketCycleAssessment) -> str:
    parts = [
        f"阶段={value.phase.value}",
        f"活跃度={value.regime.value}",
        f"上涨参考{_range_text(value.up_days_range)}",
        f"分歧参考{_range_text(value.divergence_days_range)}",
        f"下跌参考{_range_text(value.down_days_range)}",
        f"置信度{value.confidence}",
    ]
    if value.phase_age_days is not None and value.phase is not MarketCyclePhase.UNKNOWN:
        parts.append(f"当前{value.phase.value}第{value.phase_age_days}天")
    if value.turnover_baseline_sample_count and not value.turnover_baseline_ready:
        parts.append(
            f"历史成交额基准完整收盘日不足5个（{value.turnover_baseline_sample_count}个）"
        )
    if value.turnover_baseline_stale:
        parts.append("历史基准过旧")
    if value.turnover_baseline_gap_count:
        parts.append(f"历史基准有{value.turnover_baseline_gap_count}处日期缺口")
    active_age = {
        MarketCyclePhase.UP: value.phase_age_days,
        MarketCyclePhase.DIVERGENCE: value.divergence_age_days,
        MarketCyclePhase.DOWN: value.down_age_days,
    }.get(value.phase)
    active_range = {
        MarketCyclePhase.UP: value.up_days_range,
        MarketCyclePhase.DIVERGENCE: value.divergence_days_range,
        MarketCyclePhase.DOWN: value.down_days_range,
    }.get(value.phase)
    if active_age is not None and active_range and active_age > active_range[1]:
        parts.append("已超过参考窗口")
    return "；".join(parts) + "。天数仅作风险窗口参考，结构破位优先，不构成交易信号。"
