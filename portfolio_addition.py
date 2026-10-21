from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
from statistics import mean
from typing import Any, Literal, Mapping, Optional, Tuple

import pandas as pd


LOT = 100


@dataclass(frozen=True)
class Bar:
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    completed: bool = True


@dataclass(frozen=True)
class FundamentalSnapshot:
    analyzed_at: str
    score: int
    health_level: str
    revenue_yoy: Optional[float]
    profit_yoy: Optional[float]
    deduct_ratio: Optional[float]
    gross_margin: Optional[float]
    net_margin: Optional[float]
    ocf_to_profit: Optional[float]
    roe: Optional[float]
    current_pe: Optional[float]
    current_pb: Optional[float]
    pe_percentile: Optional[float]
    pb_percentile: Optional[float]
    fair_low: Optional[float]
    fair_high: Optional[float]
    risks: Tuple[str, ...] = ()
    notes: Tuple[str, ...] = ()
    quarter_trends: Tuple[Tuple[float, float], ...] = ()


@dataclass(frozen=True)
class HoldingAssessmentInput:
    code: str
    name: str
    quantity: float
    cost: float
    price: float
    total_capital: float
    available_capital: float
    max_position_pct: float
    daily_history: pd.DataFrame
    technical: Mapping[str, Any]
    fundamental: Optional[FundamentalSnapshot]
    missing: Tuple[str, ...] = ()
    initial_entry_pct: float = 5.0


@dataclass(frozen=True)
class AdditionRecommendation:
    code: str
    name: str
    selected: bool
    composite_score: float
    technical_score: float
    fundamental_score: float
    yearly_score: float
    monthly_score: float
    weekly_score: float
    shares: int
    amount: float
    stop_price: float
    position_pct_before: float
    position_pct_after: float
    reasons: Tuple[str, ...]
    missing: Tuple[str, ...]


@dataclass(frozen=True)
class ResampledHistory:
    frequency: Literal["W", "M", "Y"]
    completed: Tuple[Bar, ...]
    current: Optional[Bar]


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, float(value)))


def _valid_history(history: pd.DataFrame) -> pd.DataFrame:
    frame = history.copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    for column in ("open", "high", "low", "close", "volume"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["date", "open", "high", "low", "close", "volume"])
    frame = frame[frame["volume"] >= 0]
    return frame.sort_values("date").reset_index(drop=True)


def _period_key(timestamp: pd.Timestamp, frequency: str):
    if frequency == "W":
        days_to_friday = (4 - timestamp.weekday()) % 7
        return (timestamp + pd.Timedelta(days=days_to_friday)).date()
    if frequency == "M":
        return (timestamp.replace(day=1) + pd.offsets.MonthEnd(1)).date()
    if frequency == "Y":
        return timestamp.replace(month=12, day=31).date()
    raise ValueError("frequency must be W, M or Y")


def resample_ohlcv(
    history: pd.DataFrame,
    frequency: Literal["W", "M", "Y"],
    now: datetime,
) -> ResampledHistory:
    frame = _valid_history(history)
    if frame.empty:
        raise ValueError("history has no valid OHLCV rows")
    frame["period_key"] = frame["date"].map(lambda value: _period_key(value, frequency))
    bars = []
    for period_key, group in frame.groupby("period_key", sort=True):
        bars.append(Bar(
            date=str(group["date"].iloc[-1].date()),
            open=float(group["open"].iloc[0]),
            high=float(group["high"].max()),
            low=float(group["low"].min()),
            close=float(group["close"].iloc[-1]),
            volume=float(group["volume"].sum()),
            completed=period_key < now.date(),
        ))
    completed = tuple(bar for bar in bars if bar.completed)
    current = next((bar for bar in reversed(bars) if not bar.completed), None)
    return ResampledHistory(frequency=frequency, completed=completed, current=current)


def calculate_ma250(history: pd.DataFrame) -> Optional[float]:
    frame = _valid_history(history)
    if len(frame) < 250:
        return None
    return float(frame["close"].tail(250).mean())


def score_period(bars, price, ma250) -> float:
    if len(bars) < 3:
        raise ValueError("period history requires at least 3 completed bars")
    closes = [bar.close for bar in bars]
    highs = [bar.high for bar in bars]
    lows = [bar.low for bar in bars]
    volumes = [bar.volume for bar in bars]
    ma20 = mean(closes[-20:])
    ma20_prev = mean(closes[max(0, len(closes) - 21):-1])
    ma50 = mean(closes[-50:])
    high20 = max(highs[-20:])
    low20 = min(lows[-20:])
    location = (price - low20) / (high20 - low20) if high20 > low20 else 0.5

    trend = 0.0
    trend += 15 if price > ma20 else 0
    trend += 10 if ma20 > ma20_prev else 0
    trend += 5 if price > ma50 else 0
    trend += 5 if ma20 > ma50 else 0
    if ma250 is not None:
        trend += 5 if price > ma250 else 0

    if 0.35 <= location <= 0.75:
        position = 25.0
    elif 0.20 <= location <= 0.85:
        position = 18.0
    else:
        position = 8.0

    momentum = 0.0
    momentum += 8 if closes[-1] > closes[max(0, len(closes) - 4)] else 0
    momentum += 7 if closes[-1] > closes[max(0, len(closes) - 7)] else 0
    momentum += 5 if bars[-1].close > bars[-1].open else 0

    average_volume = mean(volumes[max(0, len(volumes) - 21):-1])
    volume_ratio = volumes[-1] / average_volume if average_volume > 0 else 0
    positive_periods = sum(
        1 for index in range(1, len(closes)) if closes[index] > closes[index - 1]
    )
    obv = sum(
        volumes[index] if closes[index] > closes[index - 1] else
        -volumes[index] if closes[index] < closes[index - 1] else 0
        for index in range(1, len(closes))
    )
    volume_score = 0.0
    volume_score += 8 if 0.8 <= volume_ratio <= 2.0 and closes[-1] > closes[-2] else 0
    volume_score += 4 if obv > 0 else 0
    volume_score += 3 if positive_periods > len(closes) / 2 and volume_ratio >= 1 else 0
    return clamp(trend + position + momentum + volume_score)


def score_technical(technical, yearly_score, monthly_score, weekly_score) -> float:
    base = 50.0 + float(technical.get("score") or 0) * 8.0
    if technical.get("entry_can_buy"):
        base += 15.0
    if technical.get("trend_weak"):
        base -= 30.0
    daily = clamp(base)
    return clamp(
        float(yearly_score) * 0.35
        + float(monthly_score) * 0.30
        + float(weekly_score) * 0.20
        + daily * 0.15
    )


def fundamental_blockers(
    snapshot: Optional[FundamentalSnapshot],
    current_price: float,
) -> Tuple[str, ...]:
    if snapshot is None:
        return ("基本面快照缺失",)
    blockers = []
    try:
        datetime.fromisoformat(snapshot.analyzed_at)
    except (TypeError, ValueError):
        blockers.append("基本面分析日期无效")
    if snapshot.health_level == "ALERT":
        blockers.append("基本面等级为 ALERT")
    trends = tuple(snapshot.quarter_trends or ())
    if len(trends) >= 2 and all(
        revenue_yoy <= -20 and profit_yoy <= -20
        for revenue_yoy, profit_yoy in trends[-2:]
    ):
        blockers.append("最近两季营收、净利润同比均下降超过 20%")
    if snapshot.score < 55:
        blockers.append("基本面综合分低于 55")
    risk_text = "；".join(snapshot.risks)
    for keyword in ("立案", "重大违规", "重大减值", "持续减持"):
        if keyword in risk_text:
            blockers.append(f"存在治理风险：{keyword}")
    fair_high = snapshot.fair_high
    latest_growth = bool(
        snapshot.revenue_yoy is not None
        and snapshot.profit_yoy is not None
        and snapshot.revenue_yoy > 0
        and snapshot.profit_yoy > 0
    )
    if (
        fair_high is not None
        and math.isfinite(float(fair_high))
        and fair_high > 0
        and current_price > fair_high * 1.20
        and not latest_growth
    ):
        blockers.append("价格高于合理区间上沿 20% 且缺少最新营收、净利同步增长")
    if snapshot.fair_low is None or snapshot.fair_high is None:
        blockers.append("基本面合理估值区间缺失")
    return tuple(blockers)


def score_fundamental(
    snapshot: Optional[FundamentalSnapshot],
    current_price: float,
) -> float:
    if snapshot is None:
        return 0.0
    financial = 35.0 * clamp(snapshot.score) / 100.0
    ocf_score = clamp((snapshot.ocf_to_profit or 0) * 75)
    margin_score = clamp((snapshot.net_margin or 0) * 5)
    roe_score = clamp((snapshot.roe or 0) * 6)
    quality = (ocf_score + margin_score + roe_score) / 3.0 * 0.25

    fair_low = snapshot.fair_low
    fair_high = snapshot.fair_high
    valuation_base = 0.0
    if (
        fair_low is not None and fair_high is not None
        and fair_high > fair_low > 0 and current_price > 0
    ):
        if current_price <= fair_high:
            valuation_base = 25.0
        elif current_price <= fair_high * 1.20:
            excess_ratio = (current_price - fair_high) / fair_high
            valuation_base = 25.0 * max(0.0, 1.0 - excess_ratio / 0.20)
    percentiles = [
        value for value in (snapshot.pe_percentile, snapshot.pb_percentile)
        if value is not None and math.isfinite(float(value))
    ]
    percentile_factor = 0.5 + (100.0 - mean(percentiles)) / 200.0 if percentiles else 0.5
    valuation = valuation_base * clamp(percentile_factor, 0.0, 1.0)

    governance = 15.0
    for risk in snapshot.risks:
        risk_text = str(risk)
        if any(keyword in risk_text for keyword in ("立案", "重大违规", "重大减值", "持续减持")):
            governance -= 15
        else:
            governance -= 5
    governance = clamp(governance, 0.0, 15.0)
    return clamp(financial + quality + valuation + governance)


def trusted_stop_price(technical, price) -> float:
    values = [
        float(technical.get("entry_stop_price") or 0),
        float(technical.get("weekly_stop_price") or 0),
    ]
    values = [
        value for value in values
        if math.isfinite(value) and 0 < value < float(price)
    ]
    return max(values) if values else 0.0


def suggest_add_quantity(
    *,
    price: float,
    stop_price: float,
    total_capital: float,
    available_capital: float,
    current_value: float,
    max_position_pct: float,
) -> int:
    values = (price, stop_price, total_capital, current_value, max_position_pct)
    if not all(math.isfinite(float(value)) for value in values):
        return 0
    if price <= 0 or stop_price <= 0 or stop_price >= price:
        return 0
    if total_capital <= 0 or current_value < 0 or max_position_pct <= 0:
        return 0
    distance = max((price - stop_price) / price, 0.01)
    if distance > 0.10:
        return 0
    risk_shares = (total_capital * 0.005) / (price * distance)
    max_value = total_capital * max_position_pct / 100.0
    cap_shares = max(0.0, max_value - current_value) / price
    shares = min(risk_shares, cap_shares)
    if math.isfinite(float(available_capital)) and available_capital > 0:
        shares = min(shares, available_capital / price)
    return int(shares // LOT) * LOT


def rank_addition_candidates(candidates) -> Optional[AdditionRecommendation]:
    eligible = [
        item for item in candidates
        if item.selected
        and item.composite_score >= 70
        and item.technical_score >= 65
        and item.shares > 0
    ]
    if not eligible:
        return None
    return sorted(
        eligible,
        key=lambda item: (
            -item.composite_score,
            -item.fundamental_score,
            -item.yearly_score,
            -item.monthly_score,
            -item.weekly_score,
            item.position_pct_before,
            item.code,
        ),
    )[0]


def _fresh_fundamental(snapshot, now) -> Tuple[bool, str]:
    if snapshot is None:
        return False, "基本面快照缺失"
    try:
        analyzed_at = datetime.fromisoformat(snapshot.analyzed_at)
    except (TypeError, ValueError):
        return False, "基本面分析日期无效"
    if analyzed_at.tzinfo is not None:
        current = now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)
        analyzed_at = analyzed_at.astimezone(timezone.utc)
    else:
        current = now.replace(tzinfo=None) if now.tzinfo else now
    age_days = (current - analyzed_at).total_seconds() / 86400.0
    if age_days < -1:
        return False, "基本面分析日期在未来"
    if age_days > 30:
        return False, "基本面快照超过30天"
    return True, f"基本面分析于{snapshot.analyzed_at[:10]}"


def _range_position_60(frame: pd.DataFrame, price: float) -> Optional[float]:
    sample = frame.tail(60)
    if len(sample) < 60:
        return None
    low = float(sample["low"].min())
    high = float(sample["high"].max())
    if not math.isfinite(low) or not math.isfinite(high) or high <= low:
        return None
    return (price - low) / (high - low)


def assess_holding(item: HoldingAssessmentInput, now: datetime) -> AdditionRecommendation:
    missing = list(item.missing)
    reasons = []
    try:
        trend_info = item.technical.get("trend_assessment") or {}
        major_trend = str(trend_info.get("major_trend") or "")
        high_info = item.technical.get("high_position") or {}
        high_state = str(high_info.get("state") or "").upper()
        permissions = item.technical.get("trade_permissions") or {}
        major_down = major_trend in {"DOWN", "大趋势向下"}
        bottoming = major_trend in {"BOTTOMING", "筑底阶段"}
        history = _valid_history(item.daily_history)
        history = history[history["date"] <= pd.Timestamp(now)]
        if len(history) < 250:
            missing.append("有效日线不足250根")
        ma250 = calculate_ma250(history)
        if ma250 is None:
            missing.append("MA250年线不可计算")

        resampled = {
            frequency: resample_ohlcv(history, frequency, now)
            for frequency in ("Y", "M", "W")
        }
        minimums = {"Y": 3, "M": 12, "W": 60}
        for frequency, minimum in minimums.items():
            if len(resampled[frequency].completed) < minimum:
                missing.append(f"完整{frequency}周期不足{minimum}根")

        if missing:
            yearly_score = monthly_score = weekly_score = 0.0
            technical_score = 0.0
        else:
            yearly_score = score_period(
                resampled["Y"].completed, item.price, ma250
            )
            monthly_score = score_period(
                resampled["M"].completed, item.price, ma250
            )
            weekly_score = score_period(
                resampled["W"].completed, item.price, ma250
            )
            technical_score = score_technical(
                item.technical, yearly_score, monthly_score, weekly_score
            )

        fresh_fundamental, freshness_text = _fresh_fundamental(item.fundamental, now)
        if fresh_fundamental:
            reasons.append(freshness_text)
            fundamental_score = score_fundamental(item.fundamental, item.price)
            blockers = list(fundamental_blockers(item.fundamental, item.price))
        else:
            fundamental_score = 0.0
            blockers = [freshness_text]

        stop_price = trusted_stop_price(item.technical, item.price)
        if stop_price <= 0:
            blockers.append("可信止损位缺失")
        elif (item.price - stop_price) / item.price > 0.10:
            blockers.append("止损距离超过10%")

        range_position = _range_position_60(history, item.price)
        if range_position is not None and range_position >= 0.85:
            blockers.append("近60日区间位置达到85%以上")
        elif range_position is not None:
            reasons.append(f"近60日区间位置{range_position * 100:.0f}%")

        limit_up_price = item.technical.get("limit_up_price")
        if (
            limit_up_price is not None
            and math.isfinite(float(limit_up_price))
            and item.price >= float(limit_up_price) * 0.995
        ):
            blockers.append("现价处于涨停价附近")

        if not item.technical.get("entry_can_buy"):
            blockers.append("日线没有可执行入场结构")
        if item.technical.get("trend_weak"):
            blockers.append("日线趋势转弱")
        if not missing and yearly_score < 45:
            blockers.append("年K趋势偏弱")
        if not missing and monthly_score < 45:
            blockers.append("月K趋势偏弱")

        composite_score = clamp(
            technical_score * 0.60 + fundamental_score * 0.40
        )
        current_value = item.quantity * item.price
        shares = suggest_add_quantity(
            price=item.price,
            stop_price=stop_price,
            total_capital=item.total_capital,
            available_capital=item.available_capital,
            current_value=current_value,
            max_position_pct=item.max_position_pct,
        )
        if major_down:
            blockers.append("大趋势向下，禁止新增资金")
            shares = 0
        elif bottoming:
            high_safe = high_state in {"", "NOT_HIGH", "非高位", "SECOND_WAVE_CONFIRMED", "二波确认"}
            probe_allowed = bool(permissions.get("allow_probe", False)) and high_safe
            if not probe_allowed:
                blockers.append("筑底阶段只允许观察，暂不安排轻仓试错")
                shares = 0
            else:
                cap_amount = float(permissions.get("probe_cap_amount") or 0.0)
                if not math.isfinite(cap_amount) or cap_amount <= 0:
                    cap_amount = (
                        item.total_capital
                        * max(0.0, float(item.initial_entry_pct or 0.0))
                        / 100.0
                        * 0.25
                    )
                cap_shares = int(cap_amount // item.price // LOT) * LOT if item.price > 0 else 0
                available_shares = (
                    int(item.available_capital // item.price // LOT) * LOT
                    if item.price > 0 and item.available_capital > 0 else 0
                )
                shares = min(shares, cap_shares, available_shares)
                reasons.append("筑底阶段仅安排不超过初始仓位金额25%的轻仓试错")
        else:
            high_blocked = high_state in {
                "MISSING",
                "数据不足",
                "HIGH_PENDING",
                "高位待定",
                "DISTRIBUTION_RISK",
                "高位派发风险",
                "SECOND_WAVE_CANDIDATE",
                "二波候选",
            }
            if high_blocked:
                blockers.append(f"高位结构不允许新增资金：{high_state or '数据不足'}")
                shares = 0
            if not bool(permissions.get("allow_add", True)):
                blockers.append("统一交易门控不允许新增资金")
                shares = 0
        if composite_score < 70:
            blockers.append("综合分低于70")
        if technical_score < 65:
            blockers.append("技术分低于65")
        if shares < LOT:
            blockers.append("参考数量不足一手")

        selected = not missing and not blockers and not bottoming and not major_down
        if selected:
            reasons.extend((
                f"综合分{composite_score:.1f}",
                f"技术分{technical_score:.1f}",
                f"基本面分{fundamental_score:.1f}",
                f"年K{yearly_score:.1f}、月K{monthly_score:.1f}、周K{weekly_score:.1f}",
            ))
        else:
            reasons.extend(blockers)

        position_before = (
            current_value / item.total_capital * 100
            if item.total_capital > 0 else 0.0
        )
        position_after = (
            (current_value + shares * item.price) / item.total_capital * 100
            if item.total_capital > 0 else 0.0
        )
        return AdditionRecommendation(
            code=item.code,
            name=item.name,
            selected=selected,
            composite_score=composite_score,
            technical_score=technical_score,
            fundamental_score=fundamental_score,
            yearly_score=yearly_score,
            monthly_score=monthly_score,
            weekly_score=weekly_score,
            shares=shares if selected or bottoming else 0,
            amount=shares * item.price if selected or bottoming else 0.0,
            stop_price=stop_price,
            position_pct_before=position_before,
            position_pct_after=position_after,
            reasons=tuple(reasons),
            missing=tuple(missing),
        )
    except (TypeError, ValueError, KeyError, AttributeError) as error:
        return AdditionRecommendation(
            code=item.code,
            name=item.name,
            selected=False,
            composite_score=0.0,
            technical_score=0.0,
            fundamental_score=0.0,
            yearly_score=0.0,
            monthly_score=0.0,
            weekly_score=0.0,
            shares=0,
            amount=0.0,
            stop_price=0.0,
            position_pct_before=0.0,
            position_pct_after=0.0,
            reasons=(f"评估失败：{error}",),
            missing=(str(error),),
        )


def select_addition_candidate(
    inputs,
    now: datetime,
) -> Optional[AdditionRecommendation]:
    candidates = [assess_holding(item, now) for item in inputs]
    return rank_addition_candidates(candidates)
