#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""盘中资金流数据的标准化、缓存和确认逻辑。

这里的主力资金是数据平台按成交单大小估算的分类，不代表真实机构账户。
模块不依赖 UI，网络提供方可以在测试或上层替换。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, Iterable, Mapping, Optional

import requests


_MISSING = {"", "-", "--", "—", "nan", "none", "null"}


def _clean_number(value: Any) -> Optional[float]:
    text = str(value or "").strip().replace(",", "")
    if text.lower() in _MISSING:
        return None
    text = text.replace("+", "", 1) if text.startswith("+") else text
    try:
        number = float(text)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def parse_amount_yi(value: Any) -> Optional[float]:
    """把资金金额统一为亿元，兼容亿元、万元和元。"""
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if text.lower() in _MISSING:
        return None
    multiplier = 1.0
    if text.endswith("亿"):
        text = text[:-1]
    elif text.endswith("万"):
        text = text[:-1]
        multiplier = 1 / 10000
    number = _clean_number(text)
    if number is None:
        return None
    if multiplier != 1.0:
        return number * multiplier
    return number / 1e8 if abs(number) > 10000 else number


def parse_ratio_pct(value: Any) -> Optional[float]:
    text = str(value or "").strip().replace(",", "")
    if text.endswith("%"):
        text = text[:-1]
    return _clean_number(text)


@dataclass(frozen=True)
class CapitalFlowSnapshot:
    key: str
    name: str
    kind: str
    quote_date: str
    as_of: datetime
    source: str
    scope: str
    session: str
    net_yi: Optional[float]
    net_ratio_pct: Optional[float] = None
    inflow_yi: Optional[float] = None
    outflow_yi: Optional[float] = None
    large_net_yi: Optional[float] = None
    super_large_net_yi: Optional[float] = None
    stale: bool = False


def is_fresh(
    snapshot: Optional[CapitalFlowSnapshot],
    now: Optional[datetime] = None,
    max_age_seconds: int = 150,
) -> bool:
    if snapshot is None or snapshot.stale or snapshot.net_yi is None:
        return False
    now = now or datetime.now()
    if snapshot.quote_date != now.strftime("%Y-%m-%d"):
        return False
    age = (now - snapshot.as_of).total_seconds()
    return 0 <= age <= max_age_seconds


def _iter_rows(frame: Any) -> Iterable[Mapping[str, Any]]:
    if frame is None:
        return []
    if hasattr(frame, "iterrows"):
        return (row.to_dict() for _, row in frame.iterrows())
    if isinstance(frame, Mapping):
        return [frame]
    return (row for row in frame if isinstance(row, Mapping))


def normalize_flow_frame(
    frame: Any,
    *,
    kind: str,
    key_column: str,
    name_column: str,
    net_column: str,
    quote_date: str,
    as_of: datetime,
    source: str,
    scope: str,
    session: str,
    ratio_column: Optional[str] = None,
    inflow_column: Optional[str] = None,
    outflow_column: Optional[str] = None,
    large_net_column: Optional[str] = None,
    super_large_net_column: Optional[str] = None,
) -> Dict[str, CapitalFlowSnapshot]:
    result = {}
    for row in _iter_rows(frame):
        key = str(row.get(key_column) or "").strip()
        if not key:
            continue
        snapshot = CapitalFlowSnapshot(
            key=key,
            name=str(row.get(name_column) or key).strip(),
            kind=kind,
            quote_date=quote_date,
            as_of=as_of,
            source=source,
            scope=scope,
            session=session,
            net_yi=parse_amount_yi(row.get(net_column)),
            net_ratio_pct=parse_ratio_pct(row.get(ratio_column)) if ratio_column else None,
            inflow_yi=parse_amount_yi(row.get(inflow_column)) if inflow_column else None,
            outflow_yi=parse_amount_yi(row.get(outflow_column)) if outflow_column else None,
            large_net_yi=parse_amount_yi(row.get(large_net_column)) if large_net_column else None,
            super_large_net_yi=(
                parse_amount_yi(row.get(super_large_net_column))
                if super_large_net_column else None
            ),
        )
        result[key] = snapshot
    return result


class FlowCache:
    def __init__(self, max_age_seconds: int = 150):
        self.max_age_seconds = max_age_seconds
        self._items: Dict[str, CapitalFlowSnapshot] = {}

    def put(self, snapshot: CapitalFlowSnapshot) -> None:
        self._items[snapshot.key] = snapshot

    def put_many(self, snapshots: Mapping[str, CapitalFlowSnapshot]) -> None:
        for snapshot in snapshots.values():
            self.put(snapshot)

    def get(self, key: str, now: Optional[datetime] = None) -> Optional[CapitalFlowSnapshot]:
        snapshot = self._items.get(key)
        return snapshot if is_fresh(snapshot, now, self.max_age_seconds) else None

    def get_any(self, key: str) -> Optional[CapitalFlowSnapshot]:
        return self._items.get(key)

    def needs_refresh(self, key: str, now: Optional[datetime] = None) -> bool:
        return self.get(key, now) is None

    def clear_stale(self, now: Optional[datetime] = None) -> None:
        now = now or datetime.now()
        self._items = {
            key: value
            for key, value in self._items.items()
            if is_fresh(value, now, self.max_age_seconds)
        }


def flow_direction(snapshot: Optional[CapitalFlowSnapshot], epsilon_yi: float = 0.01) -> Optional[str]:
    if snapshot is None or snapshot.net_yi is None:
        return None
    if snapshot.net_yi > epsilon_yi:
        return "inflow"
    if snapshot.net_yi < -epsilon_yi:
        return "outflow"
    return "neutral"


class FlowDirectionTracker:
    def __init__(self, required_count: int = 2):
        self.required_count = max(1, int(required_count))
        self._states: Dict[str, tuple] = {}

    def observe(self, snapshot: Optional[CapitalFlowSnapshot]) -> int:
        if snapshot is None:
            return 0
        direction = flow_direction(snapshot)
        if direction in (None, "neutral"):
            self._states.pop(snapshot.key, None)
            return 0
        previous_direction, count, previous_as_of = self._states.get(
            snapshot.key, (None, 0, None)
        )
        if previous_as_of == snapshot.as_of:
            return count
        count = count + 1 if direction == previous_direction else 1
        self._states[snapshot.key] = (direction, count, snapshot.as_of)
        return count

    def confirmed(self, key: str, direction: Optional[str] = None) -> bool:
        current_direction, count, _ = self._states.get(key, (None, 0, None))
        return count >= self.required_count and (
            direction is None or current_direction == direction
        )

    def direction(self, key: str) -> Optional[str]:
        return self._states.get(key, (None, 0, None))[0]


def flow_confirmation_state(
    signal_type: str,
    stock_snapshot: Optional[CapitalFlowSnapshot],
    sector_snapshot: Optional[CapitalFlowSnapshot],
    *,
    stock_confirmed: bool,
    sector_confirmed: bool,
) -> Optional[str]:
    """把资金流变成既有信号的确认状态，不独立产生信号。"""
    stock_direction = flow_direction(stock_snapshot)
    sector_direction = flow_direction(sector_snapshot)
    if not (stock_confirmed and sector_confirmed):
        return None
    if signal_type == "跌破支撑" and stock_direction == sector_direction == "outflow":
        return "risk_confirmed"
    if signal_type == "放量突破" and stock_direction == sector_direction == "inflow":
        return "breakout_confirmed"
    return None


def fetch_sector_flow(
    kind: str = "industry",
    *,
    as_of: Optional[datetime] = None,
    fetcher: Optional[Callable[[str], Any]] = None,
) -> Dict[str, CapitalFlowSnapshot]:
    """读取同花顺即时行业/概念资金流并标准化。"""
    as_of = as_of or datetime.now()
    if fetcher is None:
        import akshare as ak
        fetcher = ak.stock_fund_flow_industry if kind == "industry" else ak.stock_fund_flow_concept
    frame = fetcher("即时")
    return normalize_flow_frame(
        frame,
        kind="sector",
        key_column="行业",
        name_column="行业",
        net_column="净额",
        inflow_column="流入资金",
        outflow_column="流出资金",
        ratio_column="净占比",
        quote_date=as_of.strftime("%Y-%m-%d"),
        as_of=as_of,
        source=f"AkShare.stock_fund_flow_{kind}",
        scope="行业/概念板块",
        session="intraday",
    )


def _eastmoney_secid(code: str) -> str:
    text = str(code or "").lower().strip()
    if text.startswith("sh"):
        return f"1.{text[2:]}"
    if text.startswith("sz") or text.startswith("bj"):
        return f"0.{text[2:]}"
    return f"1.{text}" if text.startswith(("6", "68")) else f"0.{text}"


def fetch_stock_flow_snapshot(
    code: str,
    *,
    as_of: Optional[datetime] = None,
    requester: Optional[Callable[..., Any]] = None,
) -> Optional[CapitalFlowSnapshot]:
    """读取东方财富个股 1 分钟资金流最后一条，失败返回 None。"""
    as_of = as_of or datetime.now()
    requester = requester or requests.get
    params = {
        "lmt": "0",
        "klt": "1",
        "secid": _eastmoney_secid(code),
        "fields1": "f1,f2,f3,f7",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63,f64,f65",
        "ut": "b2884a393a59ad64002292a3e90d46a5",
    }
    try:
        response = requester(
            "https://push2.eastmoney.com/api/qt/stock/fflow/kline/get",
            params=params,
            timeout=12,
        )
        response.raise_for_status()
        payload = response.json()
        rows = ((payload.get("data") or {}).get("klines") or [])
        if not rows:
            return None
        parts = str(rows[-1]).split(",")
        if len(parts) < 7:
            return None
        stamp = parts[0]
        row_time = datetime.strptime(stamp[:16], "%Y-%m-%d %H:%M")
        return CapitalFlowSnapshot(
            key=str(code),
            name=str(code),
            kind="stock",
            quote_date=stamp[:10],
            as_of=row_time,
            source="EastMoney.fflow.kline",
            scope="个股",
            session="intraday",
            net_yi=parse_amount_yi(parts[1]),
            net_ratio_pct=parse_ratio_pct(parts[6]),
            large_net_yi=parse_amount_yi(parts[4]),
            super_large_net_yi=parse_amount_yi(parts[5]),
        )
    except (AttributeError, KeyError, IndexError, TypeError, ValueError, requests.RequestException):
        return None
