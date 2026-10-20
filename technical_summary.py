#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""技术分析结构化摘要。

从 analyze_history() 的结果中提取关键指标，生成可读的摘要文本。
包含评分、趋势、量价、动量、支撑压力、操作建议六个维度。

来源：Android 版 TechnicalSummary.kt
"""

from dataclasses import dataclass
from typing import Dict, Optional

from volume_price import VolumePrice, tag as vp_tag
from price_position import PriceLocation


@dataclass
class TechnicalSummary:
    """技术分析结构化摘要。"""

    score: int
    score_label: str   # "偏强" / "偏弱" / "中性"
    trend: str         # 趋势描述
    volume: str        # 量价描述
    momentum: str      # 动量指标
    levels: str        # 支撑压力位
    action: str        # 操作建议
    entry: str = ""    # 入场结构状态
    divergence: str = ""   # 量价背离结论（OBV 顶/底背离）

    @classmethod
    def from_result(
        cls,
        result: Dict,
        price_context: Optional[Dict] = None,
        volume_ratio: Optional[float] = None,
    ) -> "TechnicalSummary":
        """从 analyze_history() 的结果构建摘要。

        Args:
            result: analyze_history() 返回的字典，包含 price, ma5, ma10,
                    ma20, ma60, macd, rsi, kdj, support, resistance, score,
                    action, volume_ratio, price_change 等字段。
            price_context: 位置上下文字典，可选键：
                - 'location': 'LOW' / 'MID' / 'HIGH'
                - 'rebound_from_low': float
            volume_ratio: 量比（可选，优先使用 result 中的值）。

        Returns:
            TechnicalSummary 实例。
        """
        # ── 提取基础数据 ──
        price = result.get("price", 0)
        ma5 = result.get("ma5", 0)
        ma10 = result.get("ma10", 0)
        ma20 = result.get("ma20", 0)
        ma60 = result.get("ma60", 0)

        macd = result.get("macd", {})
        dif = macd.get("dif", 0)
        dea = macd.get("dea", 0)

        rsi = result.get("rsi", 0)
        kdj = result.get("kdj", {})
        k = kdj.get("k", 0)
        d = kdj.get("d", 0)

        volatility = result.get("volatility", {})
        atr_v = volatility.get("atr", 0)
        atr_pct = volatility.get("atr_pct", 0)

        support = result.get("support", 0)
        resistance = result.get("resistance", 0)
        score = result.get("score", 0)
        action = result.get("action", "")

        # 量比：优先使用参数，其次使用 result 中的值
        vr = volume_ratio if volume_ratio is not None else result.get("volume_ratio")
        change_pct = result.get("price_change", 0)
        if change_pct is None:
            change_pct = 0

        # ── 趋势描述 ──
        if price > ma20 > ma60:
            trend = "中期趋势偏强"
        elif price < ma20 < ma60:
            trend = "中期趋势偏弱"
        else:
            trend = "中期趋势还没确认"

        if ma5 > ma10 > ma20:
            trend += "，短期均线多头排列"
        elif ma5 < ma10 < ma20:
            trend += "，短期均线空头排列"
        else:
            trend += "，短期均线缠绕"

        # ── 量价描述 ──
        if vr is not None:
            volume = f"量比 {vr:.2f} 倍"
        else:
            volume = "量比暂缺"

        signal = VolumePrice.of(vr, change_pct, price_context)
        signal_tag = vp_tag(signal)
        if signal_tag:
            volume += f" {signal_tag}"

        # ── 动量指标 ──
        macd_state = "多头" if dif > dea else "空头"
        momentum = f"MACD {macd_state} | RSI {rsi:.1f} | KDJ K {k:.1f} / D {d:.1f}"

        # ── 支撑压力 ──
        atr_str = f"{atr_v:.2f}" if atr_v else "N/A"
        atr_pct_str = f"{atr_pct:.2f}" if atr_pct else "N/A"
        ma20_str = f"{ma20:.2f}" if ma20 else "N/A"
        levels = (
            f"支撑 {support:.2f} | 压力 {resistance:.2f} "
            f"| MA20 {ma20_str} | ATR {atr_str} ({atr_pct_str}%)"
        )

        # ── 评分标签 ──
        if score >= 4:
            score_label = "偏强"
        elif score <= -4:
            score_label = "偏弱"
        else:
            score_label = "中性"

        # ── 入场结构 ──
        entry_info = result.get("entry_structure") or {}
        entry = entry_info.get("state_label", "暂未形成有效入场结构")

        # ── 量价背离（OBV 顶/底背离）──
        obv_div = result.get("obv_divergence") or {}
        divergence = obv_div.get("short") or ""

        return cls(
            score=score,
            score_label=score_label,
            trend=trend,
            volume=volume,
            momentum=momentum,
            levels=levels,
            action=action,
            entry=entry,
            divergence=divergence,
        )


def format_summary(summary: TechnicalSummary) -> str:
    """将 TechnicalSummary 格式化为多行文本。

    Args:
        summary: TechnicalSummary 实例。

    Returns:
        格式化的摘要字符串。
    """
    return (
        f"📊 技术分析摘要\n"
        f"评分：{summary.score}分（{summary.score_label}）\n"
        f"趋势：{summary.trend}\n"
        f"量价：{summary.volume}\n"
        + (f"{summary.divergence}\n" if summary.divergence else "")
        + f"动量：{summary.momentum}\n"
        f"位置：{summary.levels}\n"
        f"入场结构：{summary.entry}\n"
        f"建议：{summary.action}"
    )
