#!/usr/bin/env python3
"""新浪盘中K线获取与15/30分钟确认证据。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Callable, Iterable, List, Mapping, Optional


@dataclass(frozen=True)
class TimeframeEvidence:
    m15_confirmations: int
    m30_aligned: bool
    risk_confirmed: bool
    complete: bool
    risk_complete: bool
    last_completed_15m: Optional[str]
    last_completed_30m: Optional[str]
    m15_distance_pct: float = 0.0
    m30_distance_pct: float = 0.0
    last_bar_rejected: bool = False
    clearance_confirmed: bool = False
    risk_confirmations: int = 0
    risk_threshold_pct: float = 0.005
    risk_breach_pct: float = 0.0
    risk_level: str = "NORMAL"


def _session_slots(quote_date: str, scale: int) -> List[datetime]:
    parsed_date = datetime.strptime(quote_date[:10], "%Y-%m-%d").date()
    slots = []
    for start_time, end_time in (
        (time(9, 30), time(11, 30)),
        (time(13, 0), time(15, 0)),
    ):
        cursor = datetime.combine(parsed_date, start_time) + timedelta(minutes=scale)
        session_end = datetime.combine(parsed_date, end_time)
        while cursor <= session_end:
            slots.append(cursor)
            cursor += timedelta(minutes=scale)
    return slots


def latest_completed_slot(
    now: datetime,
    scale: int,
    quote_date: Optional[str],
) -> Optional[str]:
    if scale not in {5, 15, 30, 60}:
        raise ValueError("不支持的K线周期")
    if not quote_date:
        return None
    try:
        parsed_quote_date = datetime.strptime(str(quote_date)[:10], "%Y-%m-%d").date()
    except (TypeError, ValueError) as error:
        raise ValueError("行情交易日期必须是YYYY-MM-DD") from error
    if now.date() != parsed_quote_date:
        return None
    completed = [slot for slot in _session_slots(quote_date, scale) if slot <= now]
    return completed[-1].strftime("%Y-%m-%d %H:%M:%S") if completed else None


def parse_intraday_rows(
    rows: Iterable[dict],
    scale: int,
    now: datetime,
    timestamp_semantics: str = "end",
) -> List[dict]:
    if scale not in {5, 15, 30, 60}:
        raise ValueError("不支持的K线周期")
    if timestamp_semantics not in {"start", "end"}:
        raise ValueError("K线时间语义必须是start或end")
    parsed = []
    seen = set()
    for row in rows or []:
        try:
            raw_timestamp = datetime.strptime(str(row["day"]), "%Y-%m-%d %H:%M:%S")
            values = {
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row["volume"]),
            }
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("盘中K线字段无效") from error
        if any(value != value for value in values.values()):
            raise ValueError("盘中K线包含无效数字")
        if values["volume"] < 0:
            raise ValueError("盘中K线成交量不能为负")
        if values["low"] > min(values["open"], values["close"], values["high"]):
            raise ValueError("盘中K线价格区间无效")
        if values["high"] < max(values["open"], values["close"], values["low"]):
            raise ValueError("盘中K线价格区间无效")
        timestamp = (
            raw_timestamp
            if timestamp_semantics == "end"
            else raw_timestamp + timedelta(minutes=scale)
        )
        if timestamp.minute % scale or timestamp.second:
            raise ValueError("盘中K线时间戳未对齐周期")
        if timestamp > now:
            continue
        key = timestamp.strftime("%Y-%m-%d %H:%M:%S")
        if key in seen:
            raise ValueError("盘中K线时间戳重复")
        seen.add(key)
        parsed.append(dict(values, timestamp=key))
    parsed.sort(key=lambda item: item["timestamp"])
    return parsed


def fetch_intraday_bars(
    code: str,
    scale: int,
    datalen: int = 120,
    now: Optional[datetime] = None,
    fetcher: Optional[Callable] = None,
) -> List[dict]:
    now = now or datetime.now()
    if fetcher is None:
        import requests

        fetcher = requests.get
    fetch = fetcher
    response = fetch(
        "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData",
        params={"symbol": code, "scale": scale, "ma": "no", "datalen": datalen},
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://finance.sina.com.cn/",
        },
        timeout=10,
    )
    response.raise_for_status()
    rows = response.json()
    if not isinstance(rows, list):
        raise ValueError("新浪盘中K线响应无效")
    return parse_intraday_rows(rows, scale=scale, now=now)


def _trailing_confirmations(bars: List[dict], threshold: float, breakout: bool) -> int:
    count = 0
    for bar in reversed(bars):
        confirmed = bar["close"] >= threshold if not breakout else bar["close"] > threshold
        if not confirmed:
            break
        count += 1
    return count


def build_timeframe_evidence(
    bars15: Iterable[dict],
    bars30: Iterable[dict],
    price: float,
    support: float,
    resistance: float,
    now: datetime,
    quote_date: Optional[str] = None,
    historical_context: Optional[Mapping] = None,
) -> TimeframeEvidence:
    bars15 = list(bars15 or [])
    bars30 = list(bars30 or [])
    quote_day = str(quote_date or "")[:10]
    current_day_bars15 = [
        bar for bar in bars15
        if quote_day and str(bar.get("timestamp", ""))[:10] == quote_day
    ]
    breakout = resistance > 0 and price >= resistance
    threshold = resistance if breakout else support
    confirmations = (
        _trailing_confirmations(current_day_bars15, threshold, breakout)
        if threshold > 0
        else 0
    )
    if len(bars30) >= 2 and support > 0:
        previous_close = bars30[-2]["close"]
        last_close = bars30[-1]["close"]
        m30_aligned = last_close >= previous_close and last_close >= support
    else:
        m30_aligned = False
    latest15 = latest_completed_slot(now, 15, quote_date)
    latest30 = latest_completed_slot(now, 30, quote_date)
    m15_fresh = bool(
        bars15
        and bars15[-1]["timestamp"] == latest15
        and bars15[-1]["timestamp"][:10] == str(quote_date)[:10]
    )
    m30_fresh = bool(
        bars30
        and bars30[-1]["timestamp"] == latest30
        and bars30[-1]["timestamp"][:10] == str(quote_date)[:10]
    )
    risk_threshold_pct = _risk_threshold_pct(bars15, support)
    risk_breach_pct = max(0.0, (support - price) / support) if support > 0 else 0.0
    risk_price_threshold = support * (1 - risk_threshold_pct)
    risk_confirmed = bool(
        support > 0
        and bars15
        and price < risk_price_threshold
        and bars15[-1]["close"] < risk_price_threshold
    )
    risk_confirmations = _risk_confirmations(
        current_day_bars15, price, risk_price_threshold
    ) if risk_confirmed else 0
    risk_level = _historical_risk_level(
        price, historical_context or {}
    )
    if threshold > 0 and confirmations:
        confirmed_bars = bars15[-min(confirmations, 2):]
        m15_distance_pct = min(
            (item["close"] - threshold) / threshold for item in confirmed_bars
        )
    else:
        m15_distance_pct = 0.0
    if threshold > 0 and bars30:
        m30_distance_pct = (bars30[-1]["close"] - threshold) / threshold
    else:
        m30_distance_pct = 0.0
    last_bar_rejected = False
    if bars15:
        last_bar = bars15[-1]
        bar_range = last_bar["high"] - last_bar["low"]
        upper_shadow = last_bar["high"] - max(last_bar["open"], last_bar["close"])
        midpoint = (last_bar["high"] + last_bar["low"]) / 2
        last_bar_rejected = bool(
            bar_range > 0
            and upper_shadow / bar_range >= 0.5
            and last_bar["close"] < midpoint
        )
    clearance_confirmed = bool(
        confirmations >= 2
        and m30_aligned
        and m15_distance_pct >= 0.003
        and m30_distance_pct >= -0.003
    )
    return TimeframeEvidence(
        m15_confirmations=confirmations,
        m30_aligned=m30_aligned,
        risk_confirmed=risk_confirmed,
        complete=(
            len(bars15) >= 2
            and len(bars30) >= 2
            and m15_fresh
            and m30_fresh
        ),
        risk_complete=bool(support > 0 and bars15 and m15_fresh),
        last_completed_15m=bars15[-1]["timestamp"] if bars15 else None,
        last_completed_30m=bars30[-1]["timestamp"] if bars30 else None,
        m15_distance_pct=m15_distance_pct,
        m30_distance_pct=m30_distance_pct,
        last_bar_rejected=last_bar_rejected,
        clearance_confirmed=clearance_confirmed,
        risk_confirmations=risk_confirmations,
        risk_threshold_pct=risk_threshold_pct,
        risk_breach_pct=risk_breach_pct,
        risk_level=risk_level,
    )


def _risk_threshold_pct(bars15: List[dict], support: float) -> float:
    if support <= 0 or not bars15:
        return 0.005
    true_ranges = []
    previous_close = None
    for bar in bars15[-14:]:
        high = float(bar["high"])
        low = float(bar["low"])
        close = float(bar["close"])
        candidates = [high - low]
        if previous_close is not None:
            candidates.extend((abs(high - previous_close), abs(low - previous_close)))
        true_ranges.append(max(candidates))
        previous_close = close
    atr = sum(true_ranges) / len(true_ranges)
    atr_pct = atr / support
    return min(0.015, max(0.005, atr_pct * 0.5))


def _risk_confirmations(
    bars15: List[dict], price: float, risk_price_threshold: float
) -> int:
    if not bars15 or price >= risk_price_threshold:
        return 0
    count = 0
    for bar in reversed(bars15):
        if float(bar["close"]) >= risk_price_threshold:
            break
        count += 1
        if count >= 3:
            break
    return count


def _historical_risk_level(price: float, context: Mapping) -> str:
    if any(bool(context.get(key)) for key in (
        "systemic_risk",
        "portfolio_risk",
        "event_risk",
        "high_exit_risk",
    )):
        return "CRITICAL"
    invalidation = float(context.get("invalidation_price") or 0)
    range_low = float(context.get("range_low") or 0)
    major_trend = str(context.get("major_trend") or "").upper()
    no_new_low = context.get("five_day_no_new_low")
    if invalidation > 0 and price <= invalidation:
        return "CRITICAL"
    if range_low > 0 and price < range_low:
        return "CRITICAL"
    if major_trend == "DOWN" and no_new_low is False:
        return "CRITICAL"
    closes_below_ma20 = int(context.get("closes_below_ma20") or 0)
    if (
        str(context.get("price_location") or "").upper() == "LOW"
        or major_trend == "DOWN"
        or closes_below_ma20 >= 5
    ):
        return "ELEVATED"
    return "NORMAL"
