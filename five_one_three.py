"""513 低位形态选股。

与 Android 版 analysis/FiveOneThreeScreener.kt 逐条对应：
5 连阳（量能温和、收盘站上 5 日线且 5 日线走高）→ 倍量阳线启动 → 3 天缩量回踩确认。

只使用最近完整交易日的 K 线，不把盘中未完成 K 线直接判为确认。
"""

from __future__ import annotations

import math
from enum import IntEnum
from typing import Any, Dict, List, Optional

MIN_HISTORY_DAYS = 60
FIVE_DAYS = 5
CONFIRMATION_DAYS = 3
BASELINE_DAYS = 20
MOVING_AVERAGE_DAYS = 5
MIN_SINGLE_GAIN = 0.01
MAX_SINGLE_GAIN = 0.03
MAX_GAIN_60D_PCT = 30.0
MAX_RANGE_POSITION = 0.85
MAX_OBSERVATION_VOLUME_RATIO = 1.8
MIN_OBSERVATION_VOLUME_RATIO = 0.65
DOUBLE_VOLUME_RATIO = 2.0
MAX_PULLBACK_RATIO = 0.06
MAX_CONFIRMATION_AMPLITUDE = 0.05
# 信号日（含确认）距最新完整交易日超过这个天数，形态不再列出——513 是短线形态，
# 十天前的「形态已确认」不该挂在列表里冒充今天的买点。
MAX_PATTERN_AGE_DAYS = 3


class FiveOneThreeStage(IntEnum):
    """形态阶段，ordinal 与 Android 版一致（用于排序：已确认 > 等待确认 > 观察中）。"""

    OBSERVING = 0
    WAITING_CONFIRMATION = 1
    CONFIRMED = 2


def _mean(values: List[float]) -> float:
    """空序列返回 NaN，与 Kotlin List.average() 一致（比较运算全部为 False）。"""
    return sum(values) / len(values) if values else float("nan")


def _date_text(value: Any) -> str:
    """datetime / date / 字符串统一成 YYYY-MM-DD。"""
    return str(value)[:10]


def _moving_average(bars: List[Dict[str, Any]], end: int, days: int) -> float:
    start = max(0, end - days + 1)
    return _mean([bar["close"] for bar in bars[start:end + 1]])


def _is_five_bullish(bars: List[Dict[str, Any]], end: int) -> bool:
    """5 连阳：每根小阳线且量能温和，5 日均量不过度萎缩，收盘站上 5 日线且 5 日线走高。"""
    if end < FIVE_DAYS - 1:
        return False
    start = end - FIVE_DAYS + 1
    five = bars[start:end + 1]
    baseline_volumes = [
        bar["volume"] for bar in bars[max(0, start - BASELINE_DAYS):start]
    ]
    # 窗口前没有基线成交量时直接拒绝，不能静默跳过量能约束
    # （Android 版此处空列表 average()=NaN、比较全为 False 会放行，这里收紧）。
    if not baseline_volumes:
        return False
    baseline = _mean(baseline_volumes)
    if baseline <= 0.0:
        return False
    for bar in five:
        gain = bar["close"] / bar["open"] - 1.0
        if (
            bar["close"] <= bar["open"]
            or gain < MIN_SINGLE_GAIN
            or gain > MAX_SINGLE_GAIN
            or bar["volume"] > baseline * MAX_OBSERVATION_VOLUME_RATIO
        ):
            return False
    if _mean([bar["volume"] for bar in five]) < baseline * MIN_OBSERVATION_VOLUME_RATIO:
        return False
    # 收盘站上 5 日线（无容差），且窗口内 5 日线整体走高（末日 MA5 ≥ 首日 MA5）
    return all(
        bar["close"] >= _moving_average(bars, start + offset, MOVING_AVERAGE_DAYS)
        for offset, bar in enumerate(five)
    ) and _moving_average(bars, end, MOVING_AVERAGE_DAYS) >= _moving_average(
        bars, start, MOVING_AVERAGE_DAYS
    )


def _is_double_volume_bullish(bars: List[Dict[str, Any]], index: int) -> bool:
    """倍量阳：量能翻倍的启动阳线，收盘站上 5 日线且上影线不长。"""
    if index <= 0 or index >= len(bars):
        return False
    bar = bars[index]
    if (
        bar["close"] <= bar["open"]
        or bar["volume"] < bars[index - 1]["volume"] * DOUBLE_VOLUME_RATIO
    ):
        return False
    if bar["close"] < _moving_average(bars, index, MOVING_AVERAGE_DAYS):
        return False
    return (
        (bar["high"] - bar["close"]) / max(bar["high"], bar["close"])
        <= MAX_PULLBACK_RATIO
    )


def _is_shrinking_confirmation(
    bars: List[Dict[str, Any]], signal_index: int, index: int
) -> bool:
    """缩量确认：量能递减且不破倍量阳开盘价，振幅收敛。"""
    if index <= signal_index or index >= len(bars):
        return False
    bar = bars[index]
    previous = bars[index - 1]
    signal = bars[signal_index]
    amplitude = (bar["high"] - bar["low"]) / max(previous["close"], 0.0001)
    return (
        bar["volume"] < previous["volume"]
        and bar["volume"] < signal["volume"]
        and bar["close"] >= signal["open"]
        and amplitude <= MAX_CONFIRMATION_AMPLITUDE
    )


def _candidate(
    code: str,
    name: str,
    price: float,
    stage: FiveOneThreeStage,
    confirmations: int,
    gain60: float,
) -> Dict[str, Any]:
    return {
        "code": code,
        "name": name,
        "price": price,
        "stage": stage,
        "progress": {
            FiveOneThreeStage.OBSERVING: "5连阳观察",
            FiveOneThreeStage.WAITING_CONFIRMATION: "等待3天缩量",
            FiveOneThreeStage.CONFIRMED: "形态已确认",
        }[stage],
        "detail": (
            f"5连阳 5/5｜倍量阳 {'0' if stage == FiveOneThreeStage.OBSERVING else '1'}/1"
            f"｜缩量确认 {confirmations}/3"
        ),
        "gain60": gain60,
    }


def evaluate(
    code: str,
    name: str,
    bars: List[Dict[str, Any]],
    today: Optional[str] = None,
    current_price: float = float("nan"),
) -> Optional[Dict[str, Any]]:
    """扫描一只股票的 513 形态，返回最近一次候选命中；无命中返回 None。"""
    return inspect(code, name, bars, today, current_price)["hit"]


def inspect(
    code: str,
    name: str,
    bars: List[Dict[str, Any]],
    today: Optional[str] = None,
    current_price: float = float("nan"),
) -> Dict[str, Any]:
    """诊断式扫描，与 Android 版 FiveOneThreeScreener.inspect 对应。

    除命中结果外还报告漏斗状态，供全市场扫描统计：
    - history_available: 完整日线是否够 60 根
    - historical_five_bullish: 序列内是否出现过 5 连阳（不管之后走没走出来）
    - recent_five_bullish: 最近一个完整交易日是否正处于 5 连阳

    bars: 日线序列，每项含 date/open/high/low/close/volume；
    today: 需要排除的未完成交易日（YYYY-MM-DD），盘中不会把当日 K 线判为确认；
    current_price: 现价，取不到时用最近完整交易日收盘价。
    """
    completed = [
        bar
        for bar in bars
        if _date_text(bar["date"]) != today
        and bar["close"] > 0.0
        and bar["open"] > 0.0
        and bar["volume"] > 0.0
    ]
    if len(completed) < MIN_HISTORY_DAYS:
        return {
            "history_available": False,
            "historical_five_bullish": False,
            "recent_five_bullish": False,
            "hit": None,
        }
    if "ST" in name.upper():
        return {
            "history_available": True,
            "historical_five_bullish": False,
            "recent_five_bullish": False,
            "hit": None,
        }

    historical_five_bullish = any(
        _is_five_bullish(completed, end) for end in range(FIVE_DAYS - 1, len(completed))
    )
    recent_five_bullish = _is_five_bullish(completed, len(completed) - 1)
    return {
        "history_available": True,
        "historical_five_bullish": historical_five_bullish,
        "recent_five_bullish": recent_five_bullish,
        "hit": _evaluate_completed(code, name, completed, current_price),
    }


def _evaluate_completed(
    code: str,
    name: str,
    completed: List[Dict[str, Any]],
    current_price: float,
) -> Optional[Dict[str, Any]]:
    window = completed[-60:]
    base_close = window[0]["close"]
    latest_close = window[-1]["close"]
    gain60 = (latest_close / base_close - 1.0) * 100.0
    window_high = max(bar["high"] for bar in window)
    window_low = min(bar["low"] for bar in window)
    span = window_high - window_low
    position = (latest_close - window_low) / span if span > 0.0 else 1.0
    if gain60 > MAX_GAIN_60D_PCT or position >= MAX_RANGE_POSITION:
        return None

    last_index = len(completed) - 1
    candidates: List[Dict[str, Any]] = []
    for end in range(FIVE_DAYS - 1, len(completed)):
        if not _is_five_bullish(completed, end):
            continue
        if end == last_index:
            price = current_price if current_price > 0.0 else latest_close
            candidates.append(
                _candidate(
                    code, name, price, FiveOneThreeStage.OBSERVING, 0, gain60
                )
            )
            continue

        signal_index = end + 1
        if not _is_double_volume_bullish(completed, signal_index):
            continue
        available = list(
            range(
                signal_index + 1,
                min(signal_index + CONFIRMATION_DAYS, last_index) + 1,
            )
        )
        confirmations = 0
        for index in available:
            if not _is_shrinking_confirmation(completed, signal_index, index):
                break
            confirmations += 1
        if confirmations == CONFIRMATION_DAYS:
            stage = FiveOneThreeStage.CONFIRMED
        elif confirmations == len(available) and confirmations > 0:
            stage = FiveOneThreeStage.WAITING_CONFIRMATION
        elif not available:
            stage = FiveOneThreeStage.WAITING_CONFIRMATION
        else:
            continue
        # 513 是短线形态：倍量阳信号日（含缩量确认）距今太远的不再列出。
        decisive_index = signal_index + confirmations
        if last_index - decisive_index > MAX_PATTERN_AGE_DAYS:
            continue
        price = current_price if current_price > 0.0 else latest_close
        candidates.append(_candidate(code, name, price, stage, confirmations, gain60))
    return candidates[-1] if candidates else None


def order_five_one_three_hits(hits: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """阶段倒序（已确认在前），同阶段按近60日涨幅降序，再按名称升序。与 Android 版一致。"""

    def _stage_value(item: Dict[str, Any]) -> int:
        stage = item.get("stage")
        try:
            return int(stage)
        except (TypeError, ValueError):
            return -1

    def _gain_value(item: Dict[str, Any]) -> float:
        try:
            value = float(item.get("gain60"))
        except (TypeError, ValueError):
            return float("-inf")
        return float("-inf") if math.isnan(value) else value

    # Python 的 sorted 按 key 升序，用负号把「阶段/涨幅」转成降序；
    # 名称保持升序，等价于 Kotlin 的 compareByDescending{stage}.thenByDescending{gain}.thenBy{name}。
    return sorted(
        hits,
        key=lambda item: (-_stage_value(item), -_gain_value(item), str(item.get("name") or "")),
    )
