#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Optional

from capital_flow import CapitalFlowSnapshot


MIN_RELIABLE_TURNOVER_YI = 0.05
SINGLE_DAY_NET_PCT = 3.0
WINDOW_NET_PCT = 3.0
STRONG_NET_PCT = 10.0


class FundFlowTone(str, Enum):
    SUPPORTIVE = "SUPPORTIVE"
    DIVERGENT = "DIVERGENT"
    RISK = "RISK"
    ABSORPTION_WATCH = "ABSORPTION_WATCH"
    NEUTRAL = "NEUTRAL"
    UNAVAILABLE = "UNAVAILABLE"
    ACCUMULATING = "ACCUMULATING"
    SUSTAINED_INFLOW = "SUSTAINED_INFLOW"
    SUSTAINED_OUTFLOW = "SUSTAINED_OUTFLOW"


@dataclass(frozen=True)
class FundFlowAssessment:
    tone: FundFlowTone
    strong: bool
    low_confidence: bool
    reason: str
    score_delta: int = 0
    blocks_promotion: bool = False


@dataclass(frozen=True)
class FundFlowWindowAssessment:
    tone: FundFlowTone
    low_confidence: bool
    score_delta: int
    blocks_promotion: bool
    reason: str
    sample_dates: tuple[str, ...]


def _finite(value: Optional[float]) -> bool:
    try:
        return value is not None and float(value) == float(value) and abs(float(value)) != float("inf")
    except (TypeError, ValueError):
        return False


def _reliable(snapshot: CapitalFlowSnapshot) -> bool:
    return _finite(snapshot.turnover_yi) and float(snapshot.turnover_yi) >= MIN_RELIABLE_TURNOVER_YI


def assess_single_day(
    change_pct: Optional[float],
    snapshot: Optional[CapitalFlowSnapshot],
) -> FundFlowAssessment:
    if snapshot is None or not _finite(change_pct) or not _finite(snapshot.net_ratio_pct):
        return FundFlowAssessment(
            tone=FundFlowTone.UNAVAILABLE,
            strong=False,
            low_confidence=False,
            reason="资金流或价格变化缺失，不参与资金流加减分；资金流按订单规模归类，不代表机构身份。",
        )

    change_pct = float(change_pct)
    net_ratio_pct = float(snapshot.net_ratio_pct)
    low_confidence = not _reliable(snapshot)
    strong = abs(net_ratio_pct) >= STRONG_NET_PCT

    if change_pct > 0 and net_ratio_pct >= SINGLE_DAY_NET_PCT:
        tone = FundFlowTone.SUPPORTIVE
        reason = "价格上涨且主动买卖净流向为正，仅作辅助确认。"
    elif change_pct > 0 and net_ratio_pct <= -SINGLE_DAY_NET_PCT:
        tone = FundFlowTone.DIVERGENT
        reason = "价格上涨但主动买卖净流向为负，存在量价资金背离，暂缓普通买入确认。"
    elif change_pct < 0 and net_ratio_pct <= -SINGLE_DAY_NET_PCT:
        tone = FundFlowTone.RISK
        reason = "价格下跌且主动买卖净流向为负，增强风险提示。"
    elif change_pct < 0 and net_ratio_pct >= SINGLE_DAY_NET_PCT:
        tone = FundFlowTone.ABSORPTION_WATCH
        reason = "价格下跌但主动买卖净流向为正，仅作潜在承接观察。"
    else:
        tone = FundFlowTone.NEUTRAL
        reason = "价格与主动买卖净流向未形成明显方向组合。"

    if low_confidence:
        delta = 0
        reason += " 成交额缺失或不足500万元，窗口按低置信度处理。"
    elif tone == FundFlowTone.SUPPORTIVE:
        delta = 1
    elif tone in {FundFlowTone.DIVERGENT, FundFlowTone.RISK}:
        delta = -1
    else:
        delta = 0

    return FundFlowAssessment(
        tone=tone,
        strong=strong,
        low_confidence=low_confidence,
        reason=reason + " 资金流按订单规模归类，不代表机构身份。",
        score_delta=delta,
    )


def assess_window(
    snapshots: Iterable[CapitalFlowSnapshot],
    change_pct: Optional[float],
) -> FundFlowWindowAssessment:
    by_date = {}
    for snapshot in snapshots:
        if snapshot is not None:
            by_date.setdefault(snapshot.quote_date, snapshot)
    ordered = [by_date[key] for key in sorted(by_date)]
    latest_three = ordered[-3:]
    sample_dates = tuple(snapshot.quote_date for snapshot in latest_three)

    if len(latest_three) != 3:
        return FundFlowWindowAssessment(
            tone=FundFlowTone.ACCUMULATING,
            low_confidence=False,
            score_delta=0,
            blocks_promotion=False,
            reason="最近5日快照中有效交易日不足3日，资金流窗口仍在积累。",
            sample_dates=sample_dates,
        )

    valid = all(_finite(snapshot.net_ratio_pct) for snapshot in latest_three)
    reliable = valid and all(_reliable(snapshot) for snapshot in latest_three)
    if not valid:
        tone = FundFlowTone.ACCUMULATING
    else:
        values = [float(snapshot.net_ratio_pct) for snapshot in latest_three]
        average = sum(values) / len(values)
        positive_days = sum(1 for value in values if value > 0)
        if average >= WINDOW_NET_PCT and positive_days >= 2:
            tone = FundFlowTone.SUSTAINED_INFLOW
        elif average <= -WINDOW_NET_PCT and positive_days <= 1:
            tone = FundFlowTone.SUSTAINED_OUTFLOW
        else:
            tone = FundFlowTone.NEUTRAL

    if not reliable:
        return FundFlowWindowAssessment(
            tone=FundFlowTone.ACCUMULATING,
            low_confidence=True,
            score_delta=0,
            blocks_promotion=False,
            reason="近3日资金流窗口含缺失字段或低成交额样本，不参与晋级评分。",
            sample_dates=sample_dates,
        )

    if tone == FundFlowTone.SUSTAINED_INFLOW:
        score_delta = 1
        blocks_promotion = False
        reason = "近3日平均净流向持续为正，资金流仅作辅助加分。"
    elif tone == FundFlowTone.SUSTAINED_OUTFLOW:
        score_delta = 0
        blocks_promotion = True
        reason = "近3日平均净流向持续为负，暂缓候选晋级。"
    else:
        score_delta = 0
        blocks_promotion = False
        reason = "近3日资金流未形成持续方向，不参与晋级评分。"

    return FundFlowWindowAssessment(
        tone=tone,
        low_confidence=False,
        score_delta=score_delta,
        blocks_promotion=blocks_promotion,
        reason=reason + f" 价格变化{change_pct:+.2f}%。" if _finite(change_pct) else reason,
        sample_dates=sample_dates,
    )


def score_delta(assessment: FundFlowAssessment) -> int:
    return 0 if assessment.low_confidence else assessment.score_delta
