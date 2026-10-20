#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""大盘技术位计算：近N日区间、支撑与压力（盘面总结「技术看盘」用）。

两条硬性规则，杜绝“支撑写在现价上方”的荒谬结论
（例如现价 3824，报告却写“下方支撑 3888 / 3888 是多头防线”）：

1. 计算区间前必须把当日实时K线并入窗口。日K线接口在盘中/盘后
   一段时间内不含当日 bar，用旧窗口算出的支撑压力全部过时。
2. 支撑只能取现价下方的价位，压力只能取现价上方的价位；
   跌破箱体下沿后该位置转为压力，不能再叫“多头防线”。
"""

from dataclasses import dataclass
from typing import Dict, List, Optional


def merge_live_bar(klines, live) -> List[Dict]:
    """把当日实时行情并入日K线序列，返回新的 dict 列表。

    Args:
        klines: 日K线列表，元素含 date/open/high/low/close。
        live: get_stock_data() 的实时行情，需含 date 与 price。

    Returns:
        新列表。当日 bar 已存在则用实时值刷新其 OHLC（保留成交量等
        其余字段）；不存在则追加。live 缺 date/price，或其日期早于
        最后一根K线时，原样返回。
    """
    bars = [dict(k) for k in (klines or [])]
    price = (live or {}).get("price")
    day = (live or {}).get("date")
    if not price or not day:
        return bars

    high = max(float(live.get("high") or price), price)
    low = min(float(live.get("low") or price), price)
    bar = {
        "date": day,
        "open": float(live.get("open") or price),
        "close": price,
        "high": high,
        "low": low,
    }

    for i, existing in enumerate(bars):
        if existing.get("date") == day:
            merged = dict(existing)
            merged.update(bar)
            bars[i] = merged
            return bars

    if bars and (bars[-1].get("date") or "") > day:
        return bars

    bars.append(bar)
    return bars


@dataclass
class IndexLevels:
    """近N日区间与支撑/压力计算结果。"""

    low: float                      # 窗口最低
    high: float                     # 窗口最高
    prior_low: Optional[float]      # 不含当日的箱体下沿
    prior_high: Optional[float]     # 不含当日的箱体上沿
    broke_down: bool                # 现价跌破前箱体下沿
    broke_up: bool                  # 现价突破前箱体上沿
    supports: List[float]           # 现价下方参考位，由近及远
    resistances: List[float]        # 现价上方参考位，由近及远
    pos: float                      # 现价在区间中的位置 0~1


def compute_levels(price, bars, today: str = "") -> Optional[IndexLevels]:
    """计算窗口区间、破位状态与支撑/压力。

    Args:
        price: 现价。
        bars: 含当日的K线窗口（建议经 merge_live_bar 后取最近N根）。
        today: 当日日期字符串，用于把当日 bar 从“前箱体”中剔除；
            留空表示窗口内全部视为历史K线。

    Returns:
        IndexLevels，数据不足时返回 None。支撑全部低于现价，
        压力全部高于现价；跌破/突破的箱体边沿会作为反向参考位。
    """
    usable = [
        k for k in (bars or [])
        if k.get("high") is not None and k.get("low") is not None
    ]
    if not usable or not price:
        return None

    low = min(k["low"] for k in usable)
    high = max(k["high"] for k in usable)

    prior = [k for k in usable if k.get("date") != today] if today else usable
    prior_low = min(k["low"] for k in prior) if prior else None
    prior_high = max(k["high"] for k in prior) if prior else None

    broke_down = prior_low is not None and price < prior_low
    broke_up = prior_high is not None and price > prior_high

    # 候选参考位 = 窗口上下沿 + 前箱体上下沿；
    # 支撑只保留现价下方，压力只保留现价上方。
    candidates = {low, high}
    if prior_low is not None:
        candidates.add(prior_low)
    if prior_high is not None:
        candidates.add(prior_high)
    supports = sorted((v for v in candidates if v < price), reverse=True)
    resistances = sorted(v for v in candidates if v > price)

    span = high - low
    pos = (price - low) / span if span > 0 else 0.5

    return IndexLevels(
        low=low,
        high=high,
        prior_low=prior_low,
        prior_high=prior_high,
        broke_down=broke_down,
        broke_up=broke_up,
        supports=supports,
        resistances=resistances,
        pos=pos,
    )
