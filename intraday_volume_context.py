#!/usr/bin/env python3
"""盘中量价上下文：成交量只调节确认和动作力度。"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import median
from typing import Iterable, Mapping, Optional


@dataclass(frozen=True)
class IntradayVolumeContext:
    market_turnover_ratio: Optional[float] = None
    market_expanding: Optional[bool] = None
    stock_volume_ratio: Optional[float] = None
    stock_volume_confirming: Optional[bool] = None
    bar_volume_ratio: Optional[float] = None
    bar_volume_confirming: Optional[bool] = None
    absorption_confirmed: bool = False
    blowoff_risk: bool = False


def _float(value) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and number > 0 else None


def _ratio(value, baseline) -> Optional[float]:
    current = _float(value)
    base = _float(baseline)
    if current is None or base is None:
        return None
    return current / base


def build_intraday_volume_context(
    *,
    market_turnover=None,
    recent_market_turnovers: Iterable[float] = (),
    stock_volume_ratio=None,
    price_change_pct=None,
    bars15: Iterable[Mapping] = (),
    price=None,
    support=None,
) -> IntradayVolumeContext:
    values = [value for item in recent_market_turnovers if (value := _float(item)) is not None]
    market_baseline = median(values) if values else None
    market_ratio = _ratio(market_turnover, market_baseline)
    market_expanding = market_ratio is not None and market_ratio >= 1.2
    stock_ratio = _float(stock_volume_ratio)
    stock_confirming = stock_ratio is not None and stock_ratio >= 1.3

    bars = list(bars15 or [])
    bar_ratio = None
    bar_confirming = None
    absorption = False
    blowoff = False
    if len(bars) >= 2:
        last = bars[-1]
        previous = bars[-2]
        try:
            high = float(last["high"])
            low = float(last["low"])
            open_price = float(last["open"])
            close = float(last["close"])
            previous_close = float(previous["close"])
            previous_volumes = [
                float(item["volume"])
                for item in bars[-21:-1]
                if _float(item.get("volume")) is not None
            ]
            if previous_volumes:
                bar_ratio = float(last["volume"]) / median(previous_volumes)
                bar_confirming = bar_ratio >= 1.3
            else:
                bar_confirming = False
            bar_range = high - low
            midpoint = (high + low) / 2
            current_price = _float(price)
            support_value = _float(support)
            lower_shadow = min(open_price, close) - low
            upper_shadow = high - max(open_price, close)
            volume_hint = bool(bar_confirming)
            absorption = bool(
                support_value is not None
                and current_price is not None
                and current_price < support_value
                and close < support_value
                and bar_range > 0
                and lower_shadow / bar_range >= 0.35
                and close >= midpoint
                and close >= previous_close * 0.99
                and volume_hint
            )
            change_value = _float(price_change_pct)
            blowoff = bool(
                change_value is not None
                and change_value > 0
                and stock_ratio is not None
                and bool(bar_confirming)
                and bar_range > 0
                and upper_shadow / bar_range >= 0.5
                and close < midpoint
            )
        except (KeyError, TypeError, ValueError):
            absorption = False
            blowoff = False

    return IntradayVolumeContext(
        market_turnover_ratio=market_ratio,
        market_expanding=market_expanding if market_ratio is not None else None,
        stock_volume_ratio=stock_ratio,
        stock_volume_confirming=stock_confirming if stock_ratio is not None else None,
        bar_volume_ratio=bar_ratio,
        bar_volume_confirming=bar_confirming,
        absorption_confirmed=absorption,
        blowoff_risk=blowoff,
    )
