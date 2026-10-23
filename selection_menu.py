#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from dataclasses import dataclass
from datetime import datetime
from typing import List, Mapping


@dataclass
class SelectionSections:
    promoted: List[str]
    near_entry: List[str]
    observing: List[str]
    today_alerts: List[str]
    promoted_count: int
    near_entry_count: int
    observing_count: int
    today_alert_count: int


def _candidate_state(record):
    state = record.get("state")
    if state:
        return state
    if record.get("entry_confirmed_at"):
        return "BUY_CANDIDATE"
    return "NEAR_ENTRY" if record.get("status") == "ready" else "OBSERVING"


def _is_visible(record):
    return (
        record.get("data_status") == "已复核"
        and record.get("status") in {None, "observing", "ready"}
        and _candidate_state(record) != "ELIMINATED"
    )


def _line(record, icon):
    price = record.get("price")
    price_text = f"{price:.2f}" if isinstance(price, (int, float)) else "--"
    days = record.get("observation_days", 0)
    rounds = record.get("confirmation_rounds", 0)
    reason = record.get("reason") or ""
    detail = f"（{reason}）" if reason else ""
    return (
        f"{icon} {record.get('name', record.get('code', '--'))}({record.get('code', '--')})"
        f" | ¥{price_text} | 第{days}天 | 确认{rounds}轮{detail}"
    )


def build_selection_sections(
    records: Mapping[str, Mapping],
    at: datetime,
    per_group: int = 8,
) -> SelectionSections:
    if per_group < 0:
        raise ValueError("per_group 不能为负数")

    visible = [dict(record) for record in records.values() if _is_visible(record)]

    def latest(record):
        return record.get("last_reviewed_date") or record.get("first_observed_date") or ""

    visible.sort(key=latest, reverse=True)

    promoted = [
        record for record in visible if _candidate_state(record) == "BUY_CANDIDATE"
    ]
    near_entry = [
        record for record in visible if _candidate_state(record) == "NEAR_ENTRY"
    ]
    observing = [
        record for record in visible if _candidate_state(record) == "OBSERVING"
    ]

    today = at.strftime("%Y-%m-%d")
    today_alerts = []
    for record in visible:
        alert = record.get("last_entry_alert")
        if isinstance(alert, dict) and alert.get("date") == today:
            today_alerts.append(_line(record, "📋"))

    return SelectionSections(
        promoted=[_line(record, "🎯") for record in promoted[:per_group]],
        near_entry=[_line(record, "🟡") for record in near_entry[:per_group]],
        observing=[_line(record, "⚪") for record in observing[:per_group]],
        today_alerts=today_alerts[:per_group],
        promoted_count=len(promoted),
        near_entry_count=len(near_entry),
        observing_count=len(observing),
        today_alert_count=len(today_alerts),
    )
