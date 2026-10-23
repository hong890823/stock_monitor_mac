#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Mapping, Optional, Sequence


EASTMONEY_LIMIT_POOL = "EASTMONEY_LIMIT_POOL"
MIN_SEAL_TO_FLOAT_PCT = 0.5
MIN_SEAL_TO_TURNOVER_PCT = 10.0
MAX_OPEN_COUNT = 1


@dataclass(frozen=True)
class SealQualityResult:
    score: Optional[int]
    coverage_pct: int
    components: Mapping[str, int]


def _finite(value: Any) -> bool:
    try:
        number = float(value)
        return math.isfinite(number)
    except (TypeError, ValueError):
        return False


def _number(row: Mapping[str, Any], key: str) -> Optional[float]:
    value = row.get(key)
    return float(value) if _finite(value) else None


class LimitCandidatePolicy:
    MIN_SEAL_TO_FLOAT_PCT = MIN_SEAL_TO_FLOAT_PCT
    MIN_SEAL_TO_TURNOVER_PCT = MIN_SEAL_TO_TURNOVER_PCT
    MAX_OPEN_COUNT = MAX_OPEN_COUNT

    @staticmethod
    def eligible(stock: Mapping[str, Any]) -> bool:
        if stock.get("source") != EASTMONEY_LIMIT_POOL:
            return False
        streak = _number(stock, "limit_streak")
        open_count = _number(stock, "open_count")
        if streak is None or streak < 1 or open_count is None:
            return False
        if open_count < 0 or open_count > MAX_OPEN_COUNT or open_count != int(open_count):
            return False
        seal_to_float = _number(stock, "seal_to_float_pct")
        seal_to_turnover = _number(stock, "seal_to_turnover_pct")
        return (
            seal_to_float is not None and seal_to_float >= MIN_SEAL_TO_FLOAT_PCT
        ) or (
            seal_to_turnover is not None and seal_to_turnover >= MIN_SEAL_TO_TURNOVER_PCT
        )


def seal_quality_score(stock: Mapping[str, Any]) -> SealQualityResult:
    components = {}
    maximums = {
        "seal_to_float": 40,
        "seal_to_turnover": 30,
        "open_count": 15,
        "limit_streak": 15,
    }

    seal_to_float = _number(stock, "seal_to_float_pct")
    if seal_to_float is not None and seal_to_float >= 0:
        components["seal_to_float"] = min(40, int(round(seal_to_float / 0.5 * 40)))

    seal_to_turnover = _number(stock, "seal_to_turnover_pct")
    if seal_to_turnover is not None and seal_to_turnover >= 0:
        components["seal_to_turnover"] = min(30, int(round(seal_to_turnover / 50.0 * 30)))

    open_count = _number(stock, "open_count")
    if open_count is not None and open_count in (0, 1):
        components["open_count"] = 15 if open_count == 0 else 7

    streak = _number(stock, "limit_streak")
    if streak is not None and streak >= 1:
        components["limit_streak"] = min(15, int(round(streak / 4.0 * 15)))

    available_max = sum(maximums[key] for key in components)
    if available_max == 0:
        return SealQualityResult(score=None, coverage_pct=0, components={})
    coverage_pct = int(round(available_max / sum(maximums.values()) * 100))
    return SealQualityResult(
        score=sum(components.values()),
        coverage_pct=coverage_pct,
        components=components,
    )


def rank_limit_up(stocks: Sequence[Mapping[str, Any]]) -> list[dict]:
    decorated = []
    for position, stock in enumerate(stocks):
        existing_score = _number(stock, "seal_quality_score")
        quality = seal_quality_score(stock)
        row = dict(stock)
        row["seal_quality_score"] = (
            int(existing_score) if existing_score is not None else quality.score
        )
        row["seal_quality_coverage_pct"] = (
            _number(stock, "seal_quality_coverage_pct")
            if _number(stock, "seal_quality_coverage_pct") is not None
            else quality.coverage_pct
        )
        row["_position"] = position
        decorated.append(row)
    return [
        {key: value for key, value in row.items() if key != "_position"}
        for row in sorted(
            decorated,
            key=lambda item: (
                -(item.get("seal_quality_score") if item.get("seal_quality_score") is not None else -1),
                item["_position"],
            ),
        )
    ]


def rank_limit_down(stocks: Sequence[Mapping[str, Any]]) -> list[dict]:
    decorated = []
    for position, stock in enumerate(stocks):
        row = dict(stock)
        row.pop("seal_quality_score", None)
        row.pop("seal_quality_coverage_pct", None)
        change = _number(stock, "change_pct")
        row["_position"] = position
        row["_change"] = change if change is not None else float("inf")
        decorated.append(row)
    return [
        {
            key: value
            for key, value in row.items()
            if key not in {"_position", "_change"}
        }
        for row in sorted(decorated, key=lambda item: (item["_change"], item["_position"]))
    ]
