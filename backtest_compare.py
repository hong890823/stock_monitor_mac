#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跨样本回测对比：同一套规则在多个随机宇宙上是否给出一致结论。

单一样本的回测结论可能是运气；至少要跨 3 个独立抽样的股票宇宙看方向是否一致。
一致性看三件事：
1. **符号**：每个宇宙里超额收益是不是都为负（或都为正）；
2. **幅度**：量级是否接近，不能一个 −0.3% 一个 −2%（后者暗示样本依赖）；
3. **排序**：三态的相对次序是否稳定（突破是否总是最差）。

用法：
    python3 backtest_compare.py _backtest_entry_structure.json \
                              _backtest_entry_structure_s7.json ...
或直接跑默认的三个文件。
"""

from __future__ import annotations

import json
import os
import sys
from typing import Dict, List, Optional

DEFAULT_FILES = [
    "_backtest_entry_structure.json",
    "_backtest_entry_structure_s7.json",
    "_backtest_entry_structure_s2026.json",
]

STATES = [
    ("BASELINE", "基准（全部交易日）"),
    ("BREAKOUT", "放量突破前高"),
    ("BREAKOUT_BUY", "  其中 can_buy"),
    ("PULLBACK_M20", "回踩上行M20"),
    ("PULLBACK_M20_BUY", "  其中 can_buy"),
    ("NO_STRUCTURE", "暂无结构"),
    ("TREND_WEAK", "趋势转弱"),
]

HORIZON = 10


def load(path: str) -> Optional[dict]:
    if not os.path.exists(path):
        print(f"  [跳过] {path} 不存在", file=sys.stderr)
        return None
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def excess(payload: dict, key: str, horizon: int = HORIZON) -> Optional[dict]:
    """相对基准的超额：胜率差、均收益差、以及各自绝对值。"""
    stats = payload.get("stats", {})
    block = stats.get(key)
    base = stats.get("BASELINE")
    if not block or not base:
        return None
    s = block.get(f"ret_{horizon}d")
    b = base.get(f"ret_{horizon}d")
    if not s or not b:
        return None
    return {
        "n_raw": block.get("n_raw", 0),
        "n_dedup": block.get("n_dedup", 0),
        "win": s["win_rate"],
        "avg": s["avg"],
        "payoff": s["payoff"],
        "win_excess": s["win_rate"] - b["win_rate"],
        "avg_excess": s["avg"] - b["avg"],
    }


def main() -> int:
    paths = sys.argv[1:] or DEFAULT_FILES
    samples: List[tuple] = []
    for path in paths:
        payload = load(path)
        if payload:
            samples.append((path, payload))

    if not samples:
        print("没有可用的回测结果文件。先跑 backtest_entry_structure.py --seed N。")
        return 1

    print("=" * 88)
    print(f"跨样本对比 · {HORIZON} 日前瞻 · 共 {len(samples)} 个独立抽样宇宙")
    print("=" * 88)
    for path, payload in samples:
        rng = payload.get("date_range", ["?", "?"])
        print(
            f"  {os.path.basename(path):<40} "
            f"股票 {payload.get('universe', '?'):>4}  "
            f"判态 {payload.get('total_evals', '?'):>8}  "
            f"seed={payload.get('seed', '?')}  "
            f"{rng[0]} ~ {rng[1]}"
        )
    print()
    print("超额 = 该状态 − 同一样本的基准（全部交易日）")
    print()

    # ── 主表：每个宇宙一列 ──
    labels = [os.path.basename(p).replace("_backtest_entry_structure", "样本")
              .replace(".json", "") for p, _ in samples]
    labels = [l if l else "样本0" for l in labels]

    for key, name in STATES:
        rows = []
        for _, payload in samples:
            rows.append(excess(payload, key))
        if not any(rows):
            continue

        print(f"【{name}】")
        header = f"  {'指标':<16}"
        for label in labels:
            header += f"{label:>16}"
        header += f"{'一致性':>12}"
        print(header)

        for metric, fmt in [
            ("样本数(日)", "n_raw"),
            ("有效段数", "n_dedup"),
            ("胜率", "win"),
            ("胜率超额", "win_excess"),
            ("均收益%", "avg"),
            ("均收益超额", "avg_excess"),
            ("盈亏比", "payoff"),
        ]:
            line = f"  {metric:<16}"
            values = []
            for row in rows:
                if row is None or row.get(fmt) is None:
                    line += f"{'—':>16}"
                    continue
                v = row[fmt]
                values.append(v)
                if fmt in ("n_raw", "n_dedup"):
                    line += f"{int(v):>16}"
                elif fmt in ("win", "win_excess"):
                    line += f"{v * 100:>15.1f}%"
                elif fmt in ("avg", "avg_excess"):
                    line += f"{v * 100:>+15.2f}%"
                else:
                    line += f"{v:>16.2f}"

            # 一致性：方向是否都相同
            if fmt in ("win_excess", "avg_excess") and len(values) >= 2:
                neg = sum(1 for v in values if v < 0)
                pos = sum(1 for v in values if v > 0)
                if neg == len(values):
                    tag = "全为负"
                elif pos == len(values):
                    tag = "全为正"
                else:
                    tag = f"{pos}正{neg}负"
                line += f"{tag:>12}"
            else:
                line += f"{'':>12}"
            print(line)
        print()

    # ── 结论：跨样本的方向一致性 ──
    print("=" * 88)
    print("跨样本一致性结论")
    print("=" * 88)
    for key, name in STATES:
        if key == "BASELINE":
            continue
        rows = [excess(payload, key) for _, payload in samples]
        rows = [r for r in rows if r and r.get("avg_excess") is not None]
        if len(rows) < 2:
            continue
        negs = sum(1 for r in rows if r["avg_excess"] < 0)
        vals = [r["avg_excess"] * 100 for r in rows]
        spread = max(vals) - min(vals)
        if negs == len(rows):
            verdict = "✓ 方向一致（全部跑输基准）"
        elif negs == 0:
            verdict = "✓ 方向一致（全部跑赢基准）"
        else:
            verdict = "✗ 方向不一致（结论依赖样本）"
        print(f"{name}")
        print(f"  均收益超额：{vals} pp")
        print(f"  极差 {spread:.2f}pp｜{negs}/{len(rows)} 个样本为负 → {verdict}")

    print()
    print("读法：")
    print("· 方向一致 + 极差小 → 结论稳健，可据此改策略。")
    print("· 方向一致 + 极差大 → 方向可信但幅度不稳，改策略时留安全边际。")
    print("· 方向不一致 → 不能按此改策略，先怀疑样本期/宇宙差异。")
    print("· 三个样本共用 2022-07~2026-08 的市场环境，跨时期稳健性尚未检验。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
