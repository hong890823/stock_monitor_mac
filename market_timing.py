#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""市场时点与资金证据的统一格式。

这里只整理可核验的上下文，不把周五、月末或节前硬编码成卖出信号。
最终判断交给 AI；持仓报告只展示证据和边界，避免日期效应被误读成确定性结论。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Iterable, Optional


def _as_date(value) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()[:10]
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def _pct(value) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _window_labels(
    day: Optional[date],
    holiday_eve: Optional[bool],
    month_end_near: Optional[bool],
) -> list:
    if day is None:
        return []
    labels = []
    if day.weekday() == 4:
        labels.append("周五")
    if month_end_near is True:
        labels.append("月末附近")
    if holiday_eve is True:
        labels.append("节假日前")
    return labels


def _turnover_history_text(history: Optional[Iterable]) -> str:
    if not history:
        return ""
    rows = []
    for raw_date, raw_value in list(history)[-5:]:
        value = _pct(raw_value)
        if value is None:
            continue
        rows.append(f"{str(raw_date)[:10]} {value:.0f}亿")
    return "｜".join(rows)


def format_market_timing_data(
    date_value,
    *,
    index_today_pct=None,
    index_recent_pct=None,
    turnover_today=None,
    turnover_previous=None,
    turnover_history=None,
    sector_breadth=None,
    sector_breadth_label: str = "板块上涨占比",
    sector_breadth_date: Optional[str] = None,
    sector_breadth_source: Optional[str] = None,
    northbound_net=None,
    northbound_date: Optional[str] = None,
    northbound_3d=None,
    northbound_5d=None,
    northbound_source: Optional[str] = None,
    holiday_eve: Optional[bool] = None,
    month_end_near: Optional[bool] = None,
    turnover_comparison_label: str = "较前一交易日",
    turnover_session: Optional[str] = None,
    turnover_source: Optional[str] = None,
    turnover_scope: Optional[str] = None,
) -> str:
    """格式化交给 AI 和持仓报告的市场时点、资金与量价证据。"""
    day = _as_date(date_value)
    date_text = day.isoformat() if day else str(date_value or "未取到")
    labels = _window_labels(day, holiday_eve, month_end_near)
    window_text = "、".join(labels) if labels else "无明确窗口标签"

    today = _pct(index_today_pct)
    recent = _pct(index_recent_pct)
    index_text = (
        f"沪指今日 {today:+.2f}%" if today is not None else "沪指今日未取到"
    )
    if recent is not None:
        index_text += f"｜近5日累计 {recent:+.2f}%"

    turnover = _pct(turnover_today)
    previous = _pct(turnover_previous)
    if turnover is None:
        turnover_text = "成交额：未取到"
    else:
        turnover_text = f"成交额：{turnover:.0f}亿"
        if previous is not None and previous > 0:
            turnover_text += (
                f"｜{turnover_comparison_label} "
                f"{(turnover / previous - 1) * 100:+.1f}%"
            )
        history_text = _turnover_history_text(turnover_history)
        if history_text:
            turnover_text += f"｜近5日：{history_text}"
    if turnover_session:
        turnover_text += f"｜{turnover_session}"

    northbound = _pct(northbound_net)
    if northbound is not None:
        northbound_text = f"北向净流向：{northbound:+.1f}亿"
        net_3d = _pct(northbound_3d)
        net_5d = _pct(northbound_5d)
        if net_3d is not None:
            northbound_text += f"｜近3日累计 {net_3d:+.1f}亿"
        if net_5d is not None:
            northbound_text += f"｜近5日累计 {net_5d:+.1f}亿"
    else:
        northbound_text = "北向净流向：未取到"
        if northbound_date:
            northbound_text += f"（最新数据日期 {str(northbound_date)[:10]}，与行情日期不一致）"
    breadth = _pct(sector_breadth)
    breadth_text = (
        f"{sector_breadth_label}：{breadth * 100:.0f}%"
        if breadth is not None and 0 <= breadth <= 1
        else f"{sector_breadth_label}：未取到"
    )

    metadata = []
    if turnover_source or turnover_scope:
        turnover_meta = "成交额"
        if turnover_source:
            turnover_meta += f"来源={turnover_source}"
        if turnover_scope:
            turnover_meta += f"、范围={turnover_scope}"
        metadata.append(turnover_meta)
    if northbound_source or northbound_date:
        north_meta = "北向"
        if northbound_source:
            north_meta += f"来源={northbound_source}"
        if northbound_date:
            north_meta += f"、数据日={str(northbound_date)[:10]}"
        metadata.append(north_meta)
    if sector_breadth_source or sector_breadth_date:
        breadth_meta = sector_breadth_label
        if sector_breadth_source:
            breadth_meta += f"来源={sector_breadth_source}"
        if sector_breadth_date:
            breadth_meta += f"、数据日={str(sector_breadth_date)[:10]}"
        metadata.append(breadth_meta)

    lines = [
        f"行情日期：{date_text}｜时点标签：{window_text}",
        index_text,
        turnover_text,
        northbound_text,
        breadth_text,
    ]
    if metadata:
        lines.append("数据口径：" + "；".join(metadata))
    lines.append(
        "边界：时点标签只是风险先验，需结合指数/板块方向、成交额与北向变化判断，"
        "不单独构成卖出信号。"
    )
    return "\n".join(lines)
