#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""基本面健康分级：把 FundamentalResult 折成破位文案用得上的一句「倾向」。

程序判断得了的是**财务与公告可见面**——业绩变脸、现金流质量、商誉等资产干净度、
立案处罚、筹码恶化；判断不了行业景气、订单、竞争格局这类「生意逻辑」。所以这里
只定两套剧本的优先级（亮红灯倾向离场、未见恶化倾向补仓做 T），最终选择留给用户。

缓存优先：提醒只读缓存、从不在提醒时拉接口。基本面变化慢，约半个月有效；
工具页 / AI 工具跑过的分析与监控补拉写同一份缓存。

来源：Android 版 FundamentalHealth.kt
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, List, Optional

# 业绩变脸阈值：最近两季营收、净利同比都低于这个百分比算双降。
DOWNTURN_YOY_PCT = -20.0

# 评分到这个分且无 ALERT 风险，才敢说「未见恶化」。
SOUND_MIN_SCORE = 70

# 评分低于这个分本身就构成恶化信号。
WEAK_SCORE = 55

# 最多带进文案的触发项条数，多了刷屏。
MAX_REASONS = 3

# 健康度缓存有效期：基本面变化慢，十天半个月分析下来都差不多，有缓存就用缓存。
CACHE_TTL_MS = 14 * 24 * 60 * 60 * 1000

# 拉取失败后的重试间隔（只对没有缓存的代码生效）。
RETRY_AFTER_FAILURE_MS = 4 * 60 * 60 * 1000


@dataclass
class FundamentalHealth:
    """一次基本面健康判断。level ∈ {"ALERT", "NEUTRAL", "SOUND"}。"""
    level: str
    score: int
    verdict: str
    reasons: List[str] = field(default_factory=list)
    analyzed_at: str = ""


@dataclass
class HealthSnapshot:
    """缓存快照：fetched_at_ms 用来判断要不要按需补拉。"""
    health: FundamentalHealth
    fetched_at_ms: int


# code -> HealthSnapshot；工具页 / AI / 监控补拉共用一份。
_cache: dict[str, HealthSnapshot] = {}
_cache_lock = threading.Lock()
_last_attempt_ms: dict[str, int] = {}


def grade(
    score: int,
    verdict: str,
    risks: Optional[List[Any]] = None,
    quarters: Optional[List[Any]] = None,
    notes: Optional[List[str]] = None,
    analyzed_at: str = "",
) -> FundamentalHealth:
    """把评分 / 风险项 / 单季趋势 / 口径提示折成三级健康度。

    risks 元素需带 .level（"ALERT"/"WATCH"/"GOOD"）与 .text，或为 (level, text) 元组；
    quarters 元素需带 .revenue_yoy / .net_profit_yoy 属性，或为 dict。
    """
    risks = risks or []
    quarters = quarters or []
    notes = notes or []
    reasons: List[str] = []
    red = False

    if score < WEAK_SCORE:
        red = True
        reasons.append(f"评分 {score}（{verdict}）")

    def _risk_level(item) -> str:
        if isinstance(item, tuple):
            return str(item[0])
        level = getattr(item, "level", "")
        # Enum 成员取 .name（"ALERT"），字符串直接用。
        return str(getattr(level, "name", level))

    def _risk_text(item) -> str:
        if isinstance(item, tuple):
            return str(item[1])
        return str(getattr(item, "text", ""))

    alerts = [item for item in risks if _risk_level(item) == "ALERT"]
    if alerts:
        red = True
        for item in alerts:
            reasons.append(_risk_text(item))

    def _yoy(quarter, key: str) -> Optional[float]:
        if isinstance(quarter, dict):
            value = quarter.get(key)
        else:
            value = getattr(quarter, key, None)
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def _double_decline(quarter) -> bool:
        revenue = _yoy(quarter, "revenue_yoy")
        # A 版字段名是 profit_yoy，Android 版是 net_profit_yoy，两边都认。
        profit = _yoy(quarter, "profit_yoy")
        if profit is None:
            profit = _yoy(quarter, "net_profit_yoy")
        return (
            revenue is not None and profit is not None
            and revenue <= DOWNTURN_YOY_PCT and profit <= DOWNTURN_YOY_PCT
        )

    last_two = quarters[-2:]
    downturn = len(last_two) == 2 and all(_double_decline(item) for item in last_two)
    if downturn:
        red = True
        reasons.append("最近两季营收、净利同比双双下滑超 20%（业绩变脸迹象）")
    # 最新一季双降但还没连续两季：够不上变脸，但也不好意思说「未见恶化」。
    single_downturn = not downturn and bool(quarters) and _double_decline(quarters[-1])
    if single_downturn:
        reasons.append("最近一季营收、净利同比双降")

    # 周期股在盈利高点的低估值是陷阱；口径提示在场时，再好的评分也不说「未见恶化」。
    cyclical = any("周期" in str(note) for note in notes)
    if red:
        level = "ALERT"
    elif single_downturn or cyclical:
        level = "NEUTRAL"
    elif score >= SOUND_MIN_SCORE and not alerts:
        level = "SOUND"
    else:
        level = "NEUTRAL"
    if cyclical and not any("周期" in reason for reason in reasons):
        reasons.append("存在周期口径提示")
    if not reasons:
        reasons.append(f"评分 {score}（{verdict}）")
    return FundamentalHealth(
        level=level,
        score=score,
        verdict=verdict,
        reasons=reasons[:MAX_REASONS],
        analyzed_at=analyzed_at,
    )


def grade_result(result: Any, analyzed_at: str = "") -> FundamentalHealth:
    """对 FundamentalResult 直接分级。"""
    return grade(
        score=int(getattr(result, "score", 0)),
        verdict=str(getattr(result, "verdict", "")),
        risks=list(getattr(result, "risks", []) or []),
        quarters=list(getattr(result, "quarters", []) or []),
        notes=list(getattr(result, "notes", []) or []),
        analyzed_at=analyzed_at,
    )


def tendency(health: Optional[FundamentalHealth], deep: bool) -> Optional[str]:
    """基本面倾向一句话。深套时给两套剧本标优先级（剧本定义在破位文案里）；
    浅亏时补在减仓建议后面。没有分析结果时也要交代一句——破位恰是最需要基本面
    判断的时刻，沉默会让人以为「没提示 = 没问题」。
    """
    if health is None:
        return (
            "基本面暂无数据：先按风险承受能力决定减仓或等待企稳，"
            "补充基本面后再判断。"
            if deep
            else "基本面未分析（可先跑一次个股基本面分析）。"
        )
    why = "；".join(health.reasons)
    if deep and health.level == "ALERT":
        return (
            f"基本面亮红灯（{why}）：倾向剧本①；程序看不到行业景气与订单，"
            "若你判断逻辑未变再考虑剧本②。"
        )
    if deep and health.level == "SOUND":
        return f"基本面未见恶化（{why}）：倾向剧本②，破位处仍不接飞刀。"
    if deep:
        return (
            f"基本面中性（{why}）：减仓或等待企稳都需要结合仓位和"
            "风险承受能力，程序不替你做最终决定。"
        )
    if health.level == "ALERT":
        return f"基本面亮红灯（{why}）：减仓后不急接回。"
    if health.level == "SOUND":
        return f"基本面未见恶化（{why}）：减仓控风险即可，企稳后可再接回。"
    return f"基本面中性（{why}）。"


# ── 缓存（提醒只读缓存，从不在提醒时拉接口） ─────────────────────────────

def publish(code: str, health: FundamentalHealth, fetched_at_ms: Optional[int] = None) -> None:
    """写入一次基本面健康判断。工具页 / AI / 监控补拉都走这里。"""
    with _cache_lock:
        _cache[code] = HealthSnapshot(health, int(fetched_at_ms or time.time() * 1000))


def snapshot(code: str) -> Optional[HealthSnapshot]:
    with _cache_lock:
        return _cache.get(code)


def health_of(code: str) -> Optional[FundamentalHealth]:
    snap = snapshot(code)
    return snap.health if snap else None


def needs_fetch(code: str, now_ms: Optional[int] = None) -> bool:
    """要不要后台补拉：有新鲜缓存就用缓存；没缓存/已过期才拉，失败后节流。"""
    now_ms = int(now_ms or time.time() * 1000)
    with _cache_lock:
        snap = _cache.get(code)
        if snap is not None and now_ms - snap.fetched_at_ms < CACHE_TTL_MS:
            return False
        return now_ms - _last_attempt_ms.get(code, 0) >= RETRY_AFTER_FAILURE_MS


def mark_attempt(code: str, now_ms: Optional[int] = None) -> None:
    with _cache_lock:
        _last_attempt_ms[code] = int(now_ms or time.time() * 1000)


def clear_cache() -> None:
    """测试用：清空缓存与尝试记录。"""
    with _cache_lock:
        _cache.clear()
        _last_attempt_ms.clear()
