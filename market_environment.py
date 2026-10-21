#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""市场环境质量判断。

成交额只代表交易活跃度，必须和指数价格行为、板块宽度及近期量能持续性一起看。
该模块保持纯函数，盘中提醒和持仓报告共用同一套口径。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from math import isfinite
from typing import Iterable, Optional

from market_turnover_tiers import NEUTRAL_TURNOVER_YI, turnover_activity_label


class MarketEnvironmentStatus(Enum):
    STRONG_SUSTAINED = "强势延续"
    STRONG_SINGLE_DAY = "高成交额待确认"
    DIVERGENCE = "放量分歧"
    NORMAL = "普通环境"
    UNKNOWN = "数据不足"


STRONG_TURNOVER_YI = NEUTRAL_TURNOVER_YI
SUSTAINED_TURNOVER_YI = 17_000.0
MIN_SUSTAINED_AVERAGE_YI = 18_000.0
MIN_SUSTAINED_DAYS = 3
MAX_PERSISTENCE_LOOKBACK = 5
MIN_BREADTH = 0.60
NEAR_INDEX_HIGH_PCT = 1.00


@dataclass(frozen=True)
class MarketEnvironmentQuality:
    status: MarketEnvironmentStatus
    predicted_turnover_yi: Optional[float]
    persistence_days: Optional[int]
    persistence_average_yi: Optional[float]
    industry_breadth: Optional[float]
    concept_breadth: Optional[float]
    market_change_pct: Optional[float]
    index_near_high: Optional[bool]
    reasons: tuple[str, ...]

    @property
    def strong(self) -> bool:
        return self.status == MarketEnvironmentStatus.STRONG_SUSTAINED

    @property
    def cautious(self) -> bool:
        return self.status == MarketEnvironmentStatus.STRONG_SINGLE_DAY

    @property
    def label(self) -> str:
        return self.status.value

    @property
    def activity_label(self) -> str:
        return turnover_activity_label(self.predicted_turnover_yi)

    def summary(self) -> str:
        parts = list(self.reasons) if self.reasons else [self.label]
        parts.append(f"量能活跃度={self.activity_label}")
        return "；".join(parts)


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


def evaluate_market_environment(
    *,
    predicted_turnover_yi: object,
    market_change_pct: object,
    market_price: object = None,
    market_high_price: object = None,
    industry_breadth: object = None,
    concept_breadth: object = None,
    recent_full_day_turnovers_yi: Iterable[object] = (),
) -> MarketEnvironmentQuality:
    """根据可观测证据给出市场环境等级，不把缺失数据猜成利好。"""
    turnover = _number(predicted_turnover_yi)
    if turnover is not None and turnover <= 0:
        turnover = None
    market_change = _number(market_change_pct)
    industry = _breadth(industry_breadth)
    concept = _breadth(concept_breadth)

    index_near_high: Optional[bool] = None
    market_price_value = _number(market_price)
    market_high_value = _number(market_high_price)
    if market_price_value is not None and market_high_value is not None and 0 < market_price_value <= market_high_value:
        index_near_high = (market_high_value - market_price_value) / market_high_value < NEAR_INDEX_HIGH_PCT / 100.0

    history = []
    for value in recent_full_day_turnovers_yi:
        number = _number(value)
        history.append(number if number is not None and number > 0 else None)
    history = history[-MAX_PERSISTENCE_LOOKBACK:]
    persistence_days = None
    persistence_average_yi = None
    if turnover is not None:
        persistence_days = 0
        for value in reversed(history):
            if value is None or value < SUSTAINED_TURNOVER_YI:
                break
            persistence_days += 1
        recent_three = history[-MIN_SUSTAINED_DAYS:]
        if len(recent_three) == MIN_SUSTAINED_DAYS and all(
            value is not None and value >= SUSTAINED_TURNOVER_YI for value in recent_three
        ):
            persistence_average_yi = sum(recent_three) / MIN_SUSTAINED_DAYS

    reasons: list[str] = []
    if turnover is None:
        reasons.append("全天预估成交额暂缺")
    else:
        reasons.append(f"全天预估成交额{turnover:,.0f}亿")
    if persistence_days is not None:
        average_text = (
            f"最近3日均值{persistence_average_yi:,.0f}亿"
            if persistence_average_yi is not None else "最近3日均值待确认"
        )
        reasons.append(
            f"此前完整收盘日连续{persistence_days}日达到1.7万亿参考线；{average_text}；今日预估单独观察"
        )
    if market_change is None:
        reasons.append("沪指涨跌暂缺")
    else:
        reasons.append(f"沪指{market_change:+.2f}%")
    if index_near_high is True:
        reasons.append("指数接近日内高位")
    elif index_near_high is False:
        reasons.append("指数未能接近日内高位，存在滞涨或分歧")
    else:
        reasons.append("指数日内位置数据暂缺或异常")
    if industry is not None:
        reasons.append(f"行业板块上涨占比{industry:.0%}")
    else:
        reasons.append("行业板块宽度暂缺")
    if concept is not None:
        reasons.append(f"概念板块上涨占比{concept:.0%}")
    else:
        reasons.append("概念板块宽度暂缺")

    if turnover is None or market_change is None or industry is None or concept is None or index_near_high is None:
        return MarketEnvironmentQuality(
            MarketEnvironmentStatus.UNKNOWN, turnover, persistence_days, persistence_average_yi,
            industry, concept, market_change, index_near_high, tuple(reasons),
        )

    breadth_strong = min(industry, concept) >= MIN_BREADTH
    index_strong = market_change >= 0.0 and index_near_high is True
    high_turnover = turnover >= STRONG_TURNOVER_YI
    sustained = (
        persistence_days is not None
        and persistence_days >= MIN_SUSTAINED_DAYS
        and persistence_average_yi is not None
        and persistence_average_yi >= MIN_SUSTAINED_AVERAGE_YI
    )

    if high_turnover and breadth_strong and index_strong:
        if sustained:
            status = MarketEnvironmentStatus.STRONG_SUSTAINED
            reasons.append("量能、指数和板块扩散暂时同向")
        else:
            status = MarketEnvironmentStatus.STRONG_SINGLE_DAY
            reasons.append("当前量能很高，但连续性尚未确认")
    elif high_turnover and (not breadth_strong or not index_strong):
        status = MarketEnvironmentStatus.DIVERGENCE
        if not breadth_strong:
            reasons.append("板块扩散不足，不能把高成交额等同于普涨")
        if not index_strong:
            reasons.append("指数涨幅或日内位置不支持强势延续")
    else:
        status = MarketEnvironmentStatus.NORMAL
        if turnover < STRONG_TURNOVER_YI:
            reasons.append("成交额尚未达到高量级判断线")

    return MarketEnvironmentQuality(
        status, turnover, persistence_days, persistence_average_yi, industry, concept,
        market_change, index_near_high, tuple(reasons),
    )
