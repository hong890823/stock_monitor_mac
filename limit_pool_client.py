#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

from datetime import datetime
import json
import math
import re
from typing import Any, Callable, Mapping, Optional
from urllib.parse import urlencode

import requests

from limit_up_policy import EASTMONEY_LIMIT_POOL, rank_limit_down, rank_limit_up


UP_POOL_URL = "https://push2ex.eastmoney.com/getTopicZTPool"
DOWN_POOL_URL = "https://push2ex.eastmoney.com/getTopicDTPool"


def _number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _scaled(value: Any, divisor: float) -> Optional[float]:
    number = _number(value)
    return None if number is None else number / divisor


def _percent(numerator: Optional[float], denominator: Optional[float]) -> Optional[float]:
    if numerator is None or denominator is None or denominator <= 0:
        return None
    return numerator / denominator * 100


def _time_text(value: Any) -> Optional[str]:
    number = _number(value)
    if number is None:
        return None
    seconds = int(number)
    hour, minute, second = seconds // 10000, seconds // 100 % 100, seconds % 100
    if not 0 <= hour <= 23 or not 0 <= minute <= 59 or not 0 <= second <= 59:
        return None
    return f"{hour:02d}:{minute:02d}:{second:02d}"


def _quote_code(code: Any) -> str:
    digits = re.sub(r"\D", "", str(code or ""))
    digits = digits[-6:]
    if digits.startswith("6"):
        return f"sh{digits}"
    if digits.startswith(("0", "3")):
        return f"sz{digits}"
    return f"bj{digits}"


def _payload(text: Any) -> Mapping[str, Any]:
    if hasattr(text, "text"):
        text = text.text
    raw = str(text or "").strip()
    if not raw:
        raise ValueError("涨跌停接口返回空响应")
    if raw.startswith("{"):
        payload = json.loads(raw)
    else:
        start, end = raw.find("("), raw.rfind(")")
        if start < 0 or end <= start:
            raise ValueError("涨跌停接口返回格式无效")
        payload = json.loads(raw[start + 1:end])
    if not isinstance(payload, Mapping) or payload.get("rc") != 0:
        raise ValueError("涨跌停接口返回异常")
    return payload


def _rows(text: Any) -> list[Mapping[str, Any]]:
    data = _payload(text).get("data") or {}
    pool = data.get("pool") if isinstance(data, Mapping) else None
    return [row for row in pool or [] if isinstance(row, Mapping)]


def _normalize(row: Mapping[str, Any], *, down: bool) -> dict:
    fund_yi = _scaled(row.get("fund"), 1e8)
    amount_yi = _scaled(row.get("amount"), 1e8)
    float_yi = _scaled(row.get("ltsz"), 1e8)
    normalized = {
        "code": _quote_code(row.get("c")),
        "name": str(row.get("n") or row.get("c") or "").strip(),
        "price": _scaled(row.get("p"), 1000),
        "change_pct": _number(row.get("zdp")),
        "seal_amount_yi": fund_yi,
        "turnover_yi": amount_yi,
        "float_market_value_yi": float_yi,
        "seal_to_turnover_pct": _percent(fund_yi, amount_yi),
        "seal_to_float_pct": _percent(fund_yi, float_yi),
        "open_count": int(_number(row.get("oc" if down else "zbc")) or 0),
        "first_seal_time": _time_text(row.get("fbt")),
        "last_seal_time": _time_text(row.get("lbt")),
        "limit_streak": int(_number(row.get("lbc")) or (1 if not down else 0)),
        "industry": str(row.get("hybk") or "").strip() or None,
        "source": EASTMONEY_LIMIT_POOL,
    }
    return normalized


def parse_pools(up_json: Any, down_json: Any) -> dict:
    return {
        "up": rank_limit_up(_normalize(row, down=False) for row in _rows(up_json)),
        "down": rank_limit_down(_normalize(row, down=True) for row in _rows(down_json)),
        "source": EASTMONEY_LIMIT_POOL,
    }


def fetch_pools(
    now: datetime,
    *,
    fetcher: Optional[Callable[..., Any]] = None,
    timeout: float = 8.0,
) -> dict:
    get = fetcher or requests.get
    quote_date = now.strftime("%Y%m%d")
    params = {
        "cb": "callback",
        "ut": "7eea3edcaed734bea9cbfc24409ed989",
        "dpt": "wz.ztzt",
        "Pageindex": "0",
        "pagesize": "1000",
        "sort": "fbt:asc",
        "date": quote_date,
    }
    query = urlencode(params)
    up_response = get(f"{UP_POOL_URL}?{query}", timeout=timeout)
    down_response = get(f"{DOWN_POOL_URL}?{query}", timeout=timeout)
    result = parse_pools(up_response, down_response)
    result["quote_date"] = now.strftime("%Y-%m-%d")
    return result
