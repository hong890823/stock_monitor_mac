#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""轻量证据卡、判断复核和暂缓做 T 结果追踪。

这里只处理已有数据的整理，不抓取行情，也不替盘中规则生成买卖信号。
"""

from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Any, Iterable, Optional


@dataclass(frozen=True)
class EvidenceCard:
    label: str
    value: Any
    source: Optional[str] = None
    quote_date: Optional[str] = None
    as_of: Optional[str] = None
    scope: Optional[str] = None
    fresh: bool = True
    available: bool = True


def format_evidence_cards(cards: Iterable[EvidenceCard]) -> tuple[str, ...]:
    lines = []
    for card in cards or ():
        if not card.available or card.value is None:
            text = f"{card.label}：暂缺"
        else:
            text = f"{card.label}：{card.value}"
            if not card.fresh:
                text += "（已过期）"
        metadata = []
        if card.source:
            metadata.append(f"来源 {card.source}")
        if card.quote_date:
            metadata.append(f"数据日 {str(card.quote_date)[:10]}")
        if card.as_of:
            metadata.append(f"时间 {card.as_of}")
        if card.scope:
            metadata.append(f"范围 {card.scope}")
        if metadata:
            text += "｜" + "、".join(metadata)
        lines.append(text)
    return tuple(lines)


def _number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def _quality_value(market: dict, quality: Any, name: str, default=None):
    if name in market:
        return market.get(name)
    if isinstance(quality, dict) and name in quality:
        return quality.get(name)
    return getattr(quality, name, default) if quality is not None else default


def build_evidence_cards(market: Optional[dict]) -> tuple[EvidenceCard, ...]:
    market = market or {}
    quality = market.get("market_environment") or market.get("quality")
    predicted = _quality_value(market, quality, "predicted_turnover_yi")
    change = _quality_value(market, quality, "market_change_pct")
    industry = _quality_value(market, quality, "industry_breadth")
    concept = _quality_value(market, quality, "concept_breadth")
    return (
        EvidenceCard(
            "全天预估成交额",
            f"{_number(predicted):,.0f}亿" if _number(predicted) is not None else None,
            source=market.get("turnover_source", "TurnoverTracker"),
            quote_date=market.get("quote_date"),
            as_of=market.get("turnover_as_of"),
            scope=market.get("turnover_scope", "沪深两市"),
            available=_number(predicted) is not None,
        ),
        EvidenceCard(
            "沪指涨跌",
            f"{_number(change):+.2f}%" if _number(change) is not None else None,
            source=market.get("index_source"),
            quote_date=market.get("quote_date"),
            available=_number(change) is not None,
        ),
        EvidenceCard(
            "行业板块上涨占比",
            f"{_number(industry):.0%}" if _number(industry) is not None else None,
            source=market.get("breadth_source"),
            quote_date=market.get("quote_date"),
            scope="行业板块",
            available=_number(industry) is not None,
        ),
        EvidenceCard(
            "概念板块上涨占比",
            f"{_number(concept):.0%}" if _number(concept) is not None else None,
            source=market.get("breadth_source"),
            quote_date=market.get("quote_date"),
            scope="概念板块",
            available=_number(concept) is not None,
        ),
    )


def build_decision_review(market: Optional[dict], overview: Optional[dict]) -> dict[str, list[str]]:
    """从已观测字段列出支持、反向、缺失和失效条件。"""
    market = market or {}
    overview = overview or {}
    quality = market.get("market_environment") or market.get("quality")
    status = str(_quality_value(market, quality, "status", ""))
    if hasattr(status, "value"):
        status = status.value
    predicted = _number(_quality_value(market, quality, "predicted_turnover_yi"))
    change = _number(_quality_value(market, quality, "market_change_pct"))
    industry = _number(_quality_value(market, quality, "industry_breadth"))
    concept = _number(_quality_value(market, quality, "concept_breadth"))
    near_high = _quality_value(market, quality, "index_near_high")
    persistence = _quality_value(market, quality, "persistence_days")
    average = _number(_quality_value(market, quality, "persistence_average_yi"))

    supporting: list[str] = []
    counter: list[str] = []
    missing: list[str] = []
    invalidators: list[str] = []

    if predicted is None:
        missing.append("全天预估成交额")
    elif predicted >= 20_000:
        supporting.append(f"全天预估成交额约{predicted:,.0f}亿，活跃度达到高量观察线")
    else:
        counter.append(f"全天预估成交额约{predicted:,.0f}亿，尚未达到2万亿高量观察线")

    if change is None:
        missing.append("沪指涨跌")
    elif change >= 0:
        supporting.append(f"沪指{change:+.2f}%，指数方向暂未形成明显拖累")
    else:
        counter.append(f"沪指{change:+.2f}%，指数方向对冲高延续不利")

    for label, value in (("行业板块宽度", industry), ("概念板块宽度", concept)):
        if value is None:
            missing.append(label)
        elif value >= 0.60:
            supporting.append(f"{label}{value:.0%}，扩散度达到参考线")
        else:
            counter.append(f"{label}{value:.0%}，板块扩散不足")

    if near_high is None:
        missing.append("指数日内位置")
    elif near_high:
        supporting.append("指数接近日内高位，暂未观察到明显冲高回落")
    else:
        counter.append("指数未能接近日内高位，存在滞涨或放量分歧")

    if persistence is None:
        missing.append("近期完整收盘日量能连续性")
    elif persistence >= 3 and average is not None and average >= 18_000:
        supporting.append(f"此前完整收盘日连续{persistence}日高量，最近3日均值约{average:,.0f}亿")
    else:
        counter.append("近期高量连续性尚未达到确认条件，单日放量仍需观察")

    flow_text = str(market.get("capital_flow") or "")
    if "净流入" in flow_text:
        supporting.append("可取得的资金流快照出现净流入线索，仅作辅助确认")
    elif "净流出" in flow_text:
        counter.append("可取得的资金流快照出现净流出线索，仅作辅助确认")
    elif flow_text and "未取到" not in flow_text and "无当日" not in flow_text:
        missing.append("资金流方向")

    if status == "强势延续":
        invalidators.append("若指数脱离日内高位、板块宽度明显收窄或量能回落，当前延续判断需要重评")
    elif status == "高成交额待确认":
        invalidators.append("若后续量能不能延续，或指数/板块出现滞涨，单日高量不再支持继续等待")
    elif status == "放量分歧":
        invalidators.append("若放量分歧转为指数和板块同步走弱，风险信号优先")

    trend = overview.get("trend") or {}
    if int(trend.get("risk_break") or 0) > 0:
        counter.append(f"持仓中有{int(trend.get('risk_break'))}只出现破位，个股风险优先于市场环境")
    return {
        "supporting": supporting,
        "counter": counter,
        "missing": missing,
        "invalidators": invalidators,
    }


def _observation_date(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    if not text:
        return None
    return text[:10]


def update_t_filter_event_outcome(event: dict, current_price: Any, observed_at: Any) -> dict:
    price = _number(current_price)
    if price is None or price <= 0:
        event["outcome_status"] = "数据不足"
        return event
    trigger = _number(event.get("trigger_price"))
    if trigger is None or trigger <= 0:
        event["outcome_status"] = "数据不足"
        return event
    observed_date = _observation_date(observed_at) or _observation_date(event.get("date"))
    observations = list(event.get("observations") or [])
    if observed_date:
        observations = [item for item in observations if item.get("date") != observed_date]
        observations.append({
            "date": observed_date,
            "observed_at": str(observed_at),
            "price": price,
        })
    event["observations"] = observations[-10:]
    event["current_price"] = price
    event["current_change_pct"] = (price / trigger - 1) * 100
    prices = [
        _number(item.get("price")) for item in event["observations"]
        if _number(item.get("price")) is not None
    ]
    prices.append(price)
    event["max_favorable_pct"] = (max(prices) / trigger - 1) * 100
    event["max_adverse_pct"] = (min(prices) / trigger - 1) * 100
    event["current_updated_at"] = str(observed_at)
    if observed_date and event["date"] != observed_date:
        dated = sorted({item.get("date") for item in event["observations"] if item.get("date")})
        try:
            offset = dated.index(observed_date) - dated.index(event["date"])
        except ValueError:
            offset = 0
        if offset >= 1:
            event["day_1_price"] = price if offset == 1 else event.get("day_1_price", price)
        if offset >= 3:
            event["day_3_price"] = price if offset == 3 else event.get("day_3_price", price)
        if offset >= 5:
            event["day_5_price"] = price if offset == 5 else event.get("day_5_price", price)
    event["outcome_status"] = "观察中"
    return event


def summarize_t_filter_outcomes(events: Iterable[dict]) -> str:
    events = list(events or [])
    if not events:
        return ""
    observed = sum(1 for event in events if event.get("outcome_status") == "观察中")
    insufficient = sum(1 for event in events if event.get("outcome_status") == "数据不足")
    parts = []
    if observed:
        parts.append(f"观察中{observed}条")
    if insufficient:
        parts.append(f"数据不足{insufficient}条")
    return "；".join(parts) if parts else "暂未形成可用观察结果"
