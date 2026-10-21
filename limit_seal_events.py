#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""持仓封板 / 炸板 / 回封事件：只认跃迁，每段各报一次。

2026-10-22 华天科技 14:00 封板、15:00 尾盘炸板，监控全程零提醒——
封板类事件此前只有板块龙头（sector_leaders）在管，持仓股没人管。

判定复用 sector_leaders 的封板口径：价格贴涨停价 **且** 卖一为空才算封死
（触板未封不算）；炸板 = 封死后开板；回封 = 炸板后再封死。每段每种事件
只报一次（跃迁制），每日每只封板/炸板/回封各事件有上限兜底抖动。
只提示事实，不带买卖指令。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from sector_leaders import effective_limit_pct, limit_up_price

MAX_EVENTS_PER_DAY = 8


@dataclass
class SealTick:
    """一轮行情快照（持仓股行情即可，无需板块）。"""
    code: str
    name: str = ""
    price: float = 0.0
    prev_close: float = 0.0
    ask1_price: Optional[float] = None   # 卖一价；0 = 卖一为空，None = 数据缺失
    ask1_vol: Optional[float] = None     # 卖一量；0 = 卖一为空，None = 数据缺失
    bid1_vol: float = 0.0     # 买一量；封板时即封单量


@dataclass
class SealEvent:
    kind: str                 # "封板" | "炸板" | "回封"
    code: str
    name: str
    price: float
    limit_price: float
    seal_volume: float = 0.0  # 封单量（股）；仅封板/回封有值
    blowout_count: int = 0    # 当日累计炸板次数（回封不清零、跨日清零）


class _SealState:
    __slots__ = ("sealed", "blowout_count", "event_count")

    def __init__(self) -> None:
        self.sealed = False
        self.blowout_count = 0
        self.event_count = 0


def is_sealed(tick: SealTick) -> bool:
    """封死涨停 = 价格贴涨停价 **且** 卖一为空（触板未封不算）。"""
    limit = limit_up_price(tick.prev_close, effective_limit_pct(tick.code))
    if limit is None or tick.price <= 0:
        return False
    # 贴涨停价（留半分钱容差）；远超涨停价的多为无涨跌幅新股，不当作封板
    if not (limit - 0.005 <= tick.price <= limit + 0.005):
        return False
    # 卖一为空才算封死；卖一价为负（异常/哨兵）视为未知，不认定封死
    if tick.ask1_price is None or tick.ask1_vol is None:
        return False
    if tick.ask1_price != 0 or tick.ask1_vol > 0:
        return False
    return True


class SealEventTracker:
    """封板→炸板→回封 状态机；每段每种事件只出一次。"""

    def __init__(self, max_events_per_day: int = MAX_EVENTS_PER_DAY) -> None:
        self.max_events_per_day = max(1, int(max_events_per_day))
        self._states: dict[str, _SealState] = {}
        self._day: Optional[str] = None

    def update(self, tick: SealTick, day: Optional[str] = None) -> List[SealEvent]:
        """喂一轮行情，返回本轮产生的跃迁事件（可能为空）。"""
        if day and day != self._day:
            self._states.clear()
            self._day = day
        elif self._day is None and day:
            self._day = day

        state = self._states.get(tick.code)
        if state is None:
            state = _SealState()
            self._states[tick.code] = state

        sealed = is_sealed(tick)
        events: List[SealEvent] = []

        def _emit(kind: str) -> None:
            if state.event_count >= self.max_events_per_day:
                return
            state.event_count += 1
            limit = limit_up_price(tick.prev_close, effective_limit_pct(tick.code))
            events.append(SealEvent(
                kind=kind,
                code=tick.code,
                name=tick.name or tick.code,
                price=tick.price,
                limit_price=limit or 0.0,
                seal_volume=tick.bid1_vol if kind != "炸板" else 0.0,
                blowout_count=state.blowout_count,
            ))

        if sealed and not state.sealed:
            # 首段报「封板」，炸过之后再封报「回封」
            _emit("回封" if state.blowout_count > 0 else "封板")
        elif state.sealed and not sealed:
            state.blowout_count += 1
            _emit("炸板")
        state.sealed = sealed
        return events

    def export_state(self) -> dict:
        """持久化抑制类状态（重启恢复防重报，与 dip_rebound 同一套约定）。"""
        return {
            "day": self._day,
            "states": {
                code: {
                    "sealed": state.sealed,
                    "blowout_count": state.blowout_count,
                    "event_count": state.event_count,
                }
                for code, state in self._states.items()
            },
        }

    def restore_state(self, data) -> None:
        """恢复 export_state 的输出；坏数据逐条忽略。"""
        if not isinstance(data, dict):
            return
        day = data.get("day")
        if isinstance(day, str) and day:
            self._day = day
        states = data.get("states")
        if not isinstance(states, dict):
            return
        for code, item in states.items():
            if not isinstance(item, dict):
                continue
            try:
                state = _SealState()
                state.sealed = bool(item.get("sealed"))
                state.blowout_count = int(item.get("blowout_count") or 0)
                state.event_count = int(item.get("event_count") or 0)
            except (TypeError, ValueError):
                continue
            self._states[str(code)] = state
