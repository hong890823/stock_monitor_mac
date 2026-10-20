#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""入场结构参数敏感性：结论是「参数没调好」还是「结构本身不对」？

在已缓存的日线上重跑判态，只改一个参数看前瞻收益怎么变。
若改参数能把负超额变成正超额 → 调参即可；
若怎么调都是负 → 结构（追突破 / 接回踩）在该样本里就是反指，得换思路。
"""

from __future__ import annotations

import json
import warnings
from typing import Dict, List, Optional

warnings.filterwarnings("ignore")

from entry_structure import EntryState, EntryInput, evaluate
from backtest_entry_structure import load_cache, summarize, mark_episode_heads, SignalRecord

HORIZONS = (5, 10, 20)
WARMUP = 30


def eval_with(
    closes, highs, lows, volumes, t, *,
    expand=1.5, shrink=0.9, prior_lookback=20, slope_days=5,
    near=0.025, invalid_pct=0.02, bias_far=0.08,
):
    """手工构造 EntryInput（绕开 compute_entry_input 的固定参数），再调 evaluate。"""
    c = [float(x) for x in closes[:t + 1]]
    h = [float(x) for x in highs[:t + 1]]
    v = [float(x) for x in volumes[:t + 1]]
    n = len(c)
    if n < prior_lookback + slope_days + 1:
        return None

    prior_high = max(h[:-1][-prior_lookback:]) if n > 1 else h[-1]
    ma20 = sum(c[-20:]) / 20
    ma20_prev = sum(c[-(20 + slope_days):-slope_days]) / 20 if n >= 20 + slope_days else ma20
    above = sum(1 for x in c[-5:] if x > prior_high)

    below = 0
    for i in range(n - 1, 19, -1):
        ref = sum(c[i - 19:i + 1]) / 20
        if c[i] < ref:
            below += 1
        else:
            break

    prior_vols = [x for x in (v[:-1][-20:] if n > 1 else v[-20:]) if x > 0]
    vr = (v[-1] / (sum(prior_vols) / len(prior_vols))) if prior_vols and v[-1] > 0 else None
    chg = (c[-1] / c[-2] - 1) * 100 if n >= 2 and c[-2] > 0 else 0.0

    # 临时改 evaluate 的阈值：用 EntryInput 传参后再包一层开关
    entry = EntryInput(
        price=c[-1], prior_high=prior_high, ma20=ma20, ma20_prev=ma20_prev,
        volume_ratio=vr, closes_above_prior_high=above, closes_below_ma20=below,
        today_change_pct=chg, holding=False, benchmark_ok=None,
    )
    return entry, _evaluate_tuned(entry, expand=expand, shrink=shrink, near=near,
                                  invalid_pct=invalid_pct, bias_far=bias_far)


def _evaluate_tuned(entry, *, expand, shrink, near, invalid_pct, bias_far):
    """按给定阈值重算三态（逻辑与 entry_structure.evaluate 对齐，只换常量）。"""
    import entry_structure as es
    old = (es.VOLUME_EXPAND_RATIO, es.VOLUME_SHRINK_RATIO, es.PULLBACK_NEAR_PCT,
           es.INVALID_BREAK_PCT, es.BIAS_FAR_PCT)
    try:
        es.VOLUME_EXPAND_RATIO, es.VOLUME_SHRINK_RATIO = expand, shrink
        es.PULLBACK_NEAR_PCT, es.INVALID_BREAK_PCT, es.BIAS_FAR_PCT = near, invalid_pct, bias_far
        return es.evaluate(entry)
    finally:
        (es.VOLUME_EXPAND_RATIO, es.VOLUME_SHRINK_RATIO, es.PULLBACK_NEAR_PCT,
         es.INVALID_BREAK_PCT, es.BIAS_FAR_PCT) = old


def run_variant(cache: dict, **params) -> dict:
    records: List[SignalRecord] = []
    for code, bars in cache.items():
        if code.startswith("sh000"):
            continue
        closes = [b["close"] for b in bars]
        highs = [b["high"] for b in bars]
        lows = [b["low"] for b in bars]
        volumes = [b["volume"] for b in bars]
        dates = [b["date"] for b in bars]
        n = len(closes)
        for t in range(WARMUP, n - 25):
            out = eval_with(closes, highs, lows, volumes, t, **params)
            if out is None:
                continue
            entry, result = out
            rec = SignalRecord(
                code=code, date=dates[t], state=result.state.name,
                can_buy=result.can_buy, trend_weak=result.trend_weak,
                benchmark_ok=None, price=closes[t], stop_price=result.stop_price,
            )
            base = closes[t]
            for horizon in HORIZONS:
                end = t + horizon
                if end < n and base > 0:
                    rec.fwd[horizon] = closes[end] / base - 1
            records.append(rec)
    mark_episode_heads(records)
    return records


def main() -> int:
    cache = load_cache()
    codes = [c for c in cache if not c.startswith("sh000")]
    print(f"缓存股票数：{len(codes)}")

    # ── 基准 ──
    base_records = run_variant(cache)
    base_sum = summarize(base_records)

    def show(label, records, state_name):
        subset = [r for r in records if r.state == state_name]
        s = summarize(subset)
        heads = sum(1 for r in subset if r.is_state_head)
        row = f"{label:<44} N={len(subset):>6} 段={heads:>6}"
        for horizon in HORIZONS:
            d = s.get(f"ret_{horizon}d")
            if d:
                row += f"  {horizon}日 胜率{d['win_rate']*100:5.1f}% 均{d['avg']*100:+6.2f}%"
        print(row)
        return s

    print()
    print("── 突破结构：动量阈值敏感性（VOLUME_EXPAND_RATIO） ──")
    print(f"{'配置':<44}{'样本':>14}  前瞻收益")
    show("当前值 1.5", base_records, EntryState.BREAKOUT.name)
    for expand in (2.0, 2.5, 3.0, 4.0):
        recs = run_variant(cache, expand=expand)
        show(f"放量阈值 {expand}", recs, EntryState.BREAKOUT.name)

    print()
    print("── 突破结构：前高回看窗口敏感性（prior_lookback） ──")
    for lookback in (40, 60, 120):
        recs = run_variant(cache, prior_lookback=lookback)
        show(f"前高回看 {lookback} 日", recs, EntryState.BREAKOUT.name)

    print()
    print("── 突破结构：MA20 方向确认（slope_days） ──")
    for slope in (10, 20):
        recs = run_variant(cache, slope_days=slope)
        show(f"M20 方向回看 {slope} 日", recs, EntryState.BREAKOUT.name)

    print()
    print("── 回踩结构：容差敏感性 ──")
    show("当前值 near=2.5%", base_records, EntryState.PULLBACK_M20.name)
    for near in (0.015, 0.035, 0.05):
        recs = run_variant(cache, near=near)
        show(f"回踩容差 {near*100:.1f}%", recs, EntryState.PULLBACK_M20.name)

    print()
    print("── 对照：同一批数据的无条件基准 ──")
    for horizon in HORIZONS:
        d = base_sum.get(f"ret_{horizon}d")
        if d:
            print(f"  {horizon}日 胜率 {d['win_rate']*100:.1f}%  均收益 {d['avg']*100:+.2f}%  盈亏比 {d['payoff']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
