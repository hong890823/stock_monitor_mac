#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Mapping, Optional


MIN_CONFIRMATION_GAP_MINUTES = 15
MAX_CONFIRMATION_WINDOW_MINUTES = 45
MAX_WEAK_DAYS = 3
MAX_OBSERVATION_DAYS = 10


class CandidateStrategy(str, Enum):
    PRE_BREAKOUT = "PRE_BREAKOUT"
    LEADER = "LEADER"
    LIMIT_UP_CONTINUATION = "LIMIT_UP_CONTINUATION"


class CandidateState(str, Enum):
    OBSERVING = "OBSERVING"
    NEAR_ENTRY = "NEAR_ENTRY"
    BUY_CANDIDATE = "BUY_CANDIDATE"
    ELIMINATED = "ELIMINATED"


@dataclass(frozen=True)
class CandidateReviewInput:
    at: datetime
    ordinary_gates_passed: bool
    trend_weak: bool
    price_trend_valid: bool
    fresh_sample: bool
    sealed: bool
    limit_streak: int
    environment_blocked: bool
    cumulative_volume_increase: bool = False


@dataclass(frozen=True)
class CandidateTransition:
    record: dict
    state: CandidateState
    blockers: tuple[str, ...]
    changed: bool


def required_confirmation_rounds(limit_streak: Optional[int]) -> int:
    try:
        streak = int(limit_streak or 0)
    except (TypeError, ValueError):
        streak = 0
    return 3 if streak >= 3 else 2


def route_source(
    source: str,
    *,
    strong_candidate: bool = False,
    limit_candidate: bool = False,
) -> CandidateStrategy:
    if limit_candidate:
        return CandidateStrategy.LIMIT_UP_CONTINUATION
    normalized = str(source or "").strip().lower()
    if strong_candidate or normalized in {"strong", "强势延续", "强势龙头"}:
        return CandidateStrategy.LEADER
    return CandidateStrategy.PRE_BREAKOUT


def _parse_timestamp(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _unique_dates(values: Any, new_date: Optional[str]) -> list[str]:
    result = []
    for value in values if isinstance(values, list) else []:
        text = str(value)[:10]
        if text and text not in result:
            result.append(text)
    if new_date and new_date not in result:
        result.append(new_date)
    return sorted(result)


def _reset_confirmation(record: dict) -> None:
    record["confirmation_rounds"] = 0
    record["confirmation_started_at"] = None
    record["last_confirmation_at"] = None


def advance_candidate(
    record: Mapping[str, Any],
    review: CandidateReviewInput,
) -> CandidateTransition:
    original = dict(record)
    updated = deepcopy(dict(record))
    if not isinstance(review.at, datetime) or not review.fresh_sample:
        return CandidateTransition(
            record=original,
            state=CandidateState(record.get("state", CandidateState.OBSERVING.value)),
            blockers=("样本缺失、失效或不是时间分离的新行情，不推进确认。",),
            changed=False,
        )

    current_state = CandidateState(record.get("state", CandidateState.OBSERVING.value))
    if current_state == CandidateState.ELIMINATED:
        return CandidateTransition(
            record=original,
            state=current_state,
            blockers=("候选已淘汰，重新入池前不推进状态。",),
            changed=False,
        )

    observation_date = review.at.date().isoformat()
    updated["observation_dates"] = _unique_dates(
        updated.get("observation_dates"), observation_date
    )

    if review.sealed:
        updated["state"] = CandidateState.OBSERVING.value
        updated["continuous_observation"] = True
        updated["limit_up_blocked"] = True
        updated["last_limit_seal_at"] = review.at.isoformat()
        _reset_confirmation(updated)
        return CandidateTransition(
            record=updated,
            state=CandidateState.OBSERVING,
            blockers=(),
            changed=updated != original,
        )

    updated["limit_up_blocked"] = False
    blockers = []
    if review.trend_weak:
        updated["weak_dates"] = _unique_dates(updated.get("weak_dates"), observation_date)

    if len(updated.get("weak_dates", [])) >= MAX_WEAK_DAYS:
        updated["state"] = CandidateState.ELIMINATED.value
        updated["eliminated_reason"] = (
            f"连续{MAX_WEAK_DAYS}个交易日走弱，淘汰观察"
        )
        _reset_confirmation(updated)
        return CandidateTransition(
            record=updated,
            state=CandidateState.ELIMINATED,
            blockers=(),
            changed=updated != original,
        )

    if (
        current_state != CandidateState.BUY_CANDIDATE
        and len(updated.get("observation_dates", [])) >= MAX_OBSERVATION_DAYS
    ):
        updated["state"] = CandidateState.ELIMINATED.value
        updated["eliminated_reason"] = (
            f"观察{MAX_OBSERVATION_DAYS}个交易日仍未确认，淘汰观察"
        )
        _reset_confirmation(updated)
        return CandidateTransition(
            record=updated,
            state=CandidateState.ELIMINATED,
            blockers=(),
            changed=updated != original,
        )

    price_trend_valid = review.price_trend_valid and not (
        review.cumulative_volume_increase and not review.price_trend_valid
    )
    if review.cumulative_volume_increase and not review.price_trend_valid:
        blockers.append("累计成交量增加不作为确认；价格/趋势条件尚未通过。")

    if not review.ordinary_gates_passed or not price_trend_valid:
        if not review.ordinary_gates_passed:
            blockers.append("普通买入门槛尚未全部通过。")
        if not review.price_trend_valid:
            blockers.append("价格趋势条件未确认。")
        updated["state"] = CandidateState.OBSERVING.value
        _reset_confirmation(updated)
        return CandidateTransition(
            record=updated,
            state=CandidateState.OBSERVING,
            blockers=tuple(blockers),
            changed=updated != original,
        )

    required_rounds = required_confirmation_rounds(review.limit_streak)
    started_at = _parse_timestamp(updated.get("confirmation_started_at"))
    last_confirmation_at = _parse_timestamp(updated.get("last_confirmation_at"))
    try:
        rounds = int(updated.get("confirmation_rounds") or 0)
    except (TypeError, ValueError):
        rounds = 0

    if rounds <= 0 or started_at is None or last_confirmation_at is None:
        updated["confirmation_rounds"] = 1
        updated["confirmation_started_at"] = review.at.isoformat()
        updated["last_confirmation_at"] = review.at.isoformat()
    else:
        elapsed_minutes = (review.at - started_at).total_seconds() / 60
        gap_minutes = (review.at - last_confirmation_at).total_seconds() / 60
        if elapsed_minutes > MAX_CONFIRMATION_WINDOW_MINUTES:
            updated["confirmation_rounds"] = 1
            updated["confirmation_started_at"] = review.at.isoformat()
            updated["last_confirmation_at"] = review.at.isoformat()
            blockers.append(
                f"确认窗口超过{MAX_CONFIRMATION_WINDOW_MINUTES}分钟，按新样本重新计时。"
            )
        elif gap_minutes < MIN_CONFIRMATION_GAP_MINUTES:
            blockers.append(
                f"新样本距上一轮不足{MIN_CONFIRMATION_GAP_MINUTES}分钟，不计确认轮次。"
            )
        else:
            updated["confirmation_rounds"] = rounds + 1
            updated["last_confirmation_at"] = review.at.isoformat()

    if review.environment_blocked:
        updated["state"] = CandidateState.NEAR_ENTRY.value
        blockers.append("市场/板块晋级环境明确转弱，暂缓晋级。")
    elif updated["confirmation_rounds"] >= required_rounds:
        updated["state"] = CandidateState.BUY_CANDIDATE.value
    else:
        updated["state"] = CandidateState.NEAR_ENTRY.value

    if updated["state"] != CandidateState.BUY_CANDIDATE.value:
        updated.pop("entry_confirmed_at", None)
    else:
        updated["entry_confirmed_at"] = review.at.isoformat()

    return CandidateTransition(
        record=updated,
        state=CandidateState(updated["state"]),
        blockers=tuple(blockers),
        changed=updated != original,
    )
