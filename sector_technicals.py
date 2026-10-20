#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""板块技术面：用成分股等权代理指数回答「个股弱是板块带的还是自己的」。

**为什么不直接用现成的板块指数**：同花顺有 975 根行业指数日线，但它和新浪是
**两套分类法** —— 2026-10-12 实测 49 个新浪板块与 90 个同花顺板块，去掉
「行业/板块/业」后缀后只对得上 7 个。混用会把个股归到错误板块，比没有更糟。
所以只用新浪口径（成分股来自 `stock_sector_detail`），指数自己等权合成。

**等权而非加权**：我们没有成分股的市值/权重数据，等权是唯一诚实的口径。
各成分股先按窗口首日收盘归一到 100 再平均 —— 否则高价股会主导指数。

**口径边界**：日线用新浪原始价（未复权），与项目里个股分析同口径。除权日在
代理指数上会产生跳空，看 20 日趋势时影响有限，看单日涨跌时要留意。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pandas as pd

# 合成代理指数至少要有这么多有效交易日，否则 analyze_history 会拒
MIN_PROXY_ROWS = 60

# 成分股日线的结束日落后最新结束日超过这么多天，就当停牌/退市/数据源给错丢掉。
# 2026-10-12 实测 sh600093 返回 2021-11~2022-06 的旧区间，一根坏线能让整个板块
# 的日期交集变空。
STALE_DAYS = 5

# 「个股跑输自己板块」的分档（百分点），与相对强弱 ±2pp 同口径
VS_SECTOR_BAND_PP = 2.0


def _to_numeric(series: "pd.Series") -> "pd.Series":
    return pd.to_numeric(series, errors="coerce")


def _drop_stale_frames(frames):
    """丢掉日期明显落后的成分股（停牌 / 退市 / 数据源给错区间）。

    2026-10-12 实测：汽车制造的 sh600093 返回的是 2021-11 ~ 2022-06 的旧区间，
    和其余 7 只的日期交集是 0 —— 一根坏线会让整个板块的指数合成失败。
    以「最新结束日」为准，落后超过 STALE_DAYS 的直接丢掉。
    """
    if len(frames) <= 1:
        return frames
    ends = []
    for frame in frames:
        dates = frame["date"].astype(str)
        ends.append(max(dates) if len(dates) else "")
    newest = max(ends)
    if not newest:
        return frames
    newest_at = _as_date(newest)
    kept = []
    for frame, end in zip(frames, ends):
        if not end:
            continue
        end_at = _as_date(end)
        # 解析不了的日期当落后丢掉 —— 宁可少一只，不要一根坏线拖垮整个板块
        if end_at is None or newest_at is None:
            continue
        if (newest_at - end_at).days <= STALE_DAYS:
            kept.append(frame)
    return kept


def _as_date(text) -> Optional["pd.Timestamp"]:
    try:
        return pd.Timestamp(str(text)[:10])
    except Exception:
        return None


def build_proxy_bars(frames: List["pd.DataFrame"], min_rows: int = MIN_PROXY_ROWS):
    """把多只成分股的日线等权合成一条板块代理指数。

    frames: 各成分股的日线，需含 date 与 close（有 open/high/low/volume 更好）。
    返回 DataFrame[date, open, high, low, close, volume]，或 None（有效成分不足）。

    日期用**并集**而不是交集：某只停牌一天不该让整条指数那天消失；
    该日它不参与均值（NaN 会被 mean 跳过），也不填 0、不前值。
    """
    usable = []
    for frame in frames or []:
        if frame is None or len(frame) < min_rows:
            continue
        data = frame.copy()
        date_col = _find_col(data, ["date", "日期", "时间", "day"])
        close_col = _find_col(data, ["close", "收盘", "收盘价"])
        if date_col is None or close_col is None:
            continue
        data = data.copy()
        data["date"] = data[date_col].astype(str)
        data["close"] = _to_numeric(data[close_col])
        data = data.dropna(subset=["date", "close"]).sort_values("date")
        if len(data) < min_rows:
            continue
        usable.append(data)

    usable = _drop_stale_frames(usable)

    normalized = []
    for data in usable:
        # 归一到首日 = 100，再参与等权平均（否则高价股主导指数）
        base = float(data["close"].iloc[0])
        if not base or base != base or base <= 0:
            continue
        scale = 100.0 / base
        keep = {"date": data["date"].values, "close": data["close"].values * scale}
        for target, aliases in (
            ("open", ["open", "开盘", "开盘价"]),
            ("high", ["high", "最高", "最高价"]),
            ("low", ["low", "最低", "最低价"]),
            ("volume", ["volume", "成交量"]),
        ):
            col = _find_col(data, aliases)
            if col is not None:
                values = _to_numeric(data[col])
                # 价格类同乘 scale；成交量不归一（后面直接求和）
                keep[target] = values.values if target == "volume" else values.values * scale
        normalized.append(pd.DataFrame(keep))

    if not normalized:
        return None
    return _equal_weight(normalized)


def _equal_weight(normalized: List["pd.DataFrame"]):
    """逐列等权平均（价格类）/ 求和（成交量）。日期取并集，缺的那天不参与。"""
    all_dates = sorted(set().union(*[set(f["date"]) for f in normalized]))
    if not all_dates:
        return None
    out = {"date": all_dates}
    for col in ("close", "open", "high", "low", "volume"):
        stacks = [
            frame.set_index("date").reindex(all_dates)[col]
            for frame in normalized if col in frame.columns
        ]
        if not stacks:
            continue
        table = pd.concat(stacks, axis=1)
        # mean 默认跳过 NaN：某只那天停牌，就只在其余成分股上取均值
        out[col] = table.sum(axis=1, min_count=1) if col == "volume" else table.mean(axis=1)
    result = pd.DataFrame(out).dropna(subset=["close"])
    return result if len(result) >= MIN_PROXY_ROWS else None


def _find_col(frame: "pd.DataFrame", aliases) -> Optional[str]:
    lower = {str(c).strip().lower(): c for c in frame.columns}
    for alias in aliases:
        if alias in frame.columns:
            return alias
        hit = lower.get(alias.strip().lower())
        if hit is not None:
            return hit
    return None


def summarize(result: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """从 `analyze_history` 结果里抽板块技术面要点。

    字段与个股用的同一套（ma20 / trend_weak / score / relative_strength…），
    这样「个股 vs 板块」可以直接相减比较。
    """
    result = result or {}
    entry = result.get("entry_structure") or {}
    vol = result.get("volatility") or {}
    ma20 = result.get("ma20")
    price = result.get("price")
    above = None
    if ma20 is not None and price is not None:
        above = float(price) >= float(ma20)
    return {
        "price": price,
        "change_pct": result.get("price_change"),
        "ma20": ma20,
        "above_ma20": above,
        "trend_weak": bool(entry.get("trend_weak")),
        "ma20_slope": entry.get("ma20_slope"),
        "state": entry.get("state"),
        "score": result.get("score"),
        "relative_strength": result.get("relative_strength"),
        "atr_pct": vol.get("atr_pct"),
        "volume_ratio": result.get("volume_ratio") or result.get("unified_volume_ratio"),
    }


def build_sector_technicals(sectors, sector_tech) -> Dict[str, Any]:
    """把各板块的技术面聚合成组合级视图，并给出「个股 vs 自己板块」的分解。

    - sectors:    `portfolio_daily_report.build_portfolio_sectors` 的输出
    - sector_tech: {板块名: summarize(...)}

    核心产出是 **vs_own_sector**：个股跑输**自己所属板块**几只。
    只比「个股 vs 沪指」答不了「是个股问题还是板块问题」——
    跟着板块一起弱的，是 β；连自己板块都跑输的，才是 α（个股自身）。
    """
    sector_tech = sector_tech or {}
    by_sector = list((sectors or {}).get("by_sector") or [])

    covered = [s for s in by_sector if s.get("name") in sector_tech]
    missing = [s.get("name") for s in by_sector if s.get("name") not in sector_tech]

    below_ma20 = sum(1 for s in covered if sector_tech[s["name"]].get("above_ma20") is False)
    weak = sum(1 for s in covered if sector_tech[s["name"]].get("trend_weak"))
    rs_vals = [sector_tech[s["name"]].get("relative_strength") for s in covered]
    rs_vals = [v for v in rs_vals if v is not None]
    rs_weak = sum(1 for v in rs_vals if v <= -VS_SECTOR_BAND_PP)
    rs_strong = sum(1 for v in rs_vals if v >= VS_SECTOR_BAND_PP)

    # 个股相对自己板块的分解由 split_vs_own_sector() 单独做 ——
    # 它需要「个股 rs」，这里只有板块 rs，不硬凑。
    return {
        "covered": len(covered),
        "missing": missing,
        "below_ma20": below_ma20,
        "trend_weak": weak,
        "rs_weak": rs_weak,
        "rs_strong": rs_strong,
        "rs_mean": (sum(rs_vals) / len(rs_vals)) if rs_vals else None,
        "by_sector": [
            {
                "name": s.get("name"),
                "count": s.get("count"),
                "change": s.get("change"),
                "weight_pct": s.get("weight_pct"),
                "stocks": list(s.get("stocks") or []),
                **(sector_tech.get(s.get("name")) or {}),
            }
            for s in by_sector
        ],
    }


def split_vs_own_sector(stocks_with_rs, sector_tech) -> Dict[str, List[dict]]:
    """个股相对**自己所属板块**的强弱分解。

    stocks_with_rs: [{"code","name","sector","rs"}]，rs 是个股近20日相对沪指。
    差值 = 个股 rs − 板块 rs，两者都减过沪指，相减即「个股相对自己板块」。
    """
    out: Dict[str, List[dict]] = {"lag": [], "inline": [], "lead": []}
    for item in stocks_with_rs or []:
        tech = sector_tech.get(item.get("sector") or "")
        sector_rs = (tech or {}).get("relative_strength")
        stock_rs = item.get("rs")
        if sector_rs is None or stock_rs is None:
            continue
        diff = float(stock_rs) - float(sector_rs)
        bucket = ("lag" if diff < -VS_SECTOR_BAND_PP
                  else "lead" if diff > VS_SECTOR_BAND_PP else "inline")
        out[bucket].append({
            "code": item.get("code"), "name": item.get("name"),
            "sector": item.get("sector"),
            "stock_rs": float(stock_rs), "sector_rs": float(sector_rs),
            "vs": diff,
        })
    for key in out:
        out[key].sort(key=lambda it: it["vs"])
    return out
