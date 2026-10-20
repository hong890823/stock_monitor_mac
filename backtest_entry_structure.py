#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""入场结构三态的历史回测。

回答一个问题：「放量突破前高 / 回踩上行M20 / 暂无结构」这三态，
在历史数据上是否真的带来不同的前瞻收益？

设计要点（这些决定了结果可不可信）：
1. **不用未来数据**：每个信号日 T 只用 T 及之前的 K 线判态，
   `entry_structure.compute_entry_input` 只看序列尾部，天然满足；
2. **要有基准线**：无条件前瞻收益（所有交易日）作为对照，
   否则「胜率 55%」在单边上涨的样本里毫无意义；
3. **重叠信号分开统计**：连续 10 天都是 BREAKOUT 时，
   既报「原始信号数」也报「去重后段数」（状态切换才算新信号），
   否则 N 会虚高、显著性被夸大；
4. **止损规则一并回测**：除持有到期外，另算「触发失效位即离场」的收益，
   用来检验失效位定得对不对——这是盈亏比的问题，不只是胜率；
5. **按大盘状态分层**：检验「指数在其 M20 下方时降级」这条过滤是否有效。

用法：
    python3 backtest_entry_structure.py --samples 300 --years 4
    python3 backtest_entry_structure.py --codes sh600519,sz000858   # 指定股票

结果落盘到 _backtest_entry_structure.json，报告打印到 stdout。
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import warnings
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

warnings.filterwarnings("ignore")

import requests

from entry_structure import EntryState, compute_entry_input, evaluate

# ── 常量 ─────────────────────────────────────────────────────────────────────

CACHE_PATH = "_backtest_bars_cache.json"
RESULT_PATH = "_backtest_entry_structure.json"   # 可被 --out 覆盖
HORIZONS = (5, 10, 20)          # 前瞻窗口（交易日）
WARMUP = 30                     # 信号日之前的最少 K 线数
API_DELAY = 0.12                # 取数间隔，避免限流
MIN_BARS_FOR_HORIZON = 25       # 保证 20 日前瞻可算


# ── 取数 ────────────────────────────────────────────────────────────────────

def fetch_sina_daily(code: str, datalen: int = 1023) -> Optional[List[dict]]:
    """新浪日线（前复权）。失败返回 None。"""
    url = (
        "https://money.finance.sina.com.cn/quotes_service/api/"
        "json_v2.php/CN_MarketData.getKLineData"
    )
    try:
        response = requests.get(
            url,
            params={"symbol": code, "scale": 240, "ma": "no", "datalen": datalen},
            headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://finance.sina.com.cn/",
            },
            timeout=15,
        )
        response.raise_for_status()
        rows = response.json()
    except Exception:
        return None
    if not isinstance(rows, list) or len(rows) < 60:
        return None

    bars = []
    for row in rows:
        try:
            bars.append({
                "date": str(row.get("day", "")),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row["volume"]),
            })
        except (KeyError, TypeError, ValueError):
            continue
    return bars if len(bars) >= 60 else None


def load_cache() -> dict:
    if os.path.exists(CACHE_PATH):
        try:
            with open(CACHE_PATH, encoding="utf-8") as handle:
                return json.load(handle)
        except Exception:
            return {}
    return {}


def save_cache(cache: dict) -> None:
    with open(CACHE_PATH, "w", encoding="utf-8") as handle:
        json.dump(cache, handle)


def fetch_universe(sample_size: int, seed: int) -> List[Tuple[str, str]]:
    """从全 A 股随机抽样（记录 seed 便于复现）。

    注意：这是**当前在市**股票，不含已退市标的，存在生存偏差——
    回测结果应视为「上界」而非真实可得收益。
    """
    import akshare as ak

    frame = ak.stock_info_a_code_name()
    rows = []
    for _, row in frame.iterrows():
        code = str(row["code"]).zfill(6)
        name = str(row["name"])
        # 只要沪深主板/创业板/科创板，跳过北交所（数据源口径不同）
        if code.startswith(("60", "68", "00", "30")):
            prefix = "sh" if code.startswith(("60", "68")) else "sz"
            rows.append((prefix + code, name))
    random.Random(seed).shuffle(rows)
    return rows[:sample_size]


# ── 回测核心 ────────────────────────────────────────────────────────────────

@dataclass
class SignalRecord:
    code: str
    date: str
    state: str
    can_buy: bool
    trend_weak: bool
    benchmark_ok: Optional[bool]
    price: float
    stop_price: Optional[float]
    fwd: Dict[int, float] = field(default_factory=dict)   # 持有到期收益
    stopped: bool = False                                  # 期内是否触发失效位
    stop_ret: Optional[float] = None                       # 触发失效位离场的收益
    is_state_head: bool = False                            # 状态片段首日
    is_weak_head: bool = False                             # 趋势转弱升沿


def classify_states(bars: List[dict], benchmark_above: Dict[str, bool]) -> List[SignalRecord]:
    """对单只股票逐日判态，返回信号记录（不含前瞻收益）。"""
    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    volumes = [b["volume"] for b in bars]
    dates = [b["date"] for b in bars]
    n = len(closes)

    records = []
    # 最后一天无法算 20 日前瞻，故到 n - MIN_BARS_FOR_HORIZON 为止
    for t in range(WARMUP, n - MIN_BARS_FOR_HORIZON):
        entry_input = compute_entry_input(
            closes[:t + 1],
            highs[:t + 1],
            lows[:t + 1],
            volumes[:t + 1],
            benchmark_ok=benchmark_above.get(dates[t]),
        )
        if entry_input is None:
            continue
        result = evaluate(entry_input)
        records.append(SignalRecord(
            code="",
            date=dates[t],
            state=result.state.name,
            can_buy=result.can_buy,
            trend_weak=result.trend_weak,
            benchmark_ok=entry_input.benchmark_ok,
            price=closes[t],
            stop_price=result.stop_price,
        ))
    return records


def attach_forward_returns(
    records: List[SignalRecord],
    bars: List[dict],
) -> None:
    """给每条信号补上前瞻收益与止损触发情况（用 T 之后的数据）。"""
    closes = [b["close"] for b in bars]
    lows = [b["low"] for b in bars]
    dates = [b["date"] for b in bars]
    index_of = {d: i for i, d in enumerate(dates)}

    for record in records:
        t = index_of.get(record.date)
        if t is None:
            continue
        base = closes[t]
        for horizon in HORIZONS:
            end = t + horizon
            if end < len(closes) and base > 0:
                record.fwd[horizon] = closes[end] / base - 1

        # 失效位触发明细：期内任一日最低价跌破 stop 即离场
        if record.stop_price and base > 0:
            for offset in range(1, 21):
                j = t + offset
                if j >= len(lows):
                    break
                if lows[j] <= record.stop_price:
                    record.stopped = True
                    record.stop_ret = record.stop_price / base - 1
                    break


def summarize(records: List[SignalRecord]) -> dict:
    """按状态汇总胜率 / 平均收益 / 盈亏比 / 止损触发率。"""
    out = {}
    for horizon in HORIZONS:
        rets = [r.fwd[horizon] for r in records if horizon in r.fwd]
        if not rets:
            out[f"ret_{horizon}d"] = None
            continue
        wins = [x for x in rets if x > 0]
        losses = [x for x in rets if x < 0]
        avg_win = sum(wins) / len(wins) if wins else 0.0
        avg_loss = abs(sum(losses) / len(losses)) if losses else 0.0
        sorted_rets = sorted(rets)
        median = sorted_rets[len(sorted_rets) // 2]
        out[f"ret_{horizon}d"] = {
            "n": len(rets),
            "win_rate": len(wins) / len(rets),
            "avg": sum(rets) / len(rets),
            "median": median,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            # 盈亏比：平均盈利 ÷ 平均亏损。>1 才说明「赚的时候赚得比亏时多」
            "payoff": (avg_win / avg_loss) if avg_loss > 0 else None,
            "profit_factor": (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else None,
        }

    stopped = [r for r in records if r.stopped]
    out["stop"] = {
        "n_with_stop": sum(1 for r in records if r.stop_price),
        "stopped": len(stopped),
        "stop_rate": (len(stopped) / sum(1 for r in records if r.stop_price))
                     if any(r.stop_price for r in records) else None,
        "avg_stop_ret": (sum(r.stop_ret for r in stopped) / len(stopped)) if stopped else None,
        "stopped_and_still_won_20d": sum(
            1 for r in stopped if r.fwd.get(20, 0) > 0
        ),
    }
    return out


def mark_episode_heads(records: List[SignalRecord]) -> None:
    """给每条记录标上「是否为状态片段首日」。

    必须在**单只股票的完整判态序列**上做：先按 state 过滤再找切换，
    会把整段同态压成 1 个片段，样本量被严重低估。

    趋势转弱是 flag 不是 state，单独按 flag 的升降沿打标。
    """
    by_code: Dict[str, List[SignalRecord]] = defaultdict(list)
    for record in records:
        by_code[record.code].append(record)

    for code in by_code:
        previous_state = None
        previous_weak = False
        for record in sorted(by_code[code], key=lambda r: r.date):
            record.is_state_head = record.state != previous_state
            record.is_weak_head = record.trend_weak and not previous_weak
            previous_state = record.state
            previous_weak = record.trend_weak


def count_episodes(records: List[SignalRecord], flag: str = "state") -> int:
    """统计片段数。flag='state' 按状态切换；flag='weak' 按趋势转弱升沿。"""
    attr = "is_state_head" if flag == "state" else "is_weak_head"
    return sum(1 for r in records if getattr(r, attr, False))


# ── 报告 ────────────────────────────────────────────────────────────────────

def _pct(x: Optional[float]) -> str:
    return f"{x * 100:+.2f}%" if x is not None else "—"


def _rate(x: Optional[float]) -> str:
    return f"{x * 100:.1f}%" if x is not None else "—"


def format_report(payload: dict) -> str:
    lines = []
    lines.append("=" * 72)
    lines.append("入场结构三态 · 历史回测报告")
    lines.append("=" * 72)
    lines.append(f"股票数：{payload['universe']}｜样本区间：{payload['date_range'][0]} ~ {payload['date_range'][1]}")
    lines.append(f"总判态次数：{payload['total_evals']}｜随机种子：{payload['seed']}")
    lines.append("前瞻收益 = T+N 收盘 ÷ T 收盘 − 1；信号在 T 收盘后判定，无未来数据。")
    lines.append("")

    # ── 主对照表 ──
    lines.append("【一】三态 vs 基准（持有到期）")
    lines.append("-" * 72)
    header = f"{'状态':<22}{'N':>7}{'去重N':>7}"
    for h in HORIZONS:
        header += f"  {h}日胜率{'':>2}{h}日均收益{'':>2}{h}日盈亏比"
    lines.append(header)

    for label, key in [
        ("基准（所有交易日）", "BASELINE"),
        ("放量突破前高", EntryState.BREAKOUT.name),
        ("  其中 can_buy", EntryState.BREAKOUT.name + "_BUY"),
        ("回踩上行M20", EntryState.PULLBACK_M20.name),
        ("  其中 can_buy", EntryState.PULLBACK_M20.name + "_BUY"),
        ("暂无结构", EntryState.NO_STRUCTURE.name),
        ("趋势转弱", "TREND_WEAK"),
    ]:
        block = payload["stats"].get(key)
        if not block:
            continue
        n_raw = block.get("n_raw", 0)
        n_dedup = block.get("n_dedup", 0)
        row = f"{label:<22}{n_raw:>7}{n_dedup:>7}"
        for h in HORIZONS:
            s = block.get(f"ret_{h}d")
            if s:
                payoff = f"{s['payoff']:.2f}" if s["payoff"] else "—"
                row += f"  {_rate(s['win_rate']):>8}{_pct(s['avg']):>10}{payoff:>8}"
            else:
                row += f"  {'—':>8}{'—':>10}{'—':>8}"
        lines.append(row)

    lines.append("")
    lines.append("【二】大盘过滤是否有效（BREAKOUT / PULLBACK 分层）")
    lines.append("-" * 72)
    for label, key in [
        ("大盘在MA20上方", "_MK_UP"),
        ("大盘在MA20下方", "_MK_DOWN"),
    ]:
        for state_key, state_name in [
            (EntryState.BREAKOUT.name, "突破"),
            (EntryState.PULLBACK_M20.name, "回踩"),
        ]:
            block = payload["stats"].get(state_key + key)
            if not block or not block.get("ret_10d"):
                continue
            s = block["ret_10d"]
            payoff = f"{s['payoff']:.2f}" if s["payoff"] else "—"
            lines.append(
                f"{state_name} · {label:<12} N={block['n_raw']:>6}  "
                f"10日胜率 {_rate(s['win_rate'])}  均收益 {_pct(s['avg'])}  盈亏比 {payoff}"
            )

    lines.append("")
    lines.append("【三】失效位（止损）检验")
    lines.append("-" * 72)
    for label, key in [
        ("放量突破前高", EntryState.BREAKOUT.name),
        ("回踩上行M20", EntryState.PULLBACK_M20.name),
    ]:
        block = payload["stats"].get(key)
        if not block or "stop" not in block:
            continue
        st = block["stop"]
        lines.append(
            f"{label}：带失效位信号 {st['n_with_stop']} 条，期内触发 {st['stopped']} 条"
            f"（{_rate(st['stop_rate'])}）"
        )
        if st["avg_stop_ret"] is not None:
            lines.append(f"  触发离场平均收益 {_pct(st['avg_stop_ret'])}（应为负，约为 −2%）")
        lines.append(
            f"  其中触发失效位后 20 日仍上涨的：{st['stopped_and_still_won_20d']} 条"
            f"——数字越大说明失效位越紧、越容易被洗出去"
        )

    lines.append("")
    lines.append("【四】分年度稳定性")
    lines.append("-" * 72)
    for year, block in sorted(payload.get("by_year", {}).items()):
        for key, name in [
            (EntryState.BREAKOUT.name, "突破"),
            (EntryState.PULLBACK_M20.name, "回踩"),
            (EntryState.NO_STRUCTURE.name, "无结构"),
            ("BASELINE", "基准"),
        ]:
            s = block.get(key)
            if not s or not s.get("ret_10d"):
                continue
            r = s["ret_10d"]
            lines.append(
                f"{year}  {name:<6} N={r['n']:>5}  10日胜率 {_rate(r['win_rate'])}  "
                f"均收益 {_pct(r['avg'])}"
            )

    lines.append("")
    lines.append("【五】如何读这份报告")
    lines.append("-" * 72)
    lines.append("· 胜率只有在高于「基准」时才有意义；单边上涨的年份里基准本身可能就 55%+。")
    lines.append("· 盈亏比（平均盈利÷平均亏损）要和胜率一起看：胜率 45% + 盈亏比 1.8 优于 60% + 0.8。")
    lines.append("· 「去重N」才是有效样本量；原始 N 里包含连续同态的重复计数。")
    lines.append("· 本回测不含交易成本、滑点、涨跌停无法成交；含生存偏差（退市股不在样本内）。")
    lines.append("· 样本是当前在市股票的随机抽样，结论对该宇宙成立，换宇宙需重跑。")
    return "\n".join(lines)


# ── 主流程 ──────────────────────────────────────────────────────────────────

def run(codes: Sequence[Tuple[str, str]], seed: int, out_path: str = RESULT_PATH) -> dict:
    cache = load_cache()
    benchmark_above: Dict[str, bool] = {}

    # ── 大盘基准：指数是否在其 MA20 上方（逐日） ──
    print("取大盘日线（sh000001）...", file=sys.stderr)
    bench = cache.get("sh000001") or fetch_sina_daily("sh000001", 1023)
    if bench:
        cache["sh000001"] = bench
        bcloses = [b["close"] for b in bench]
        for i in range(20, len(bcloses)):
            ma20 = sum(bcloses[i - 19:i + 1]) / 20
            benchmark_above[bench[i]["date"]] = bcloses[i] > ma20
    print(f"  大盘可用日期 {len(benchmark_above)} 天", file=sys.stderr)

    all_records: List[SignalRecord] = []
    fetched = 0
    skipped = 0
    dates_seen: List[str] = []

    for index, (code, name) in enumerate(codes, 1):
        bars = cache.get(code)
        if bars is None:
            bars = fetch_sina_daily(code)
            time.sleep(API_DELAY)
            if bars:
                cache[code] = bars
                if index % 50 == 0:
                    save_cache(cache)
            else:
                skipped += 1
                continue
        fetched += 1
        if not dates_seen:
            dates_seen = [b["date"] for b in bars]

        records = classify_states(bars, benchmark_above)
        attach_forward_returns(records, bars)
        for record in records:
            record.code = code
        all_records.extend(records)

        if index % 25 == 0:
            print(
                f"  进度 {index}/{len(codes)}｜累计信号 {len(all_records)}",
                file=sys.stderr,
            )

    save_cache(cache)

    # ── 汇总 ──
    mark_episode_heads(all_records)   # 必须在完整序列上打标，再分子集
    stats: Dict[str, dict] = {}

    def _collect(records: List[SignalRecord], flag: str = "state") -> dict:
        block = summarize(records)
        block["n_raw"] = len(records)
        block["n_dedup"] = count_episodes(records, flag)
        return block

    # 基准：所有判态日
    stats["BASELINE"] = _collect(all_records)

    for state in (EntryState.BREAKOUT, EntryState.PULLBACK_M20, EntryState.NO_STRUCTURE):
        subset = [r for r in all_records if r.state == state.name]
        stats[state.name] = _collect(subset)
        stats[state.name + "_BUY"] = _collect([r for r in subset if r.can_buy])
        # 大盘分层
        stats[state.name + "_MK_UP"] = _collect(
            [r for r in subset if r.benchmark_ok is True]
        )
        stats[state.name + "_MK_DOWN"] = _collect(
            [r for r in subset if r.benchmark_ok is False]
        )

    weak = [r for r in all_records if r.trend_weak]
    stats["TREND_WEAK"] = _collect(weak, flag="weak")

    # 分年度
    by_year: Dict[str, Dict[str, dict]] = defaultdict(dict)
    for record in all_records:
        year = record.date[:4]
        by_year[year].setdefault("_all", []).append(record)
        by_year[year].setdefault(record.state, []).append(record)
    by_year_out = {}
    for year, groups in by_year.items():
        by_year_out[year] = {
            key: summarize(recs) for key, recs in groups.items() if recs
        }
        by_year_out[year]["BASELINE"] = by_year_out[year].get("_all", {})
        by_year_out[year].pop("_all", None)

    date_all = sorted({r.date for r in all_records})
    payload = {
        "universe": fetched,
        "skipped": skipped,
        "seed": seed,
        "total_evals": len(all_records),
        "date_range": [date_all[0], date_all[-1]] if date_all else ["—", "—"],
        "stats": stats,
        "by_year": by_year_out,
    }

    with open(out_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="入场结构三态历史回测")
    parser.add_argument("--samples", type=int, default=300, help="随机抽样股票数")
    parser.add_argument("--seed", type=int, default=42, help="抽样随机种子")
    parser.add_argument("--codes", type=str, default="", help="指定股票，逗号分隔 sh600519,sz000858")
    parser.add_argument("--out", type=str, default=RESULT_PATH, help="结果 JSON 落盘路径")
    args = parser.parse_args()

    if args.codes:
        codes = []
        for raw in args.codes.split(","):
            raw = raw.strip().lower()
            if raw:
                codes.append((raw, raw))
    else:
        print(f"从全 A 股随机抽样 {args.samples} 只（seed={args.seed}）...", file=sys.stderr)
        codes = fetch_universe(args.samples, args.seed)
        print(f"  抽到 {len(codes)} 只", file=sys.stderr)

    payload = run(codes, seed=args.seed, out_path=args.out)
    print(format_report(payload))
    print(f"\n结果已存：{args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
