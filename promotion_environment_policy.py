#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Optional, Sequence


INDEX_RISK_PCT = -0.8
BOARD_WEAK_PCT = -1.0
LIMIT_DOWN_BREADTH_COUNT = 20


@dataclass(frozen=True)
class PromotionEnvironmentInput:
    shanghai_change_pct: Optional[float]
    shenzhen_change_pct: Optional[float]
    limit_up_count: Optional[int]
    limit_down_count: Optional[int]
    board_changes: Sequence[Optional[float]] = ()


@dataclass(frozen=True)
class PromotionEnvironmentDecision:
    blocked: bool
    reason: Optional[str]
    evidence: tuple[str, ...]


def _finite(value: Optional[float]) -> bool:
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _count(value: Optional[int]) -> Optional[int]:
    try:
        if value is None:
            return None
        number = int(value)
        return number if number >= 0 and number == value else None
    except (TypeError, ValueError):
        return None


def evaluate_environment(
    environment: PromotionEnvironmentInput,
    *,
    enabled: bool = False,
) -> PromotionEnvironmentDecision:
    evidence = []
    index_changes = [
        float(value)
        for value in (
            environment.shanghai_change_pct,
            environment.shenzhen_change_pct,
        )
        if _finite(value)
    ]
    index_risk = any(value <= INDEX_RISK_PCT for value in index_changes)
    if index_changes:
        evidence.append(
            "主要指数变化：" + "、".join(f"{value:+.2f}%" for value in index_changes)
        )

    limit_up_count = _count(environment.limit_up_count)
    limit_down_count = _count(environment.limit_down_count)
    breadth_risk = (
        limit_up_count is not None
        and limit_down_count is not None
        and limit_down_count >= LIMIT_DOWN_BREADTH_COUNT
        and limit_down_count > limit_up_count
    )
    if limit_up_count is not None and limit_down_count is not None:
        evidence.append(f"涨跌停广度：涨停{limit_up_count}只、跌停{limit_down_count}只")

    board_changes = [
        float(value) for value in environment.board_changes if _finite(value)
    ]
    board_risk = bool(board_changes) and all(
        value <= BOARD_WEAK_PCT for value in board_changes
    )
    if board_changes:
        evidence.append(
            "已知板块变化：" + "、".join(f"{value:+.2f}%" for value in board_changes)
        )

    if not enabled:
        return PromotionEnvironmentDecision(
            blocked=False,
            reason=None,
            evidence=tuple(evidence),
        )

    if index_risk:
        reason = "主要指数进入风险区，暂缓候选晋级"
    elif breadth_risk:
        reason = "跌停数量明显偏多，市场承接转弱，暂缓候选晋级"
    elif board_risk:
        reason = "所属板块同步转弱，暂缓候选晋级"
    else:
        reason = None
    return PromotionEnvironmentDecision(
        blocked=reason is not None,
        reason=reason,
        evidence=tuple(evidence),
    )
