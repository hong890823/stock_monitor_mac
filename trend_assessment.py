#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""统一短期趋势、大趋势和普通交易门控。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, time
from enum import Enum
import math
from typing import Any, Optional

import pandas as pd

from entry_structure import compute_entry_input, evaluate
from portfolio_addition import resample_ohlcv


class ShortTrend(str, Enum):
    MISSING = "数据不足"
    EXTENDED = "连续拉升"
    DOWN = "近期偏弱"
    UP = "近期向上"
    REBOUND = "修复反弹"
    BOX = "箱体震荡"
    RANGE = "横盘震荡"


class BoxState(str, Enum):
    MISSING = "箱体数据不足"
    NOT_BOX = "非箱体"
    BOX = "箱体震荡"
    REJECTED_BREAKOUT = "假突破/回到箱体"
    UPSIDE_BREAKOUT = "向上突破箱体"
    DOWNSIDE_BREAKOUT = "向下跌破箱体"


class MajorTrend(str, Enum):
    MISSING = "大趋势数据不足"
    DOWN = "大趋势向下"
    BOTTOMING = "筑底阶段"
    REPAIR = "修复阶段"
    UP = "大趋势向上"


class TrendConflict(str, Enum):
    MISSING_DATA = "数据不足"
    DOWN_MAJOR_REBOUND = "下跌趋势中的反弹"
    BOTTOM_REVERSAL_WATCH = "底部反转观察"
    ALIGNED_UP = "长短趋势同步向上"
    NO_CONFIRMED_ALIGNMENT = "趋势尚未形成完整共振"


@dataclass(frozen=True)
class TrendEvidence:
    ma5: Optional[float] = None
    ma10: Optional[float] = None
    ma20: Optional[float] = None
    ma60: Optional[float] = None
    ma20_prev5: Optional[float] = None
    atr: Optional[float] = None
    atr_pct: Optional[float] = None
    change_5d_pct: Optional[float] = None
    up_days_5d: Optional[int] = None
    closes_below_ma20: int = 0
    trend_weak: bool = False
    weekly_direction: str = "数据不足"
    monthly_direction: str = "数据不足"
    ma250: Optional[float] = None
    five_day_no_new_low: Optional[bool] = None
    box_state: str = BoxState.MISSING.value
    box_upper: Optional[float] = None
    box_lower: Optional[float] = None
    box_width_pct: Optional[float] = None
    box_days: Optional[int] = None
    data_as_of: str = ""


@dataclass(frozen=True)
class BoxAssessment:
    state: BoxState
    upper: Optional[float]
    lower: Optional[float]
    width_pct: Optional[float]
    high_touches: int
    low_touches: int
    days: int
    reasons: tuple[str, ...]
    missing_data: tuple[str, ...]


@dataclass(frozen=True)
class TrendAssessment:
    short_trend: ShortTrend
    major_trend: MajorTrend
    conflict: TrendConflict
    reasons: tuple[str, ...]
    missing_data: tuple[str, ...]
    evidence: TrendEvidence
    summary: str


@dataclass(frozen=True)
class TradePermissions:
    allow_normal_buy: bool = False
    allow_add: bool = False
    allow_reentry: bool = False
    allow_t: bool = False
    allow_probe: bool = False
    allow_risk_action: bool = True
    risk_only: bool = False
    probe_cap_ratio: float = 0.0
    probe_cap_amount: float = 0.0
    blockers: tuple[str, ...] = ()


def _finite(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _mean(values, period):
    selected = [value for value in values[-period:] if math.isfinite(value)]
    return sum(selected) / len(selected) if len(selected) == period else None


def _period_direction(bars):
    closes = [float(bar.close) for bar in bars]
    if len(closes) < 3:
        return "数据不足"
    period = min(20, len(closes) - 1)
    current_ma = sum(closes[-period:]) / period
    previous_ma = sum(closes[-period - 1:-1]) / period
    if closes[-1] > current_ma and current_ma > previous_ma * 1.0001:
        return "上行"
    if closes[-1] < current_ma and current_ma < previous_ma * 0.9999:
        return "下行"
    return "震荡"


def _validated_history(history):
    if not isinstance(history, pd.DataFrame):
        raise ValueError("history must be a pandas DataFrame")
    required = {"date", "open", "high", "low", "close", "volume"}
    missing = required - set(history.columns)
    if missing:
        raise ValueError("history missing columns: " + ",".join(sorted(missing)))
    frame = history.copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    for column in ("open", "high", "low", "close", "volume"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=list(required))
    frame = frame[frame["volume"] >= 0]
    valid_range = (
        (frame["low"] <= frame[["open", "close", "high"]].min(axis=1))
        & (frame["high"] >= frame[["open", "close", "low"]].max(axis=1))
    )
    return frame[valid_range].sort_values("date").reset_index(drop=True)


def _quantile(values, fraction):
    ordered = sorted(values)
    if not ordered:
        return None
    index = round((len(ordered) - 1) * fraction)
    return ordered[index]


def _box_geometry(rows):
    highs = [float(row["high"]) for row in rows]
    lows = [float(row["low"]) for row in rows]
    closes = [float(row["close"]) for row in rows]
    upper = _quantile(highs, 0.90)
    lower = _quantile(lows, 0.10)
    if upper is None or lower is None or upper <= lower:
        return None
    true_ranges = []
    for index, row in enumerate(rows):
        previous_close = closes[index - 1] if index else closes[index]
        true_ranges.append(max(
            float(row["high"]) - float(row["low"]),
            abs(float(row["high"]) - previous_close),
            abs(float(row["low"]) - previous_close),
        ))
    atr = sum(true_ranges) / len(true_ranges)
    midpoint = (upper + lower) / 2
    tolerance = max(midpoint * 0.005, atr * 0.25)
    width_pct = (upper - lower) / lower * 100
    high_touches = sum(value >= upper - tolerance for value in highs)
    low_touches = sum(value <= lower + tolerance for value in lows)
    contained = sum(
        float(row["high"]) <= upper + tolerance
        and float(row["low"]) >= lower - tolerance
        for row in rows
    )
    contained_ratio = contained / len(rows)
    qualifies = (
        3.0 <= width_pct <= 20.0
        and high_touches >= 2
        and low_touches >= 2
        and contained_ratio >= 0.80
        and all(lower <= value <= upper for value in closes[-3:])
    )
    return {
        "upper": upper,
        "lower": lower,
        "width_pct": width_pct,
        "high_touches": high_touches,
        "low_touches": low_touches,
        "days": len(rows),
        "qualifies": qualifies,
    }


def assess_box(history, now=None) -> BoxAssessment:
    if not isinstance(history, pd.DataFrame):
        history = pd.DataFrame(history or [])
    try:
        frame = _validated_history(history)
    except (TypeError, ValueError) as error:
        return BoxAssessment(
            BoxState.MISSING, None, None, None, 0, 0, 0, (), (str(error),),
        )
    if len(frame) < 20:
        return BoxAssessment(
            BoxState.MISSING, None, None, None, 0, 0, len(frame),
            (), ("有效日线不足20根",),
        )

    window = frame.tail(40).to_dict("records")
    last = window[-1]
    reference = window[:-1] if len(window) >= 21 else window
    geometry = _box_geometry(reference)
    if geometry is None or not geometry["qualifies"]:
        geometry = _box_geometry(window)
        if geometry is None or not geometry["qualifies"]:
            return BoxAssessment(
                BoxState.NOT_BOX, None, None, None, 0, 0, len(window),
                ("区间宽度、上下沿触及次数或箱内占比不足",), (),
            )
        return BoxAssessment(
            BoxState.BOX,
            geometry["upper"], geometry["lower"], geometry["width_pct"],
            geometry["high_touches"], geometry["low_touches"], geometry["days"],
            ("区间宽度3%至20%，上下沿均至少两次触及",), (),
        )

    upper = geometry["upper"]
    lower = geometry["lower"]
    tolerance = max((upper + lower) / 2 * 0.005, (upper - lower) * 0.03)
    close = float(last["close"])
    if close > upper * 1.01 and float(last["high"]) > upper + tolerance:
        state = BoxState.UPSIDE_BREAKOUT
    elif close < lower * 0.99 and float(last["low"]) < lower - tolerance:
        state = BoxState.DOWNSIDE_BREAKOUT
    elif (
        float(last["high"]) > upper + tolerance
        or float(last["low"]) < lower - tolerance
    ):
        state = BoxState.REJECTED_BREAKOUT
    else:
        state = BoxState.BOX
    return BoxAssessment(
        state,
        upper, lower, geometry["width_pct"],
        geometry["high_touches"], geometry["low_touches"], geometry["days"],
        (f"箱体参考{geometry['days']}个交易日",), (),
    )


def assess_trend(history, price=None, now=None, benchmark_ok=None) -> TrendAssessment:
    now = now or datetime.now()
    if not isinstance(history, pd.DataFrame):
        history = pd.DataFrame(history or [])
    try:
        frame = _validated_history(history)
    except (TypeError, ValueError) as error:
        evidence = TrendEvidence(data_as_of="")
        return TrendAssessment(
            ShortTrend.MISSING, MajorTrend.MISSING, TrendConflict.MISSING_DATA,
            (), (str(error),), evidence, "历史数据无效，趋势无法判断。",
        )

    today = now.date()
    completed_mask = (frame["date"].dt.date < today) | (
        (frame["date"].dt.date == today) & (now.time() >= time(15, 0))
    )
    completed = frame[completed_mask].copy()
    missing = []
    reasons = []
    closes = completed["close"].astype(float).tolist()
    highs = completed["high"].astype(float).tolist()
    lows = completed["low"].astype(float).tolist()
    volumes = completed["volume"].astype(float).tolist()
    current_price = _finite(price)
    if current_price is None:
        current_price = closes[-1] if closes else None
    if current_price is None or current_price <= 0:
        missing.append("现价缺失或无效")

    entry_input = None
    entry = None
    if len(closes) >= 25 and current_price:
        entry_input = compute_entry_input(
            closes, highs, lows, volumes, price=current_price,
            benchmark_ok=benchmark_ok,
        )
        entry = evaluate(entry_input) if entry_input is not None else None
    else:
        missing.append("完整日线不足25根")
    box = assess_box(completed.to_dict("records"), now=now)

    ma5 = _mean(closes, 5)
    ma10 = _mean(closes, 10)
    ma20 = _mean(closes, 20)
    ma60 = _mean(closes, 60)
    atr = entry_input.atr if entry_input else None
    atr_pct = atr / current_price * 100 if atr and current_price else None
    recent = entry_input.recent_trend if entry_input else None

    short_trend = ShortTrend.MISSING
    if entry_input is not None and current_price:
        alignment_up = ma5 is not None and ma10 is not None and ma20 is not None and current_price >= ma5 and ma5 > ma10 > ma20
        ma20_up = ma20 and entry_input.ma20_prev and ma20 > entry_input.ma20_prev * 1.001
        change_5d = recent.change_5d_pct if recent else None
        up_days = recent.up_days_5d if recent else None
        extended_threshold = max(6.0, 3 * atr_pct) if atr_pct is not None else 6.0
        if change_5d is not None and up_days is not None and change_5d >= extended_threshold and up_days >= 3:
            short_trend = ShortTrend.EXTENDED
            reasons.append(f"近5日上涨{change_5d:.2f}%且上涨{up_days}天，达到连续拉升阈值{extended_threshold:.2f}%")
        elif entry.trend_weak:
            short_trend = ShortTrend.DOWN
            reasons.append("价格有效跌破M20，近期趋势偏弱")
        elif alignment_up and ma20_up:
            short_trend = ShortTrend.UP
            reasons.append("价格站上MA5，MA5>MA10>MA20且M20上行")
        else:
            prior_close = closes[-2] if len(closes) >= 2 else None
            ma5_prev = _mean(closes[:-1], 5)
            if prior_close and current_price > prior_close and ma5 and ma5_prev and ma5 > ma5_prev:
                short_trend = ShortTrend.REBOUND
                reasons.append("价格与MA5转上，但尚未形成完整多头排列")
            else:
                short_trend = ShortTrend.RANGE
                reasons.append("均线与近5日表现尚未形成明确方向")
        if short_trend in {ShortTrend.RANGE, ShortTrend.REBOUND} and box.state in {
            BoxState.BOX, BoxState.REJECTED_BREAKOUT
        }:
            short_trend = ShortTrend.BOX
            reasons.append("价格在明确上下沿之间往返，识别为箱体震荡")
        if box.state in {
            BoxState.UPSIDE_BREAKOUT, BoxState.DOWNSIDE_BREAKOUT
        }:
            reasons.append(box.state.value)

    weekly = monthly = None
    ma250 = None
    if len(closes) >= 250:
        ma250 = sum(closes[-250:]) / 250
    else:
        missing.append("有效日线不足250根")
    try:
        weekly = resample_ohlcv(frame, "W", now).completed
        monthly = resample_ohlcv(frame, "M", now).completed
    except (TypeError, ValueError):
        weekly, monthly = (), ()
    if len(weekly) < 60:
        missing.append("完整周线不足60根")
    if len(monthly) < 12:
        missing.append("完整月线不足12根")
    weekly_direction = _period_direction(weekly) if weekly else "数据不足"
    monthly_direction = _period_direction(monthly) if monthly else "数据不足"

    no_new_low = None
    if len(lows) >= 25:
        prior_low = min(lows[-25:-5])
        no_new_low = all(value >= prior_low for value in lows[-5:])

    major_trend = MajorTrend.MISSING
    if not missing and current_price and ma250 is not None:
        weak_context = weekly_direction == "下行" or monthly_direction == "下行" or current_price < ma250
        if weekly_direction == "上行" and monthly_direction in {"上行", "震荡"} and current_price >= ma250:
            major_trend = MajorTrend.UP
            reasons.append("周线上行、月线不弱且价格站上MA250")
        elif (
            (weekly_direction == "下行" and monthly_direction == "下行")
            or (current_price < ma250 and weekly_direction != "上行" and monthly_direction != "上行")
        ):
            major_trend = MajorTrend.DOWN
            reasons.append("周月线偏弱或价格跌破MA250")
        elif (
            weak_context and no_new_low and ma20 and entry_input
            and ma20 >= entry_input.ma20_prev * 0.999
            and current_price > ma20 and weekly_direction != "下行"
        ):
            major_trend = MajorTrend.BOTTOMING
            reasons.append("近5日不再创新低、M20走平转上且价格站上M20")
        else:
            major_trend = MajorTrend.REPAIR
            reasons.append("周月线或MA250尚未完全转强，暂按修复阶段观察")

    if major_trend == MajorTrend.MISSING or short_trend == ShortTrend.MISSING:
        conflict = TrendConflict.MISSING_DATA
        summary = "历史数据不足，只观察，不生成趋势交易结论。"
    elif major_trend == MajorTrend.DOWN and short_trend in {ShortTrend.UP, ShortTrend.EXTENDED, ShortTrend.REBOUND}:
        conflict = TrendConflict.DOWN_MAJOR_REBOUND
        summary = "大趋势向下，短期转强只能定义为下跌趋势中的反弹，不按正常上涨趋势交易。"
    elif major_trend == MajorTrend.BOTTOMING and short_trend in {ShortTrend.UP, ShortTrend.EXTENDED, ShortTrend.REBOUND}:
        conflict = TrendConflict.BOTTOM_REVERSAL_WATCH
        summary = "出现底部反转观察信号，只允许轻仓试错，不作为正常上涨趋势。"
    elif major_trend == MajorTrend.UP and short_trend in {ShortTrend.UP, ShortTrend.EXTENDED}:
        conflict = TrendConflict.ALIGNED_UP
        summary = "大趋势与短期趋势同步向上。"
    else:
        conflict = TrendConflict.NO_CONFIRMED_ALIGNMENT
        summary = "趋势尚未形成完整共振，以观察或风险处置为主。"

    evidence = TrendEvidence(
        ma5=ma5, ma10=ma10, ma20=ma20, ma60=ma60,
        ma20_prev5=entry_input.ma20_prev if entry_input else None,
        atr=atr, atr_pct=atr_pct,
        change_5d_pct=recent.change_5d_pct if recent else None,
        up_days_5d=recent.up_days_5d if recent else None,
        closes_below_ma20=entry_input.closes_below_ma20 if entry_input else 0,
        trend_weak=bool(entry.trend_weak) if entry else False,
        weekly_direction=weekly_direction, monthly_direction=monthly_direction,
        ma250=ma250, five_day_no_new_low=no_new_low,
        box_state=box.state.value,
        box_upper=box.upper,
        box_lower=box.lower,
        box_width_pct=box.width_pct,
        box_days=box.days,
        data_as_of=str(completed["date"].iloc[-1].date()) if len(completed) else "",
    )
    return TrendAssessment(
        short_trend, major_trend, conflict, tuple(dict.fromkeys(reasons)),
        tuple(dict.fromkeys(missing)), evidence, summary,
    )


def build_trade_permissions(
    trend, entry_can_buy, high_state, market_ok, sector_ok, timeframe,
    intent, total_capital, initial_entry_pct,
) -> TradePermissions:
    intent = str(intent or "").lower()
    risk_intent = intent in {"risk_exit", "stop", "break_support", "risk"}
    if risk_intent:
        return TradePermissions(allow_risk_action=True, risk_only=True)

    high = str(getattr(high_state, "name", high_state) or "").upper()
    high_safe = high in {"NOT_HIGH", "SECOND_WAVE_CONFIRMED"}
    aligned = trend.major_trend == MajorTrend.UP and trend.short_trend in {ShortTrend.UP, ShortTrend.EXTENDED}
    minutes_ok = bool(
        getattr(timeframe, "complete", False)
        and getattr(timeframe, "m15_confirmations", 0) >= 2
        and getattr(timeframe, "m30_aligned", False)
        and getattr(timeframe, "clearance_confirmed", True)
    )
    environment_ok = market_ok is True and sector_ok is True
    blockers = []
    if not aligned:
        blockers.append("长短趋势未同步向上")
    if not entry_can_buy:
        blockers.append("个股入场结构未成立")
    if not high_safe:
        blockers.append("高位结构不允许普通机会")
    if not environment_ok:
        blockers.append("市场或板块条件缺失/偏弱")
    if not minutes_ok:
        blockers.append("15/30分钟执行条件未完整确认")

    normal = aligned and entry_can_buy and high_safe and environment_ok and minutes_ok
    ordinary_t = aligned and high_safe and environment_ok and minutes_ok
    allow_probe = trend.major_trend == MajorTrend.BOTTOMING and high_safe
    probe_cap = max(0.0, _finite(total_capital) or 0.0) * max(0.0, _finite(initial_entry_pct) or 0.0) / 100 * 0.25
    return TradePermissions(
        allow_normal_buy=normal and intent == "buy",
        allow_add=normal and intent in {"add", "buy"},
        allow_reentry=ordinary_t and intent in {"reentry", "t_buy", "buyback", "do_t"},
        allow_t=ordinary_t and intent in {"t_sell", "t_buy", "do_t"},
        allow_probe=allow_probe and intent in {"probe_buy", "bottom_watch"},
        risk_only=trend.major_trend == MajorTrend.DOWN,
        probe_cap_ratio=0.25 if allow_probe else 0.0,
        probe_cap_amount=probe_cap if allow_probe else 0.0,
        blockers=tuple(dict.fromkeys(blockers)),
    )


def trend_assessment_to_dict(value: TrendAssessment) -> dict:
    result = asdict(value)
    result["short_trend"] = value.short_trend.value
    result["major_trend"] = value.major_trend.value
    result["conflict"] = value.conflict.value
    return result
