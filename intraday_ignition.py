#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""盘中点火：短窗量能突增 + 突破整理平台上沿，封板前的盘面异动提醒。

2026-10-22 华天科技 13:50–13:55 一根 5 分钟量能 ~4 倍于前 30 分钟均量、
价格突破 14.08 的整理平台上沿，约 5 分钟后封板。封板本身追不上，追得上
的是这根点火柱。回测（2022-07~2026-08）显示突破信号跑输无条件买入，
所以这里只提示盘面异动事实，不给买卖指令。

判定（15 秒行情快照驱动，不拉 K 线）：
- 短窗（默认 5 分钟）成交量 ≥ 3 × 基线窗（默认前 30 分钟）的同长均量；
- 且现价突破「爆量前整理平台」上沿 = 基线窗内的最高价；
- 每只每天最多报 1 次（封板通常一天一段，重复点火不刷屏）。
基线窗要攒满才判——开盘头半小时没有基线，不报（早盘脉冲本来就分不出真假）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

SHORT_WINDOW_SEC = 300.0
BASELINE_WINDOW_SEC = 1800.0
VOLUME_BURST_RATIO = 3.0


@dataclass
class IgnitionSample:
    """一轮行情快照。cumulative_volume 为当日累计成交量（股）。"""
    code: str
    name: str
    price: float
    cumulative_volume: float
    timestamp: float


@dataclass
class IgnitionAlert:
    code: str
    name: str
    price: float
    volume_ratio: float
    platform_top: float


class _IgnitionState:
    __slots__ = ("samples", "fired")

    def __init__(self) -> None:
        self.samples: List[Tuple[float, float, float]] = []  # (ts, 累计量, 现价)
        self.fired = False


class IgnitionTracker:
    """按代码滚动采样，量能/平台双条件都满足才报一次。"""

    def __init__(
        self,
        short_window: float = SHORT_WINDOW_SEC,
        baseline_window: float = BASELINE_WINDOW_SEC,
        volume_ratio: float = VOLUME_BURST_RATIO,
    ) -> None:
        self.short_window = float(short_window)
        self.baseline_window = float(baseline_window)
        self.volume_ratio = float(volume_ratio)
        self._states: dict = {}
        self._day = None

    def update(self, sample: IgnitionSample, day: Optional[str] = None) -> Optional[IgnitionAlert]:
        if day and day != self._day:
            self._states.clear()
            self._day = day
        elif self._day is None and day:
            self._day = day

        state = self._states.get(sample.code)
        if state is None:
            state = _IgnitionState()
            self._states[sample.code] = state

        now = float(sample.timestamp)
        price = float(sample.price)
        cum = float(sample.cumulative_volume)
        if not all(map(_finite, (now, price, cum))) or price <= 0 or cum < 0:
            return None

        state.samples.append((now, cum, price))
        horizon = self.short_window + self.baseline_window
        state.samples = [row for row in state.samples if row[0] > now - horizon - 60.0]

        if state.fired:
            return None

        # 基线窗要攒满才算得出均量；否则早盘脉冲全是假信号。
        oldest = state.samples[0][0]
        if oldest > now - horizon:
            return None

        cum_short = _cum_at(state.samples, now - self.short_window)
        cum_base = _cum_at(state.samples, now - horizon)
        if cum_short is None or cum_base is None:
            return None
        short_vol = cum - cum_short
        base_vol = cum_short - cum_base
        base_same_window = base_vol * (self.short_window / self.baseline_window)
        if base_same_window <= 0 or short_vol <= 0:
            return None
        ratio = short_vol / base_same_window
        if ratio < self.volume_ratio:
            return None

        # 整理平台上沿 = 爆量前（基线窗 + 短窗之前）的最高价。
        platform_top = max(
            (row[2] for row in state.samples if row[0] <= now - self.short_window),
            default=None,
        )
        if platform_top is None or price <= platform_top:
            return None

        state.fired = True
        return IgnitionAlert(
            code=sample.code,
            name=sample.name or sample.code,
            price=price,
            volume_ratio=ratio,
            platform_top=platform_top,
        )

    def export_state(self) -> dict:
        """持久化状态（重启恢复防重报，采样缓冲一并落盘）。

        缓冲不落盘的话重启后基线窗要重攒 30 分钟——等于又盲一段；
        恢复后过期采样由 update() 的时间窗裁剪自然丢弃。
        """
        return {
            "day": self._day,
            "states": {
                code: {
                    "fired": state.fired,
                    "samples": [list(row) for row in state.samples],
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
                state = _IgnitionState()
                state.fired = bool(item.get("fired"))
                for row in item.get("samples") or []:
                    ts, cum, price = float(row[0]), float(row[1]), float(row[2])
                    state.samples.append((ts, cum, price))
            except (TypeError, ValueError, IndexError):
                continue
            self._states[str(code)] = state


def _finite(value) -> bool:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return number == number and number not in (float("inf"), float("-inf"))


def _cum_at(samples: List[Tuple[float, float, float]], cutoff: float) -> Optional[float]:
    """cutoff 时刻（含）之前的最新累计量；没有采样返回 None。"""
    result = None
    for ts, cum, _ in samples:
        if ts <= cutoff:
            result = cum
        else:
            break
    return result
