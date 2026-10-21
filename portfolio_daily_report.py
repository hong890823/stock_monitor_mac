#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""持仓信息报告：今日盈亏 + 技术面整体分析（菜单入口「📦 持仓信息」）。

只做计算与文案拼装，不碰网络、IO、UI —— 可单测、每次输出一致。
网络与调度在 stock_menubar.py，单股技术面在 portfolio_analysis.py。

三个容易算错的口径，集中在这里、不要散落实现：

1. **组合涨跌幅的基数**。今日盈亏合计是金额，沪指是涨跌幅，两者不可比。
   桥是「昨收持仓市值」day_base = Σ(q×prev_close)，于是
   today_total / day_base 正是组合当日收益率（昨收市值加权）。
   与成本、total_capital、现金全部无关 —— total_capital=0 时也成立。
   反过来，用 total_capital 当基数会把「今日涨跌」和「仓位轻重」混为一谈。

2. **优先处理是分档元组排序，不是打分**。深套只作档内标注，不单独造最高档，
   否则深套股永远霸占榜首、破位的盈利股反而看不见。

3. **交易日判据是行情日期，不是 weekday**。_is_trading_hours / _intraday_indicator_slot
   都只判 weekday()，没有节假日日历。唯一可靠判据是 sina 行情里的 date 字段。
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Dict, List, Optional

from deep_loss_policy import (
    deep_loss_threshold_pct,
    floating_pnl_pct,
    is_deep_loss,
)
from sector_technicals import (
    build_sector_technicals,
    split_vs_own_sector,
)
from market_evidence import (
    build_decision_review,
    build_evidence_cards,
    format_evidence_cards,
    summarize_t_filter_outcomes,
)
from continuation_hold_policy import format_continuation_hold_info
from strong_continuation_policy import format_strong_risk
from trade_action_guard import guard_actionable_copy

# ── 口径常量 ──────────────────────────────────────────────

# |超额| ≤ 这个值算「基本同步」，别把 0.01pp 的噪声讲成跑赢
EXCESS_NEUTRAL_PP = 0.15

# 15:00 收盘（自然日的分钟数）
FINALIZE_MINUTE = 900

# 定稿失败后的重试间隔（秒）。没写成定稿时（例如行情日期异常）才需要它 ——
# 不要拿「分析槽位」去重，合成槽位会和 _last_portfolio_analysis_slot 撞车。
FINALIZE_RETRY_SECONDS = 600

# 菜单信息行上限（不含「打开完整报告」与宽度填充）
MENU_SUMMARY_MAX_LINES = 10

# 第三节「优先处理」最多列几行，其余折叠成计数
ACTION_LIST_MAX = 6

# 相对强度强/弱的分档阈值，与 stock_analysis.signals 同口径
RS_STRONG_PP = 2.0
RS_WEAK_PP = -2.0

# 优先级档位（越小越急）
TIER_DEEP_BREAK = 0       # 深套破位：两套剧本决策点
TIER_SHALLOW_BREAK = 1    # 浅亏/盈利破位：反抽站不回就减
TIER_DEEP_WEAK = 2        # 深套但方向仍向下
TIER_TREND_WEAK = 3       # 趋势转弱
TIER_T_SELL = 4           # 冲高做T：是机会不是风险，但有动作
TIER_HOLD = 5             # 持有观察

TIER_LABELS = {
    TIER_DEEP_BREAK: "深套破位",
    TIER_SHALLOW_BREAK: "破位",
    TIER_DEEP_WEAK: "深套走弱",
    TIER_TREND_WEAK: "趋势转弱",
    TIER_T_SELL: "冲高做T",
    TIER_HOLD: "持有观察",
}

SIGNAL_BREAK = "减仓风险"
SIGNAL_TREND_WEAK = "趋势转弱观察"
SIGNAL_T_SELL = "冲高做T"


def format_t_filter_events(events) -> str:
    """格式化被强势环境暂缓的做T机会，并显示后续价格结果。"""
    lines = []
    for event in events or []:
        name = event.get("name") or event.get("code") or "未知股票"
        trigger = _safe_float(event.get("trigger_price"), 0.0)
        current = _safe_float(event.get("current_price"), 0.0)
        turnover = _safe_float(event.get("predicted_turnover"), 0.0)
        market = event.get("market_change_pct")
        sector = event.get("sector_change_pct")
        status = event.get("environment_status")
        reasons = [str(item) for item in (event.get("environment_reasons") or []) if item]
        outcome = "当前价格未取到"
        if trigger > 0 and current > 0:
            delta = (current / trigger - 1) * 100
            outcome = f"当前{current:.2f}（相对触发价{delta:+.2f}%）"
        context = []
        if turnover > 0:
            context.append(f"预估成交额{turnover:,.0f}亿")
        if market is not None:
            context.append(f"大盘{_safe_float(market):+.2f}%")
        if sector is not None:
            context.append(f"板块{_safe_float(sector):+.2f}%")
        context_text = "，".join(context)
        evidence = f"；环境判断：{status}" if status else ""
        if reasons:
            evidence += f"（{'；'.join(reasons[-3:])}）"
        max_favorable = event.get("max_favorable_pct")
        max_adverse = event.get("max_adverse_pct")
        if max_favorable is not None or max_adverse is not None:
            evidence += (
                f"；观察区间最高相对触发价{_safe_float(max_favorable):+.2f}%"
                f"、最低{_safe_float(max_adverse):+.2f}%"
            )
        lines.append(
            f"{name}：触发价{trigger:.2f}，{outcome}"
            + (f"；{context_text}" if context_text else "")
            + evidence
        )
    return "\n".join(lines)


# ── 小工具 ──────────────────────────────────────────────

def _safe_float(value, default=0.0):
    """任意来源的数值字段收敛成 float；None/空串/脏字符串/NaN 一律退默认值。

    行情与配置字段随时段和数据源会给出 None 或空串，直接参与比较或格式化
    会抛 TypeError，把整次报告生成打断（项目里为此立过 tests/test_menu_format.py）。
    """
    try:
        if value is None:
            return default
        result = float(value)
        if result != result:  # NaN
            return default
        return result
    except (TypeError, ValueError):
        return default


def _positive_float(value) -> Optional[float]:
    number = _safe_float(value, default=None)
    return number if number is not None and math.isfinite(number) and number > 0 else None


def _fmt_amount(value) -> str:
    """盈亏金额：带正负号与千分位，如 +1,234 / -770。"""
    return f"{_safe_float(value):+,.0f}"


def _fmt_pct(value, digits=2) -> Optional[str]:
    """百分比：带正负号；None 返回 None（调用方决定怎么降级）。"""
    if value is None:
        return None
    return f"{_safe_float(value):+.{digits}f}%"


def _fmt_wan(value) -> str:
    """金额的「万」缩写，用于菜单窄行：-16.7万。"""
    number = _safe_float(value)
    if abs(number) >= 10000:
        return f"{number / 10000:+,.1f}万"
    return _fmt_amount(number)


def _ceil_half(n: int) -> int:
    return (n + 1) // 2


# ── 盈亏（从 stock_menubar._build_daily_pnl 抽出，语义不变）──

def compute_pnl(positions, quotes) -> Dict[str, Any]:
    """持仓今日盈亏（相对昨收）与浮动盈亏（相对成本）。

    口径：
    - 只统计实际持仓（quantity>0）；观察股数量填 0，不参与盈亏。
    - 缺行情的票不计入合计与 day_base，单独计数 —— 被当成 0 盈亏静默加总，
      合计会偏低，比显示「--」更糟。
    - 成本为 0（未填）时不算浮动盈亏，避免把市值当成利润。
    - day_base = Σ(q×prev_close)，与 today_total 是同一集合，是组合涨跌幅的分母。
    """
    rows = []
    today_total = 0.0
    float_total = 0.0
    float_ready = False
    day_base = 0.0
    missing = 0
    watched_only = 0

    for position in positions or []:
        code = str(position.get("code", "") or "")
        name = position.get("name") or code
        quantity = _safe_float(position.get("quantity"), 0.0)
        if quantity <= 0:
            watched_only += 1
            continue
        cost = _safe_float(position.get("cost"), 0.0)
        live = (quotes or {}).get(code) or {}
        price = _safe_float(live.get("price"), 0.0)
        prev_close = _safe_float(live.get("prev_close"), 0.0)
        if price <= 0 or prev_close <= 0:
            rows.append({
                "code": code, "name": name, "quantity": quantity, "cost": cost,
                "price": 0.0, "prev_close": 0.0, "pct": None,
                "today": None, "floating": None, "market_value": 0.0,
            })
            missing += 1
            continue
        today = quantity * (price - prev_close)
        floating = quantity * (price - cost) if cost > 0 else None
        rows.append({
            "code": code, "name": name, "quantity": quantity, "cost": cost,
            "price": price, "prev_close": prev_close,
            "pct": (price - prev_close) / prev_close * 100,
            "today": today,
            "floating": floating,
            "market_value": quantity * price,
        })
        today_total += today
        day_base += quantity * prev_close
        if floating is not None:
            float_total += floating
            float_ready = True

    return {
        "rows": rows,
        "today_total": today_total,
        "float_total": float_total if float_ready else None,
        "day_base": day_base,
        "missing": missing,
        "watched_only": watched_only,
    }


def day_return_pct(pnl) -> Optional[float]:
    """组合今日涨跌幅（%）；基数不足时返回 None，不硬算。"""
    base = _safe_float((pnl or {}).get("day_base"), 0.0)
    if base <= 0:
        return None
    return _safe_float((pnl or {}).get("today_total"), 0.0) / base * 100


def compare_to_index(portfolio_pct, index_pct) -> Optional[Dict[str, Any]]:
    """组合涨跌幅与指数涨跌幅比较（都是百分点，量纲一致才可比）。"""
    if portfolio_pct is None:
        return None
    portfolio_pct = _safe_float(portfolio_pct)
    if index_pct is None:
        return {
            "portfolio_pct": portfolio_pct, "index_pct": None,
            "excess_pp": None, "verdict": "无法比较（缺沪指行情）",
        }
    index_pct = _safe_float(index_pct)
    excess = portfolio_pct - index_pct
    if excess > EXCESS_NEUTRAL_PP:
        verdict = "跑赢"
    elif excess < -EXCESS_NEUTRAL_PP:
        verdict = "跑输"
    else:
        verdict = "基本同步"
    return {
        "portfolio_pct": portfolio_pct, "index_pct": index_pct,
        "excess_pp": excess, "verdict": verdict,
    }


# ── 单股 ──────────────────────────────────────────────

def atr_pct_of(result, price=None) -> Optional[float]:
    """深套阈值用的 ATR%。

    优先 volatility.atr_pct（analyze_history 直接算好），退回 atr/现价。
    注意 result["atr"] 是 2026-10-12 补上的透传字段 —— 在那之前
    stock_menubar._check_intraday_signals 读 result.get("atr") 恒为 None，
    深套阈值静默退回固定 -20%，和报告这边的 ATR 缩放阈值不一致。
    """
    result = result or {}
    vol = result.get("volatility") or {}
    pct = _safe_float(vol.get("atr_pct"), 0.0)
    if pct > 0:
        return pct
    atr = _safe_float(result.get("atr"), 0.0) or _safe_float(vol.get("atr"), 0.0)
    px = _safe_float(price, 0.0) or _safe_float(result.get("price"), 0.0)
    if atr > 0 and px > 0:
        return atr / px * 100
    return None


def floating_pct_of(result) -> Optional[float]:
    """相对成本的浮亏幅度；成本未填返回 None。"""
    result = result or {}
    return floating_pnl_pct(result.get("cost"), result.get("price"))


def deep_loss_threshold_of(result) -> float:
    """这只股的深套阈值（负百分比），按 ATR 缩放。"""
    return deep_loss_threshold_pct(atr_pct_of(result))


def is_deep(result) -> bool:
    """是否深套。阈值口径只用 deep_loss_policy，不在这里重实现。"""
    result = result or {}
    return is_deep_loss(result.get("cost"), result.get("price"), atr_pct_of(result))


def is_broken(result) -> bool:
    """是否破位（跌破风险支撑）。破位是风险事件，优先级高于「趋势转弱」。"""
    return (result or {}).get("signal") == SIGNAL_BREAK


def _is_below_ma20(result) -> bool:
    result = result or {}
    if result.get("trend_weak"):
        return True
    ma20 = _safe_float(result.get("ma20"), 0.0)
    price = _safe_float(result.get("price"), 0.0)
    return ma20 > 0 and price > 0 and price < ma20


def priority_tier(result) -> int:
    """优先级档位（0 最急）。深套只作档内标注，不单独造最高档。"""
    result = result or {}
    signal = result.get("signal") or ""
    deep = is_deep(result)

    if signal == SIGNAL_BREAK:
        return TIER_DEEP_BREAK if deep else TIER_SHALLOW_BREAK
    if deep and _is_below_ma20(result):
        return TIER_DEEP_WEAK
    if signal == SIGNAL_TREND_WEAK or result.get("trend_weak"):
        return TIER_TREND_WEAK
    if signal == SIGNAL_T_SELL:
        return TIER_T_SELL
    return TIER_HOLD


def priority_sort_key(result, pnl_row) -> tuple:
    """排序键（升序）：先看档位，再看今天失血多少，再看伤得多重，再看评分。

    name 收尾保证确定性 —— 输入顺序来自 json，光靠 Python 的稳定排序不够。
    """
    result = result or {}
    pnl_row = pnl_row or {}
    return (
        priority_tier(result),
        -abs(_safe_float(pnl_row.get("today"), 0.0)),
        -abs(_safe_float(pnl_row.get("floating"), 0.0)),
        -_safe_float(result.get("score"), 0.0),
        str(result.get("name") or ""),
    )


# ── 组合聚合 ──────────────────────────────────────────

def _row_by_code(pnl) -> Dict[str, dict]:
    return {r.get("code"): r for r in (pnl or {}).get("rows", [])}


def _stocks_with_sector_rs(results, sector_map) -> List[dict]:
    """给 split_vs_own_sector 准备输入：每股带自己的 rs 与所属板块。

    一只股落在多个板块时取第一个 —— 新浪口径下 A 股是单行业归属，
    概念板块才是多归属，这里按行业算。
    """
    out = []
    for result in results or []:
        code = result.get("code", "")
        sectors = (sector_map or {}).get(code) or []
        if not sectors:
            continue
        out.append({
            "code": code,
            "name": result.get("name") or code,
            "sector": sectors[0],
            "rs": result.get("relative_strength"),
        })
    return out


def _top_by(rows, key, reverse, limit=3, require_sign=None):
    """取 |key| 最大的前 N 行。require_sign='neg' 只要负数，'pos' 只要正数。"""
    picked = []
    for row in rows:
        value = row.get(key)
        if value is None:
            continue
        value = _safe_float(value)
        if require_sign == "neg" and value >= 0:
            continue
        if require_sign == "pos" and value <= 0:
            continue
        picked.append(row)
    picked.sort(key=lambda r: abs(_safe_float(r.get(key), 0.0)), reverse=reverse)
    return picked[:limit]


def build_actions(results, pnl) -> List[dict]:
    """优先处理清单：只含实际持有且有行情的票，按优先级排好序。"""
    row_by_code = _row_by_code(pnl)
    items = []
    for result in results or []:
        code = result.get("code", "")
        row = row_by_code.get(code)
        if row is None or row.get("today") is None:
            continue  # 缺行情不进清单，另行计数
        if _safe_float(result.get("quantity"), 0.0) <= 0:
            continue  # 观察股
        items.append({
            "code": code,
            "name": result.get("name") or code,
            "tier": priority_tier(result),
            "tier_label": TIER_LABELS.get(priority_tier(result), ""),
            "today": row.get("today"),
            "floating": row.get("floating"),
            "floating_pct": floating_pct_of(result),
            "signal": result.get("signal"),
            "score": _safe_float(result.get("score"), 0.0),
            "support": _safe_float(result.get("support"), 0.0),
            "stop_price": result.get("entry_stop_price"),
            "deep": is_deep(result),
            "action": result.get("action") or "",
            "action_core": result.get("action_core") or result.get("action") or "",
            "situation_notes": list(result.get("situation_notes") or []),
        })
    items.sort(key=lambda it: (
        it["tier"],
        -abs(_safe_float(it["today"], 0.0)),
        -abs(_safe_float(it["floating"], 0.0)),
        -it["score"],
        it["name"],
    ))
    return items


def build_cautions(overview) -> List[str]:
    """组合级固定话术，规则触发。这里说的是「组合」的判断，不是个股。"""
    cautions = []
    n = int(overview.get("n_holdings") or 0)
    if n <= 0:
        return cautions
    trend = overview.get("trend") or {}
    risk_break = int(trend.get("risk_break") or 0)
    deep_loss = int(trend.get("deep_loss") or 0)
    half = _ceil_half(n)

    if risk_break >= half:
        cautions.append(
            f"过半持仓（{risk_break}/{n}）处于破位风险，组合层面先控总仓位再逐只处理；"
            f"若多只同日破位属系统性走弱，更不建议逐只补仓摊平。"
        )
    if deep_loss >= half:
        cautions.append(
            f"过半持仓（{deep_loss}/{n}）深套，避免同时补仓摊平，"
            "按清单逐只复核两套剧本（逻辑坏→等待反弹离场条件；"
            "逻辑在→等待站回支撑确认），不直接补仓或离场。"
        )
    vs_index = overview.get("vs_index") or {}
    if (vs_index.get("excess_pp") is not None
            and _safe_float(vs_index.get("excess_pp")) < 0
            and vs_index.get("index_pct") is not None
            and _safe_float(vs_index.get("index_pct")) < -1.0):
        cautions.append(
            "今日下跌以跟跌大盘为主；看第二节相对强弱计数，判断个股是否超跌于大盘。"
        )
    actions = overview.get("actions") or []
    if actions and all(it.get("tier", 0) >= TIER_T_SELL for it in actions):
        cautions.append("今日无破位/深套风险，维持观察即可，不必频繁操作。")
    return cautions


# 超跌 / 跟跌 / 抗跌 的分档（相对指数涨跌幅的百分点差）
VS_INDEX_BAND_PP = 2.0

# 日线落后多少天算「不新鲜」，与 stock_analysis.STALE_DATA_DAYS 同口径
STALE_DATA_DAYS = 10

# 单一板块市值权重到这个比例算「板块集中」
SECTOR_CONCENTRATED_PCT = 40.0


def build_portfolio_sectors(results, sector_map, sector_quotes) -> Dict[str, Any]:
    """持仓的板块归属、集中度、各板块今日强弱。

    要答的问题是「这堆亏损是板块踩雷还是全市场」——
    12 只分散在 10 个板块还同跌，那就是 β；集中在 2 个板块同跌，才要查板块。

    - sector_map:    {code: [板块名, ...]}，来源 stock_sector_detail 反查
    - sector_quotes: {板块名: {"change": 今日涨跌%}}，来源 stock_sector_spot
    板块名用**新浪口径**（不要混同花顺 —— 两套分类法对不齐，2026-10-12 实测
    49 个板块去后缀后只对得上 7 个，混用会对错板块）。
    """
    sector_map = sector_map or {}
    sector_quotes = sector_quotes or {}
    by_sector: Dict[str, Dict[str, Any]] = {}
    unclassified = []
    total_weight = 0.0

    for result in results or []:
        code = result.get("code", "")
        name = result.get("name") or code
        # 权重用市值；调用方没给 market_value 时退回 数量×现价
        weight = _safe_float(result.get("market_value"), 0.0)
        if weight <= 0:
            weight = _safe_float(result.get("quantity"), 0.0) * _safe_float(result.get("price"), 0.0)
        total_weight += weight
        sectors = sector_map.get(code) or []
        if not sectors:
            unclassified.append(name)
            continue
        for sector in sectors:
            bucket = by_sector.setdefault(sector, {
                "name": sector, "stocks": [], "count": 0,
                "weight": 0.0, "change": None,
            })
            bucket["stocks"].append(name)
            bucket["count"] += 1
            bucket["weight"] += weight
            quote = sector_quotes.get(sector) or {}
            if quote.get("change") is not None:
                bucket["change"] = _safe_float(quote.get("change"))

    for bucket in by_sector.values():
        if total_weight > 0:
            bucket["weight_pct"] = bucket["weight"] / total_weight * 100
        else:
            bucket["weight_pct"] = None

    ordered = sorted(
        by_sector.values(),
        key=lambda b: (-(b["weight_pct"] or 0), b["name"]),
    )
    top_pct = ordered[0]["weight_pct"] if ordered else None
    falling = sum(1 for b in ordered if b["change"] is not None and b["change"] < -0.15)
    rising = sum(1 for b in ordered if b["change"] is not None and b["change"] > 0.15)

    return {
        "by_sector": ordered,
        "n_sectors": len(ordered),
        "top_sector": ordered[0] if ordered else None,
        "top_sector_pct": top_pct,
        "concentrated": top_pct is not None and top_pct >= SECTOR_CONCENTRATED_PCT,
        "falling": falling,
        "rising": rising,
        "unclassified": unclassified,
    }


def sector_lines(sectors) -> List[str]:
    """板块维度三行：分布 / 集中度 / 板块今日强弱。"""
    if not sectors or not sectors.get("by_sector"):
        if sectors and sectors.get("unclassified"):
            return [f"  板块：{len(sectors['unclassified'])} 只未能归类（缺板块成分数据）"]
        return ["  板块：暂无板块归属数据"]

    def _chg(value):
        return _fmt_pct(value) if value is not None else "--"

    lines = []
    n = sectors.get("n_sectors", 0)
    lines.append(f"  板块分布：{n} 个板块"
                 + ("｜⚠ 板块集中" if sectors.get("concentrated") else "｜板块分散"))
    top = sectors.get("top_sector") or {}
    if top:
        pct = top.get("weight_pct")
        pct_text = f"（占市值 {pct:.0f}%）" if pct is not None else ""
        lines.append(
            f"  最重板块：{top.get('name')}{pct_text}，"
            f"{top.get('count')} 只 · 今日{_chg(top.get('change'))}"
        )
    names = "／".join(
        f"{b['name']}{_chg(b['change'])}"
        for b in (sectors.get("by_sector") or [])[:5]
    )
    if names:
        lines.append(f"  板块今日：{names}")
    if sectors.get("unclassified"):
        lines.append(f"  未归类 {len(sectors['unclassified'])} 只")
    return lines


def build_attribution(results, pnl, index_pct, total_market_value) -> Dict[str, Any]:
    """今日归因：这堆亏损是不是同一件事。

    数分布（跌了几只）答不了「为什么亏」。要答的是：
    - **同涨同跌**：全跌 = 系统性 β；有涨有跌 = 个股 α
    - **相对大盘**：跟跌大盘 vs 超跌于大盘 vs 抗跌 —— 分开「β 拖累」和「个股更弱」
    - **仓位集中度**：亏损是个股集中还是结构分散
    """
    rows = [r for r in (pnl or {}).get("rows", []) if r.get("today") is not None]
    ups = [r for r in rows if _safe_float(r.get("pct"), 0.0) > 0.15]
    downs = [r for r in rows if _safe_float(r.get("pct"), 0.0) < -0.15]
    flats = [r for r in rows if r not in ups and r not in downs]

    # 相对大盘分档：超跌（跑输 >2pp）/ 跟跌（±2pp 内）/ 抗跌（跑赢 >2pp）
    buckets = {"lag": [], "inline": [], "lead": []}
    if index_pct is not None:
        for row in rows:
            pct = _safe_float(row.get("pct"), 0.0)
            diff = pct - _safe_float(index_pct)
            item = {
                "code": row.get("code"), "name": row.get("name"),
                "pct": pct, "vs": diff,
            }
            if diff < -VS_INDEX_BAND_PP:
                buckets["lag"].append(item)
            elif diff > VS_INDEX_BAND_PP:
                buckets["lead"].append(item)
            else:
                buckets["inline"].append(item)
        for key in buckets:
            buckets[key].sort(key=lambda it: it["vs"])

    # 仓位集中度：按市值权重，total_capital=0 也照算
    weights = sorted(
        (_safe_float(r.get("market_value"), 0.0) for r in rows),
        reverse=True,
    )
    top3_pct = None
    if total_market_value and _safe_float(total_market_value) > 0 and weights:
        top3_pct = sum(weights[:3]) / _safe_float(total_market_value) * 100

    return {
        "up": len(ups), "down": len(downs), "flat": len(flats),
        "all_down": bool(rows) and len(downs) == len(rows),
        "all_up": bool(rows) and len(ups) == len(rows),
        # 11/12 收跌也该读作「系统性走弱、一只抗跌」，不是「个股分化」——
        # 纯字面的 all_down 会把这种日子讲反（2026-10-12 冒烟抓到）。
        # 阈值「至少 n-1 只且 n≥3」：2 只里跌 1 只是对半，不算系统性。
        "mostly_down": bool(rows) and len(rows) >= 3 and len(downs) >= len(rows) - 1,
        "mostly_up": bool(rows) and len(rows) >= 3 and len(ups) >= len(rows) - 1,
        "up_names": [r.get("name") for r in ups],
        "down_names": [r.get("name") for r in downs],
        "vs_index": buckets,
        "concentration_top3_pct": top3_pct,
    }


def build_conclusion(overview) -> str:
    """组合判断依据：解释为什么给出当前整体操作方向。

    前面偏重事实归因，这里把这些事实收敛成组合级判断。
    """
    n = int(overview.get("n_holdings") or 0)
    if n <= 0:
        return "尚未配置实际持仓。"
    attr = overview.get("attribution") or {}
    t = overview.get("trend") or {}
    parts = []

    if attr.get("all_down"):
        parts.append(
            f"{n} 只今日全部收跌，属系统性 β 而非个股 α，"
            f"不建议今日逐只动手"
        )
    elif attr.get("mostly_down"):
        survivors = "、".join(attr.get("up_names") or [])
        tail = f"，仅{survivors}抗跌，那只优先关注" if survivors else ""
        parts.append(
            f"{attr.get('down')}/{n} 收跌，属系统性走弱而非个股分化{tail}，"
            f"不建议今日逐只动手"
        )
    elif attr.get("all_up"):
        parts.append(f"{n} 只今日全部收涨，注意是否同步过热")
    elif attr.get("mostly_up"):
        parts.append(f"{attr.get('up')}/{n} 收涨，整体偏强")
    else:
        parts.append(
            f"今日涨跌分化（涨 {attr.get('up', 0)} 跌 {attr.get('down', 0)}），属个股行情，"
            f"按风险优先处理清单逐只处理"
        )

    # 板块维度：分散同跌 = 全市场 β；集中同跌 = 先查板块
    sectors = overview.get("sectors") or {}
    n_sectors = int(sectors.get("n_sectors") or 0)
    systemic = attr.get("all_down") or attr.get("mostly_down")
    if n_sectors and systemic:
        if n_sectors >= 6:
            parts.append(f"且分散在 {n_sectors} 个板块，非板块踩雷，是全市场走弱")
        elif n_sectors <= 3:
            parts.append(f"且集中在 {n_sectors} 个板块，优先查板块是否集体走坏")
    elif n_sectors and sectors.get("concentrated"):
        top = sectors.get("top_sector") or {}
        parts.append(
            f"板块集中于「{top.get('name')}」"
            f"{(top.get('weight_pct') or 0):.0f}%，板块风险会放大成组合风险"
        )

    buckets = attr.get("vs_index") or {}
    lag = buckets.get("lag") or []
    if lag:
        worst = "、".join(
            f"{it['name']}{it['vs']:+.1f}pp" for it in lag[:3]
        )
        parts.append(f"超跌于大盘 {len(lag)} 只（{worst}）需分清是超跌还是基本面")

    # 板块技术面把「β 还是 α」再切一刀：跟自己板块一起弱 = 板块带的；
    # 连自己板块都跑输 = 个股自身问题，那才该单独盯。
    vs_own = overview.get("vs_own_sector") or {}
    lag_own = vs_own.get("lag") or []
    if lag_own:
        names = "、".join(f"{it['name']}{it['vs']:+.1f}pp" for it in lag_own[:3])
        parts.append(
            f"其中 {len(lag_own)} 只连自己板块都跑输（{names}），"
            f"这部分是个股自身问题而非板块拖累"
        )
    elif vs_own.get("inline"):
        parts.append(
            f"{len(vs_own.get('inline') or [])} 只都跟着自己板块走，弱势属板块 β 而非个股 α"
        )

    top3 = attr.get("concentration_top3_pct")
    if top3 is not None:
        if top3 >= 50:
            parts.append(f"仓位集中（前 3 只占 {top3:.0f}%），个股风险会放大成组合风险")
        else:
            parts.append(f"仓位分散（前 3 只占 {top3:.0f}%）")

    if int(t.get("risk_break") or 0) and int(t.get("deep_loss") or 0):
        parts.append("已有破位与深套叠加，优先按两套剧本处理最急的那几只")
    return "；".join(parts) + "。"


def build_overview(results, pnl, market) -> Dict[str, Any]:
    """把 12 只的单股结果聚合成组合级判断。

    聚合与呈现分离：报告和菜单都从这个 dict 渲染，口径只在这里定义一次。
    """
    results = list(results or [])
    pnl = pnl or {}
    rows = pnl.get("rows", [])
    row_by_code = _row_by_code(pnl)

    # 数据覆盖
    n_holdings = len(rows)
    n_watched = int(pnl.get("watched_only") or 0)
    n_missing = int(pnl.get("missing") or 0)
    n_with_quote = n_holdings - n_missing

    # 市值权重（total_capital=0 时 position_pct 全是 None，但市值权重永远可算）
    total_market_value = sum(_safe_float(r.get("market_value"), 0.0) for r in rows)

    def _with_weight(row):
        item = {
            "code": row.get("code"), "name": row.get("name"),
            "today": row.get("today"), "floating": row.get("floating"),
            "pct": row.get("pct"),
            "floating_pct": floating_pct_of({"cost": row.get("cost"), "price": row.get("price")}),
            "weight": None,
        }
        mv = _safe_float(row.get("market_value"), 0.0)
        if total_market_value > 0 and mv > 0:
            item["weight"] = mv / total_market_value * 100
        return item

    today_values = [_safe_float(r.get("today"), 0.0) for r in rows if r.get("today") is not None]
    sum_abs_today = sum(abs(v) for v in today_values)

    def _share(row):
        if sum_abs_today <= 0 or row.get("today") is None:
            return None
        return abs(_safe_float(row.get("today"))) / sum_abs_today * 100

    def _top(rows_, key, sign, limit=3):
        out = []
        for row in _top_by(rows_, key, reverse=True, limit=limit, require_sign=sign):
            item = _with_weight(row)
            item["share"] = _share(row)
            out.append(item)
        return out

    top_drag = _top(rows, "today", "neg")
    top_boost = _top(rows, "today", "pos")
    top_float_loss = _top(rows, "floating", "neg")

    winners = sum(1 for v in today_values if v > 0)
    losers = sum(1 for v in today_values if v < 0)
    flat = sum(1 for v in today_values if v == 0)

    portfolio_pct = day_return_pct(pnl)
    index_pct = (market or {}).get("index_pct")
    if index_pct is not None:
        index_pct = _safe_float(index_pct)

    # ── 技术面（只从已分析的 results 聚合，缺行情的不在里面）──
    below_ma20 = above_ma20 = ma20_unknown = 0
    trend_weak = 0
    deep_loss = 0
    risk_break = 0
    deep_and_break = 0
    score_sum = 0.0
    score_count = 0
    score_neg = 0
    worst = None
    rs_strong = rs_weak = 0
    benchmark_filtered = 0
    vp_risk_tags = 0
    pos_low = pos_mid = pos_high = 0
    gap_down_or_fade = 0
    entry = {"BREAKOUT": 0, "PULLBACK_M20": 0, "NO_STRUCTURE": 0}

    for result in results:
        deep = is_deep(result)
        broken = is_broken(result)
        if deep:
            deep_loss += 1
        if broken:
            risk_break += 1
        if deep and broken:
            deep_and_break += 1

        ma20 = _safe_float(result.get("ma20"), 0.0)
        price = _safe_float(result.get("price"), 0.0)
        if ma20 <= 0 or price <= 0:
            ma20_unknown += 1
        elif price < ma20:
            below_ma20 += 1
        else:
            above_ma20 += 1

        if result.get("trend_weak") or result.get("signal") == SIGNAL_TREND_WEAK:
            trend_weak += 1

        score = _safe_float(result.get("score"), 0.0)
        score_sum += score
        score_count += 1
        if score < 0:
            score_neg += 1
        if worst is None or score < worst[1]:
            worst = (result.get("name") or result.get("code", ""), score)

        rs = result.get("relative_strength")
        if rs is not None:
            rs = _safe_float(rs)
            if rs >= RS_STRONG_PP:
                rs_strong += 1
            elif rs <= RS_WEAK_PP:
                rs_weak += 1

        if result.get("benchmark_ok") is False:
            benchmark_filtered += 1

        tag = str(result.get("volume_price_tag") or "")
        if ("跌" in tag) or ("滞涨" in tag):
            vp_risk_tags += 1

        loc = ((result.get("price_position") or {}).get("location_key") or "").upper()
        if loc == "LOW":
            pos_low += 1
        elif loc == "HIGH":
            pos_high += 1
        elif loc == "MID":
            pos_mid += 1

        opening = result.get("opening") or {}
        structure = str(opening.get("structure") or "")
        gap_pct = _safe_float(opening.get("gap_pct"), 0.0)
        if "低开低走" in structure or "高开低走" in structure or abs(gap_pct) >= 1.0:
            gap_down_or_fade += 1

        state = result.get("entry_state") or "NO_STRUCTURE"
        entry[state] = entry.get(state, 0) + 1

    # 板块维度：归属/集中度（来自成分股映射）+ 技术面（来自等权代理指数）
    sectors = build_portfolio_sectors(
        results,
        (market or {}).get("sector_map"),
        (market or {}).get("sector_quotes"),
    )
    sector_tech_map = (market or {}).get("sector_tech") or {}
    sector_tech_agg = build_sector_technicals(sectors, sector_tech_map)
    vs_own_sector = split_vs_own_sector(
        _stocks_with_sector_rs(results, (market or {}).get("sector_map") or {}),
        sector_tech_map,
    )

    market = market or {}
    market_quality = market.get("market_environment") or market.get("quality")
    market_evidence = dict(market)
    if market_quality is not None:
        for field in (
            "status", "predicted_turnover_yi", "persistence_days",
            "persistence_average_yi", "industry_breadth", "concept_breadth",
            "market_change_pct", "index_near_high", "activity_label",
        ):
            if not hasattr(market_quality, field):
                continue
            value = getattr(market_quality, field)
            if field == "status" and hasattr(value, "value"):
                value = value.value
            market_evidence.setdefault(field, value)

    overview = {
        "n_holdings": n_holdings,
        "n_watched": n_watched,
        "n_missing": n_missing,
        "n_with_quote": n_with_quote,
        "total_market_value": total_market_value,
        "pnl": {
            "today_total": pnl.get("today_total"),
            "float_total": pnl.get("float_total"),
            "day_base": pnl.get("day_base"),
            "today_pct": portfolio_pct,
            "winners": winners, "losers": losers, "flat": flat,
            "sum_abs_today": sum_abs_today,
            "top_drag": top_drag, "top_boost": top_boost,
            "top_float_loss": top_float_loss,
        },
        "vs_index": compare_to_index(portfolio_pct, index_pct),
        "trend": {
            "below_ma20": below_ma20, "above_ma20": above_ma20,
            "ma20_unknown": ma20_unknown, "trend_weak": trend_weak,
            "deep_loss": deep_loss, "risk_break": risk_break,
            "deep_and_break": deep_and_break,
            "score_mean": (score_sum / score_count) if score_count else None,
            "score_neg": score_neg,
            "worst": ({"name": worst[0], "score": worst[1]} if worst else None),
            "rs_strong": rs_strong, "rs_weak": rs_weak,
            "benchmark_filtered": benchmark_filtered,
            "vp_risk_tags": vp_risk_tags,
            "pos_low": pos_low, "pos_mid": pos_mid, "pos_high": pos_high,
            "gap_down_or_fade": gap_down_or_fade,
            "entry": entry,
        },
        "attribution": build_attribution(results, pnl, index_pct, total_market_value),
        "sectors": sectors,
        "sector_tech": sector_tech_agg,
        "vs_own_sector": vs_own_sector,
        "market_timing": (market or {}).get("market_timing") or "",
        "capital_flow": (market or {}).get("capital_flow") or "",
        "t_filter_events": (market or {}).get("t_filter_events") or [],
        "evidence_cards": build_evidence_cards(market_evidence),
        "decision_review": build_decision_review(market_evidence, {
            "trend": {
                "risk_break": risk_break,
            },
        }),
        "actions": build_actions(results, pnl),
        "cautions": [],
    }
    overview["cautions"] = build_cautions(overview)
    overview["conclusion"] = build_conclusion(overview)
    return overview


# ── 渲染：完整报告 ──────────────────────────────────────

def _share_text(item) -> str:
    """今日盈亏占全部|今日盈亏|的份额。只用于今日榜，不用于浮亏榜。"""
    share = item.get("share")
    return f"（{share:.0f}%）" if share is not None else ""


def _today_name_list(items, limit=3) -> str:
    """今日榜：金额 + 占|今日盈亏|份额。"""
    parts = []
    for item in (items or [])[:limit]:
        parts.append(f"{item.get('name')} {_fmt_amount(item.get('today'))}{_share_text(item)}")
    return "／".join(parts) if parts else "无"


def _float_name_list(items, limit=3) -> str:
    """浮亏榜：金额 + 相对成本的浮亏幅度（**不是**今日份额，两者口径不同）。"""
    parts = []
    for item in (items or [])[:limit]:
        pct = item.get("floating_pct")
        pct_text = f"（{pct:+.1f}%）" if pct is not None else ""
        parts.append(f"{item.get('name')} {_fmt_amount(item.get('floating'))}{pct_text}")
    return "／".join(parts) if parts else "无"


def _attribution_lines(attr) -> List[str]:
    """归因三行：同涨同跌 / 相对大盘 / 仓位集中度。"""
    lines = []
    if not attr:
        return lines
    if attr.get("all_down"):
        lines.append(f"  同涨同跌：{attr.get('down', 0)} 只今日全部收跌 → 系统性β 而非个股α")
    elif attr.get("mostly_down"):
        survivors = "、".join(attr.get("up_names") or [])
        tail = f"，仅{survivors}抗跌" if survivors else ""
        lines.append(
            f"  同涨同跌：跌 {attr.get('down', 0)} / {attr.get('down', 0) + attr.get('up', 0) + attr.get('flat', 0)}"
            f" → 系统性走弱{tail}"
        )
    elif attr.get("all_up"):
        lines.append(f"  同涨同跌：{attr.get('up', 0)} 只今日全部收涨")
    elif attr.get("mostly_up"):
        lines.append(f"  同涨同跌：涨 {attr.get('up', 0)} 只，整体偏强")
    else:
        lines.append(
            f"  同涨同跌：涨 {attr.get('up', 0)} / 跌 {attr.get('down', 0)}"
            f" / 平 {attr.get('flat', 0)} → 个股分化，非系统性"
        )

    buckets = attr.get("vs_index") or {}
    if buckets:
        lag = buckets.get("lag") or []
        inline = buckets.get("inline") or []
        lead = buckets.get("lead") or []
        lag_text = ""
        if lag:
            lag_text = "（" + "／".join(
                f"{it['name']}{it['vs']:+.1f}pp" for it in lag[:3]
            ) + "）"
        lines.append(
            f"  相对大盘：跟跌 {len(inline)}｜超跌 {len(lag)}{lag_text}｜抗跌 {len(lead)}"
        )

    top3 = attr.get("concentration_top3_pct")
    if top3 is not None:
        lines.append(f"  仓位集中度：前 3 只占 {top3:.0f}%")
    return lines


def sector_tech_lines(tech, vs_own) -> List[str]:
    """板块技术面两行：板块自己的趋势 + 个股相对自己板块。

    后者才是板块技术面的产出 —— 只比「个股 vs 沪指」答不了「是个股问题还是板块问题」。
    """
    lines = []
    tech = tech or {}
    covered = int(tech.get("covered") or 0)
    if not covered:
        missing = tech.get("missing") or []
        if missing:
            lines.append(f"  板块技术面：{len(missing)} 个板块缺代理指数（成分股日线不足），暂不判板块趋势")
        return lines

    mean_rs = tech.get("rs_mean")
    mean_text = f"｜平均相对强弱 {mean_rs:+.1f}pp" if mean_rs is not None else ""
    lines.append(
        f"  板块技术面：跌破M20 {tech.get('below_ma20', 0)}/{covered}"
        f"｜转弱 {tech.get('trend_weak', 0)}"
        f"｜弱于大盘 {tech.get('rs_weak', 0)} / 强于 {tech.get('rs_strong', 0)}"
        f"{mean_text}"
    )

    vs_own = vs_own or {}
    lag = vs_own.get("lag") or []
    inline = vs_own.get("inline") or []
    lead = vs_own.get("lead") or []
    if lag or lead:
        lag_text = ""
        if lag:
            lag_text = "（" + "／".join(
                f"{it['name']}{it['vs']:+.1f}pp" for it in lag[:3]
            ) + "）"
        lines.append(
            f"  相对自己板块：跑输 {len(lag)}{lag_text}"
            f"｜跟住 {len(inline)}｜强于 {len(lead)}"
            f"    ← 跑输的才是个股自身问题"
        )
    elif inline:
        lines.append(
            f"  相对自己板块：{len(inline)} 只都跟着自己板块走，"
            f"今日弱势属板块 β，不是个股 α"
        )
    return lines


def _addition_is_actionable(recommendation) -> bool:
    return (
        recommendation is not None
        and not (isinstance(recommendation, dict) and recommendation.get("error"))
        and bool(getattr(recommendation, "selected", False))
        and _safe_float(getattr(recommendation, "shares", 0)) > 0
    )


def format_portfolio_advice_section(overview, addition=None) -> List[str]:
    """给出组合层面的操作方向，风险结论优先于单项加仓机会。"""
    overview = overview or {}
    lines = ["三、持仓整体操作建议"]
    n = int(_safe_float(overview.get("n_holdings"), 0))
    if n <= 0:
        lines.extend([
            "  整体操作：暂无实际持仓，不产生仓位调整建议；如仍有加仓推荐，先核实持仓数据。",
            "  执行顺序：配置并核实实际持仓后，再生成组合级执行顺序。",
            "  判断依据：尚未配置实际持仓。",
        ])
        return lines

    trend = overview.get("trend") or {}
    risk_break = int(_safe_float(trend.get("risk_break"), 0))
    deep_loss = int(_safe_float(trend.get("deep_loss"), 0))
    below_ma20 = int(_safe_float(trend.get("below_ma20"), 0))
    trend_weak = int(_safe_float(trend.get("trend_weak"), 0))
    half = _ceil_half(n)
    risk_count = max(risk_break, deep_loss)
    high_risk = risk_count >= half or below_ma20 >= half
    has_risk = risk_count > 0 or trend_weak >= half
    actionable_addition = _addition_is_actionable(addition)

    if high_risk:
        lines.extend([
            "  整体操作：降低总仓位，暂停新增资金；先处置破位和深套持仓，不做组合级补仓摊平。",
            "  执行顺序：先处置破位和深套持仓 → 再保留仍有交易逻辑的底仓 → 最后复核剩余仓位。",
            "  新增边界：在高风险持仓未收敛前，新增仓位保持为0；不因浮亏扩大补仓。",
        ])
        if actionable_addition:
            lines.append(
                "  加仓边界：加仓机会仅供风险持仓处置完成后复核，"
                "不改变当前暂停整体补仓的结论。"
            )
    elif has_risk:
        lines.extend([
            "  整体操作：控制现有总仓位，暂停整体补仓；按风险优先处理清单处置异常持仓。",
            "  执行顺序：风险持仓 → 其余持有观察 → 风险收敛后再复核加仓。",
            "  新增边界：新增仓位保持为0，除非风险项完成处置且组合重新站回 M20。",
        ])
        if actionable_addition:
            lines.append(
                "  加仓边界：单项加仓机会不覆盖组合风险，须待风险持仓处置完成后复核。"
            )
    elif actionable_addition:
        lines.extend([
            "  整体操作：维持现有仓位，可对通过评估的单只持仓小仓加仓；其余股票不新增。",
            "  执行顺序：先确认推荐个股的止损和失效条件 → 只执行单项加仓建议 → 再复查组合仓位。",
            "  新增边界：新增仅限报告推荐的单只和参考股数，不跨股票扩散。",
        ])
    else:
        lines.extend([
            "  整体操作：维持现有仓位，继续观察；暂不整体加仓。",
            "  执行顺序：维持现有持仓 → 等待新的入场结构和市场确认 → 再复核组合仓位。",
            "  新增边界：在出现明确入场结构前，新增仓位保持为0。",
        ])

    conclusion = str(overview.get("conclusion") or "").strip()
    lines.append(f"  判断依据：{conclusion or '暂无组合级判断依据。'}")
    if high_risk or has_risk:
        lines.append(
            "  失效条件：破位数量明显收窄、组合重新站回 M20，"
            "且市场与板块证据不再同步走弱后，重新评估是否停止降仓或恢复补仓。"
        )
    else:
        lines.append(
            "  失效条件：任一新增仓位跌破止损、组合风险数上升或市场与板块证据转弱，"
            "立即停止新增并转入风险处置。"
        )
    return lines


def format_addition_section(recommendation, evaluations=None, meta=None) -> List[str]:
    """生成独立的持仓加仓机会章节，不重新计算或强制产生候选。"""
    meta = meta or {}
    lines = ["四、加仓机会"]
    data_date = str(
        meta.get("addition_data_date")
        or meta.get("fundamental_date")
        or meta.get("quote_date")
        or "—"
    )
    if isinstance(recommendation, dict) and recommendation.get("error"):
        lines.append("  加仓评估暂不可用，不影响原风险结论")
        return lines
    if (
        recommendation is None
        or not getattr(recommendation, "selected", False)
        or getattr(recommendation, "shares", 0) <= 0
    ):
        lines.append("  当前不建议加仓")
        evaluations = list(evaluations or ([] if recommendation is None else [recommendation]))
        blockers = []
        missing = []
        for evaluation in evaluations:
            for reason in getattr(evaluation, "reasons", ()) or ():
                if reason not in blockers:
                    blockers.append(reason)
            for item in getattr(evaluation, "missing", ()) or ():
                if item not in missing:
                    missing.append(item)
        if blockers:
            lines.append("  主要否决：" + "；".join(blockers[:3]))
        if missing:
            lines.append("  待补数据：" + "；".join(missing[:3]))
        lines.append(f"  数据日期：{data_date}")
        return lines

    shares = int(getattr(recommendation, "shares", 0))
    amount = _safe_float(getattr(recommendation, "amount"), 0.0)
    price = 0.0
    if shares > 0:
        price = amount / shares
    lines.append(
        f"  {recommendation.name}（{recommendation.code}）｜综合"
        f"{recommendation.composite_score:.1f}｜技术{recommendation.technical_score:.1f}"
        f"｜基本面{recommendation.fundamental_score:.1f}"
    )
    lines.append(
        f"  参考买入：{shares}股／{_fmt_amount(amount)}元｜现价{price:.2f}"
        f"｜止损{recommendation.stop_price:.2f}"
    )
    lines.append(
        f"  仓位：{recommendation.position_pct_before:.1f}% → "
        f"{recommendation.position_pct_after:.1f}%"
    )
    lines.append(
        f"  周期分：年K{recommendation.yearly_score:.1f}｜"
        f"月K{recommendation.monthly_score:.1f}｜周K{recommendation.weekly_score:.1f}"
    )
    lines.append("  支持证据：" + "；".join(recommendation.reasons or ("暂无",)))
    if recommendation.missing:
        lines.append("  待补数据：" + "；".join(recommendation.missing[:3]))
    lines.append(f"  数据日期：{data_date}")
    lines.append(
        "  失效条件：跌破可信止损位、日线转弱、基本面转为 ALERT，"
        "或关键数据超过30天未更新"
    )
    return lines


def format_market_context_section(overview) -> List[str]:
    """把市场时点、资金流和判断复核合并为紧凑的市场背景。"""
    overview = overview or {}
    parts = []

    timing = str(overview.get("market_timing") or "").strip()
    if timing:
        parts.append(("市场", "；".join(line.strip() for line in timing.splitlines() if line.strip())))

    capital_flow = str(overview.get("capital_flow") or "").strip()
    if capital_flow:
        parts.append(("资金", "；".join(line.strip() for line in capital_flow.splitlines() if line.strip())))

    cards = format_evidence_cards(overview.get("evidence_cards") or ())
    if cards:
        parts.append(("证据", "；".join(cards)))

    review = overview.get("decision_review") or {}
    review_bits = []
    for short, key in (
        ("支持", "supporting"),
        ("反向", "counter"),
        ("缺失", "missing"),
        ("失效", "invalidators"),
    ):
        values = review.get(key) or []
        if values:
            review_bits.append(f"{short}：{'；'.join(values)}")
    if review_bits:
        parts.append(("复核", "；".join(review_bits)))

    t_filter = format_t_filter_events(overview.get("t_filter_events"))
    outcome = summarize_t_filter_outcomes(overview.get("t_filter_events"))
    t_bits = [line.strip() for line in t_filter.splitlines() if line.strip()] if t_filter else []
    if outcome:
        t_bits.append(outcome + "；仅记录事实")
    if t_bits:
        parts.append(("做T", "；".join(t_bits)))

    if not parts:
        return []
    return ["市场背景"] + [f"  {label}：{value}" for label, value in parts]


def format_daily_report(
    overview,
    results,
    pnl,
    meta,
    addition=None,
    evaluations=None,
) -> str:
    """拼装完整报告：盈亏归因、技术整体、整体建议、加仓、优先处理、逐股明细。"""
    overview = overview or {}
    results = results or []
    pnl = pnl or {}
    meta = meta or {}
    p = overview.get("pnl") or {}
    t = overview.get("trend") or {}
    vs = overview.get("vs_index") or {}
    actions = overview.get("actions") or []
    row_by_code = _row_by_code(pnl)
    n = int(overview.get("n_holdings") or 0)

    mode = meta.get("mode") or "intraday"
    mode_text = "收盘定稿" if mode == "final" else "盘中，数字会变"
    lines = [
        f"持仓信息 {meta.get('generated_at', '')}（{mode_text}）",
        f"数据日期 {meta.get('quote_date') or '—'}｜"
        f"持仓 {n} 只｜观察 {overview.get('n_watched', 0)} 只｜"
        f"缺行情 {overview.get('n_missing', 0)} 只",
    ]

    market_context = format_market_context_section(overview)
    if market_context:
        lines.extend([""])
        lines.extend(market_context)

    lines.extend(["", "一、今日盈亏归因"])

    if n == 0:
        lines.append("尚未配置实际持仓" + (
            f"（观察股 {overview.get('n_watched', 0)} 只不计盈亏）"
            if overview.get("n_watched") else ""
        ))
    else:
        today_total = _safe_float(p.get("today_total"), 0.0)
        today_pct = p.get("today_pct")
        pct_text = f"（{today_pct:+.2f}%）" if today_pct is not None else "（口径不足，不给涨跌幅）"
        base_text = (
            f"｜昨收持仓市值基数 {_fmt_amount(p.get('day_base'))} 元"
            if _safe_float(p.get("day_base"), 0.0) > 0 else ""
        )
        float_text = (
            f"{_fmt_amount(p.get('float_total'))} 元（相对成本）"
            if p.get("float_total") is not None else "口径不足（成本未填）"
        )
        lines.append(
            f"  组合：今日 {_fmt_amount(today_total)} 元{pct_text}｜"
            f"涨跌 {p.get('winners', 0)}/{p.get('losers', 0)}/{p.get('flat', 0)}"
            f"｜浮动盈亏 {float_text}{base_text}"
        )

        if vs.get("excess_pp") is not None:
            lines.append(
                f"  相对指数：沪指 {_safe_float(vs.get('index_pct')):+.2f}%｜超额 "
                f"{_safe_float(vs.get('excess_pp')):+.2f}pp（{vs.get('verdict')}）"
            )
        elif vs:
            lines.append(f"  相对指数：{vs.get('verdict')}")
        else:
            lines.append("  相对指数：无法比较（缺组合涨跌幅）")

        lines.append(
            f"  贡献：拖累前{min(3, n)} {_today_name_list(p.get('top_drag'), 3)}"
            f"｜扛住的 {_today_name_list(p.get('top_boost'), 3)}"
            f"｜浮亏金额前{min(3, n)} {_float_name_list(p.get('top_float_loss'), 3)}"
        )
        attribution = [
            line.strip() for line in _attribution_lines(overview.get("attribution") or {})
        ]
        if attribution:
            lines.append("  结构：" + "｜".join(attribution))
        sector_bits = [
            line.strip() for line in sector_lines(overview.get("sectors") or {})
        ]
        if sector_bits:
            lines.append("  板块：" + "｜".join(sector_bits))
        sector_tech = [
            line.strip() for line in sector_tech_lines(
                overview.get("sector_tech"), overview.get("vs_own_sector"),
            )
        ]
        if sector_tech:
            lines.append("  板块技术：" + "｜".join(sector_tech))

    lines.extend(["", "二、技术面整体判断"])
    if not results:
        lines.append("  暂无已分析的持仓（等待行情或分析槽位）。")
    else:
        nr = len(results)
        cross = t.get("deep_and_break", 0)
        e = t.get("entry") or {}
        lines.append(
            f"  趋势：跌破 M20/转弱 {t.get('below_ma20', 0)}｜站上 M20 {t.get('above_ma20', 0)}"
            + (f"｜未知 {t.get('ma20_unknown')}" if t.get("ma20_unknown") else "")
            + f"｜入场突破 {e.get('BREAKOUT', 0)}/回踩 {e.get('PULLBACK_M20', 0)}"
            f"/无结构 {e.get('NO_STRUCTURE', 0)}"
        )
        lines.append(
            f"  风险：深套 {t.get('deep_loss', 0)}｜破位 {t.get('risk_break', 0)}"
            f"｜深套且破位 {cross}｜量价风险 {t.get('vp_risk_tags', 0)}"
            f"｜位置 低{t.get('pos_low', 0)}/中{t.get('pos_mid', 0)}/高{t.get('pos_high', 0)}"
        )
        detail_bits = []
        if t.get("score_mean") is not None:
            worst = t.get("worst") or {}
            worst_text = (
                f"｜最低 {worst.get('name')} {_safe_float(worst.get('score')):+.0f}"
                if worst else ""
            )
            detail_bits.append(
                f"评分均值 {_safe_float(t.get('score_mean')):+.1f}"
                f"｜负分 {t.get('score_neg', 0)} 只{worst_text}"
            )
        detail_bits.append(
            f"近20日弱/强 {t.get('rs_weak', 0)}/{t.get('rs_strong', 0)}"
            f"｜大盘过滤降级 {t.get('benchmark_filtered', 0)} 只"
        )
        detail_bits.append(f"低开/冲高回落 {t.get('gap_down_or_fade', 0)} 只")
        if nr and t.get("deep_loss", 0) == 0 and t.get("risk_break", 0) == 0:
            detail_bits.append("今日无破位/深套风险")
        lines.append("  其他：" + "｜".join(detail_bits))

    cautions = overview.get("cautions") or []
    if cautions:
        lines.append("  ⚠ 风险提示：" + "；".join(cautions))

    lines.extend([""])
    lines.extend(format_portfolio_advice_section(overview, addition))
    lines.extend([""])
    lines.extend(format_addition_section(addition, evaluations, meta))
    lines.extend(["", "五、优先处理"])
    if not actions:
        lines.append("  无需处理的持仓（观察股或缺行情不进清单）。")
    else:
        # 处置框架对同一批持仓基本一致，说一遍就够 ——
        # 每行重复它就是用户抱怨的「永远同一套话术」。
        lines.append(
            "  通用规则：不加仓、不摊平；深套只复核两套条件式剧本，"
            "未通过盘中动作守卫时不直接补仓或离场。"
        )
        shown = actions[:ACTION_LIST_MAX]
        for index, item in enumerate(shown, 1):
            floating = item.get("floating")
            floating_text = (
                f"｜浮亏 {_fmt_amount(floating)}"
                f"（{_safe_float(item.get('floating_pct')):+.1f}%）"
                if floating is not None else ""
            )
            stop = item.get("stop_price")
            stop_text = f"｜止损 {_safe_float(stop):.2f}" if stop else ""
            support = _safe_float(item.get("support"), 0.0)
            # 「破支撑」只在真的破位时说 —— 有支撑位 ≠ 已破位。
            # 曾经这里无条件打「破支撑」，导致菜单写「破位 0」而明细每行都写「破支撑」。
            if support > 0:
                support_text = (
                    f"｜破支撑 {support:.2f}" if item.get("signal") == SIGNAL_BREAK
                    else f"｜支撑 {support:.2f}"
                )
            else:
                support_text = ""
            item_line = (
                f"  {index}.[{item.get('tier_label')}] {item.get('name')} "
                f"今日{_fmt_amount(item.get('today'))}{floating_text}"
                f"{support_text}{stop_text}"
            )
            notes = item.get("situation_notes") or []
            if notes:
                item_line += f"｜今日 {notes[0]}"
            lines.append(item_line)
        rest = len(actions) - len(shown)
        if rest > 0:
            lines.append(f"  （其余 {rest} 只为持有观察 / 做T机会，见第六节）")

    lines.extend(["", "六、逐股明细（按优先级）"])
    if not results:
        lines.append("  暂无已分析的持仓。")
    else:
        ordered = sorted(
            results,
            key=lambda r: priority_sort_key(r, row_by_code.get(r.get("code", "")) or {}),
        )
        for result in ordered:
            lines.extend(_format_one_position(result, row_by_code.get(result.get("code", "")) or {}))
            lines.append("")

    lines.extend([
        "口径：今日盈亏=数量×(现价−昨收)；组合涨跌幅按昨日持仓市值；评分不是上涨概率。",
        "除权日当日盈亏含除权影响。⚠️ 市场有风险，投资需谨慎，不构成投资建议。",
    ])
    return "\n".join(lines)


def _format_one_position(result, row) -> List[str]:
    """逐股明细紧凑版：持仓盈亏、风险关键位、差异化动作各一行。"""
    code = result.get("code", "")
    name = result.get("name") or code
    quantity = _safe_float(result.get("quantity"), 0.0)
    cost = _safe_float(result.get("cost"), 0.0)
    position_text = f"{name}（{code}） 持仓{quantity:.0f}股"
    if cost > 0:
        position_text += f"｜成本{cost:.2f}"

    today = row.get("today")
    today_text = (
        f"今日 {_fmt_amount(today)}"
        + (f"（{_safe_float(row.get('pct')):+.2f}%）" if row.get("pct") is not None else "")
        if today is not None else "今日 --（等待行情）"
    )
    floating = row.get("floating")
    floating_text = (
        f"｜浮亏 {_fmt_amount(floating)}"
        + (f"（{_safe_float(floating_pct_of(result)):+.1f}%）"
           if floating_pct_of(result) is not None else "")
        if floating is not None else ""
    )
    weight = row.get("weight")
    weight_text = f"｜市值权重 {weight:.0f}%" if weight else ""
    out = [f"{position_text}｜{today_text}{floating_text}{weight_text}"]

    vol = result.get("volatility") or {}
    pos = result.get("price_position") or {}
    entry_info = result.get("entry_structure") or {}
    trend = entry_info.get("trend_assessment") or {}
    evidence = trend.get("evidence") or {}
    high = entry_info.get("high_position") or {}
    strong = entry_info.get("strong_continuation") or result.get("strong_continuation") or {}
    hold = entry_info.get("continuation_hold") or result.get("continuation_hold") or {}
    permissions = result.get("trade_permissions") or entry_info.get("trade_permissions") or {}
    blockers = list(permissions.get("blockers") or [])
    trend_parts = [
        f"短期趋势：{trend.get('short_trend') or '数据不足'}｜"
        f"大趋势：{trend.get('major_trend') or '数据不足'}｜"
        f"趋势关系：{trend.get('conflict') or '数据不足'}｜"
    ]
    box_upper = _positive_float(evidence.get("box_upper"))
    box_lower = _positive_float(evidence.get("box_lower"))
    if box_upper is not None and box_lower is not None and box_upper > box_lower:
        box_parts = []
        box_state = str(evidence.get("box_state") or "")
        if box_state not in {"", "箱体震荡", "非箱体", "箱体数据不足"}:
            box_parts.append(f"箱体状态 {box_state}")
        box_parts.extend([f"箱体上沿 {box_upper:.2f}", f"箱体下沿 {box_lower:.2f}"])
        box_width = _positive_float(evidence.get("box_width_pct"))
        if box_width is not None:
            box_parts.append(f"区间 {box_width:.1f}%")
        trend_parts.append("｜".join(box_parts) + "｜")
    trend_parts.append(
        f"高位结构：{high.get('state') or '数据不足'}｜"
        f"限制：{'；'.join(blockers) if blockers else '无'}"
    )
    strong_risk = format_strong_risk(strong)
    if strong_risk:
        trend_parts.append(strong_risk + "｜")
    if hold:
        trend_parts.append(
            f"持有策略：{hold.get('state_label') or hold.get('state') or '数据不足'}｜"
            f"{format_continuation_hold_info(hold)}｜"
        )
    if entry_info.get("can_enter") or result.get("can_enter"):
        trend_parts.append("日线就绪（非交易执行）｜")
    out.append("".join(trend_parts))
    state_parts = [
        f"状态 {result.get('signal') or '—'}",
        f"评分 {_safe_float(result.get('score')):+.0f}",
        f"量比 {_safe_float(result.get('volume_ratio')):.2f}",
        f"位置 {pos.get('location') or '—'}",
    ]

    support = _safe_float(result.get("support"), 0.0)
    resistance = _safe_float(result.get("resistance"), 0.0)
    stop = result.get("entry_stop_price")
    state_parts.append(f"现价 {_safe_float(result.get('price')):.2f}")
    state_parts.append(f"支撑 {support:.2f}" if support > 0 else "支撑 —")
    state_parts.append(f"压力 {resistance:.2f}" if resistance > 0 else "压力 —")

    tags = []
    age = result.get("data_age_days")
    if age is not None and _safe_float(age) > STALE_DATA_DAYS:
        tags.append(f"⚠️日线停在{result.get('data_as_of') or '未知'}，落后{float(age):.0f}天")
    deep = is_deep(result)
    if deep:
        tags.append(f"深套(阈值{_safe_float(deep_loss_threshold_of(result)):.0f}%)")
    closes_below = _safe_float(result.get("closes_below_ma20"), 0.0)
    if result.get("trend_weak"):
        tags.append(f"跌破M20" + (f"(连{closes_below:.0f}日)" if closes_below >= 2 else ""))
    # 相对强弱不再进这里 —— 「今日情境」那行已经写了，两边都写是重复噪音
    opening = result.get("opening") or {}
    structure = str(opening.get("structure") or "")
    gap_pct = _safe_float(opening.get("gap_pct"), 0.0)
    if "低开低走" in structure:
        tags.append("低开低走")
    elif "高开低走" in structure:
        tags.append("高开低走")
    elif abs(gap_pct) >= 1.0:
        tags.append(f"跳空{gap_pct:+.2f}%")
    intraday = result.get("intraday_signal") or {}
    if intraday.get("title"):
        tags.append(f"盘中曾触发：{intraday.get('title')}")
    if tags:
        state_parts.append(f"技术面 {'｜'.join(tags)}")

    action_text = str(result.get("action_core") or result.get("action") or "").strip()
    fundamental = ""
    for keyword, label in (
        ("基本面亮红灯", "基本面亮红灯"),
        ("基本面未见恶化", "基本面未见恶化"),
        ("基本面中性", "基本面中性"),
        ("基本面暂无数据", "基本面暂无数据"),
    ):
        if keyword in action_text:
            fundamental = label
            break
    if fundamental:
        state_parts.append(fundamental)
    out.append("｜".join(state_parts))

    notes = list(result.get("situation_notes") or [])
    if not notes and "今日情境：" in action_text:
        notes = [action_text.split("今日情境：", 1)[1].strip(" 。")]
    action_summary = ""
    if action_text:
        signal = str(result.get("signal") or "")
        entry_state = str(result.get("entry_state") or "NO_STRUCTURE")
        if signal == SIGNAL_BREAK and is_deep(result):
            if fundamental == "基本面亮红灯":
                plan = "倾向剧本①"
            elif fundamental == "基本面未见恶化":
                plan = "倾向剧本②"
            else:
                plan = "基本面中性，按风险承受能力选择剧本"
            action_summary = (
                f"{plan}｜逻辑坏：反弹至压力 {resistance:.2f} 分批离场；"
                f"逻辑在：站回支撑 {support:.2f} 后小仓补，创新低取消"
            )
        elif signal == SIGNAL_BREAK:
            high_exit = (
                not is_deep(result)
                and str(
                    ((result.get("price_position") or {}).get("location_key") or "")
                ).upper() == "HIGH"
                and bool(result.get("trend_weak"))
            )
            action_summary = (
                "高位非深套趋势走坏，确认破位后清仓离场，执行可分批"
                if high_exit
                else f"跌破支撑 {support:.2f}，不能收回则分批减仓；站回企稳后再看"
            )
        elif signal == SIGNAL_T_SELL:
            action_summary = "乖离偏大，可分批减出部分仓位，回踩 M20 附近再接回"
        elif result.get("trend_weak"):
            action_summary = (
                f"止损 {_safe_float(stop):.2f}；重新站回 M20 并企稳前不恢复加仓"
                if stop else "重新站回 M20 并企稳前不恢复加仓"
            )
        elif entry_state == "BREAKOUT":
            action_summary = (
                f"持有观察；跌回失效位 {_safe_float(stop):.2f} 离场"
                if stop else "持有观察，跌破突破位离场"
            )
        else:
            action_summary = "持有观察，等待 M20、前高和量能确认"
    if action_summary:
        action_kind = (
            "risk_exit"
            if signal == SIGNAL_BREAK
            else "t_sell"
            if signal == SIGNAL_T_SELL
            else "buy"
            if entry_state == "PULLBACK_M20"
            else "hold"
        )
        action_summary = guard_actionable_copy(
            action_kind,
            action_summary,
            trade_date=result.get("trade_date") or result.get("data_as_of"),
        ).text
    action_parts = []
    if action_summary:
        action_parts.append(f"动作：{action_summary}")
    action_parts.append(f"失效参考 {_safe_float(stop):.2f}" if stop else "失效参考 —")
    if notes:
        action_parts.append("今日：" + "；".join(notes[:2]))
    out.append("｜".join(action_parts))
    return out


# ── 渲染：菜单摘要 ──────────────────────────────────────

def menu_title(overview, meta) -> str:
    """父项标题：全菜单最重要的 20 个字符 = 今天亏了多少、涨跌幅多少。"""
    overview = overview or {}
    p = overview.get("pnl") or {}
    n = int(overview.get("n_holdings") or 0)
    if n <= 0:
        return "📦 持仓信息  （未配置持仓）"
    today_pct = p.get("today_pct")
    total = _fmt_amount(p.get("today_total"))
    pct_text = f"｜{today_pct:+.2f}%" if today_pct is not None else ""
    return f"📦 持仓信息  {total}{pct_text}"


def format_menu_summary(overview, meta) -> List[str]:
    """菜单信息行（不含「打开完整报告」与宽度填充）。

    原则：菜单行只回答「今天亏了多少 / 比大盘如何 / 最危险的是谁」；
    点开才看得懂的分布统计一律不进菜单。
    """
    overview = overview or {}
    meta = meta or {}
    p = overview.get("pnl") or {}
    t = overview.get("trend") or {}
    vs = overview.get("vs_index") or {}
    actions = overview.get("actions") or []
    n = int(overview.get("n_holdings") or 0)

    if n <= 0:
        return ["尚未配置实际持仓"]

    lines = []
    mode = "收盘定稿" if (meta.get("mode") == "final") else "盘中"
    stamp = meta.get("quote_date") or meta.get("generated_at") or ""
    if vs.get("excess_pp") is not None:
        lines.append(
            f"{mode}｜沪指{_safe_float(vs.get('index_pct')):+.2f}%"
            f"｜超额{_safe_float(vs.get('excess_pp')):+.2f}pp"
        )
    else:
        lines.append(f"{mode}｜{stamp}")

    risk_break = int(t.get("risk_break") or 0)
    deep_loss = int(t.get("deep_loss") or 0)
    cross = int(t.get("deep_and_break") or 0)
    if risk_break == 0 and deep_loss == 0:
        lines.append("无破位/深套风险，维持观察")
    else:
        lines.append(
            f"⚠ 破位 {risk_break}｜深套 {deep_loss}"
            + (f"（交叉 {cross}）" if cross else "")
            + f"｜浮亏 {_fmt_wan(p.get('float_total') or 0)}"
        )

    for index, item in enumerate(actions[:2]):
        prefix = "先处理" if index == 0 else "其次"
        floating = item.get("floating")
        loss_text = f"·浮亏{_fmt_wan(floating)}" if floating is not None else ""
        lines.append(f"{prefix}：{item.get('name')} {item.get('tier_label')}{loss_text}")

    drag = p.get("top_drag") or []
    if drag:
        parts = [
            f"{(it.get('name') or '')[:4]}{_fmt_amount(it.get('today'))}"
            for it in drag[:3]
        ]
        lines.append("拖累：" + "/".join(parts))

    below = int(t.get("below_ma20") or 0)
    if below and below == n:
        filtered = int(t.get("benchmark_filtered") or 0)
        tail = f"｜大盘过滤降级 {filtered} 只" if filtered else ""
        lines.append(f"{below}/{n} 跌破M20{tail}")

    return lines[:MENU_SUMMARY_MAX_LINES]


# ── 收盘定稿 guard ──────────────────────────────────────

def portfolio_fingerprint(positions) -> str:
    """持仓指纹：数量/成本变了就算持仓变更。

    必须 round 浮点 —— json 读写微差会让指纹永远对不上，
    于是每天重复定稿（2026-10-12 设计评审时点名的坑）。
    """
    items = []
    for position in positions or []:
        items.append((
            str(position.get("code", "") or ""),
            round(_safe_float(position.get("quantity"), 0.0), 0),
            round(_safe_float(position.get("cost"), 0.0), 2),
        ))
    items.sort()
    payload = json.dumps(items, ensure_ascii=False, separators=(",", ":"))
    return "sha1:" + hashlib.sha1(payload.encode("utf-8")).hexdigest()


def decide_report_mode(now, quote_date_str, state, fingerprint) -> str:
    """该跳过 / 出盘中版 / 出定稿版。返回 "skip" | "intraday" | "finalize"。

    交易日判据是**行情日期**而不是 weekday：_is_trading_hours 与
    _intraday_indicator_slot 只判 weekday()，节假日照样返回槽位，
    拿上一交易日的价去「定稿今天」是错的。
    """
    today = now.strftime("%Y-%m-%d")
    if not quote_date_str or quote_date_str != today:
        return "skip"                     # 节假日 / 旧数据，不许定稿
    if now.hour * 60 + now.minute < FINALIZE_MINUTE:
        return "intraday"
    state = state or {}
    if (state.get("finalized_date") == today
            and state.get("portfolio_fingerprint") == fingerprint):
        return "skip"                     # 定稿已锁，不再更新
    return "finalize"                     # 当日首次，或持仓变更
