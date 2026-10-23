#!/usr/bin/env python3
"""按交易日缓存的盘中风险日级上下文。"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional, Tuple

from deep_loss_policy import is_deep_loss


def _value(source: Any, key: str, default=None):
    if isinstance(source, Mapping):
        return source.get(key, default)
    return getattr(source, key, default)


def _is_deep_loss(result: Mapping[str, Any]) -> bool:
    try:
        cost = float(result.get("cost") or 0)
        price = float(result.get("price") or 0)
        atr = float(result.get("atr") or 0)
    except (TypeError, ValueError):
        return False
    if cost <= 0 or price <= 0 or atr <= 0:
        return False
    return is_deep_loss(cost, price, atr / price * 100)


def _high_exit_risk(result: Mapping[str, Any]) -> bool:
    entry = result.get("entry_structure") or {}
    price_position = result.get("price_position") or {}
    price_location = str(price_position.get("location_key") or "").upper()
    trend_weak = bool(result.get("trend_weak") or entry.get("trend_weak"))
    high_position = entry.get("high_position") or {}
    high_state = str(_value(high_position, "state", "") or "").upper()
    distribution = high_state in {"高位派发风险", "DISTRIBUTION_RISK"}
    if price_location != "HIGH":
        return False
    return distribution or (trend_weak and not _is_deep_loss(result))


def build_daily_risk_context(
    result: Mapping[str, Any],
    overview: Optional[Mapping[str, Any]] = None,
    quote_date: Optional[str] = None,
) -> Dict[str, Any]:
    """从日线结果和组合日报告提取盘中策略所需的慢变量。

    这里不读取行情、不触发网络请求；调用方按交易日缓存，盘中只读快照。
    """
    result = result or {}
    entry = result.get("entry_structure") or {}
    price_position = result.get("price_position") or {}
    trend = entry.get("trend_assessment") or {}
    trend_evidence = trend.get("evidence") or {}
    attribution = ((overview or {}).get("attribution") or {})
    trend_overview = ((overview or {}).get("trend") or {})
    systemic_risk = bool(
        attribution.get("all_down") or attribution.get("mostly_down")
    )
    holdings = int((overview or {}).get("n_holdings") or 0)
    risk_break = int(trend_overview.get("risk_break") or 0)
    trend_weak = int(trend_overview.get("trend_weak") or 0)
    portfolio_risk = bool(
        holdings >= 3
        and (
            risk_break >= (holdings + 1) // 2
            or trend_weak >= (holdings + 1) // 2
        )
    )
    fundamental_level = str(result.get("fundamental_level") or "").upper()
    position_share_pct = result.get("position_share_pct")
    try:
        position_share_value = float(position_share_pct)
    except (TypeError, ValueError):
        position_share_value = None
    normalized_date = str(quote_date or result.get("trade_date") or "")[:10]
    return {
        "source_date": normalized_date,
        "price_location": price_position.get("location_key"),
        "range_low": price_position.get("range_low"),
        "invalidation_price": entry.get("stop_price"),
        "major_trend": trend.get("major_trend"),
        "five_day_no_new_low": trend_evidence.get("five_day_no_new_low"),
        "closes_below_ma20": int(entry.get("closes_below_ma20") or 0),
        "high_exit_risk": _high_exit_risk(result),
        "systemic_risk": systemic_risk,
        "portfolio_risk": portfolio_risk,
        "event_risk": fundamental_level == "ALERT",
        "position_risk": bool(
            position_share_value is not None and position_share_value > 10.0
        ),
        "liquidity_risk": result.get("liquidity_risk"),
    }


class DailyRiskContextCache:
    """同一交易日只构建一次日级上下文。"""

    def __init__(self):
        self._values: Dict[Tuple[str, str], Dict[str, Any]] = {}

    def get(
        self,
        code: str,
        quote_date: Optional[str],
        result: Mapping[str, Any],
        overview: Optional[Mapping[str, Any]],
    ) -> Dict[str, Any]:
        normalized_date = str(quote_date or "")[:10]
        key = (str(code or "").lower().strip(), normalized_date)
        if not normalized_date or not overview:
            return build_daily_risk_context(result, overview, normalized_date)
        if key not in self._values:
            self._values[key] = build_daily_risk_context(
                result, overview, normalized_date,
            )
        return dict(self._values[key])
