#!/usr/bin/env python3
"""统一过滤盘中买卖/做T文案，避免未经确认的动作指令。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Optional


@dataclass(frozen=True)
class GuardedCopy:
    level: str
    text: str
    max_quantity: int = 0
    invalidation_price: float = 0.0
    reason: str = ""


def _valid_trade_date(value: Optional[str]) -> bool:
    try:
        date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return False
    return True


def _evidence_value(evidence: Any, name: str, default=False):
    return getattr(evidence, name, default) if evidence is not None else default


def _eligibility_value(eligibility: Any, name: str, default=0):
    return getattr(eligibility, name, default) if eligibility is not None else default


def action_kind_for_signal(signal_type: Any, text: Any = "") -> str:
    signal_text = f"{signal_type or ''} {text or ''}"
    if any(phrase in signal_text for phrase in ("跌破支撑", "破位", "离场风险", "风险离场")):
        return "risk_exit"
    if any(phrase in signal_text for phrase in ("冲高做T", "做T减仓", "做 T")):
        return "t_sell"
    if any(phrase in signal_text for phrase in ("分批卖出", "分批减仓", "清仓离场", "离场窗口", "减仓控风险")):
        return "normal_exit"
    if any(phrase in signal_text for phrase in ("小仓补", "补仓", "接回", "站回支撑", "买入观察", "回踩上行 M20")):
        return "buy"
    return "hold"


def guard_intraday_signal(
    signal_type: Any,
    text: Any,
    eligibility: Any = None,
    evidence: Any = None,
    trade_date: Optional[str] = None,
    invalidation_price: float = 0.0,
) -> GuardedCopy:
    kind = action_kind_for_signal(signal_type, text)
    if kind == "hold":
        return GuardedCopy("observation", str(text or "仅观察：当前没有可执行动作。"), reason="观察信号")
    return guard_actionable_copy(
        kind,
        text,
        eligibility=eligibility,
        evidence=evidence,
        trade_date=trade_date,
        invalidation_price=invalidation_price,
    )


def guard_actionable_copy(
    kind: str,
    text: str,
    eligibility: Any = None,
    evidence: Any = None,
    trade_date: Optional[str] = None,
    invalidation_price: float = 0.0,
) -> GuardedCopy:
    kind = str(kind or "").lower().strip()
    text = str(text or "").strip()
    if kind in {"buy", "add"}:
        if not _valid_trade_date(trade_date):
            return GuardedCopy("observation", "仅观察：行情日期未确认，不执行买入或补仓。", reason="缺少行情日期")
        if "仅观察" in text and any(
            marker in text for marker in ("趋势", "高位", "市场", "板块", "15/30")
        ):
            return GuardedCopy("observation", text, reason="交易门控限制")
        if (
            not _evidence_value(evidence, "complete")
            or _evidence_value(evidence, "m15_confirmations", 0) < 2
            or not _evidence_value(evidence, "m30_aligned")
            or not _evidence_value(evidence, "clearance_confirmed")
            or _evidence_value(evidence, "last_bar_rejected")
            or float(invalidation_price or 0.0) <= 0
        ):
            return GuardedCopy("observation", "仅观察：15/30分钟条件未达到稳健买点，不执行买入或补仓。", reason="买入证据不足")
        quantity = (int(_eligibility_value(eligibility, "buy_capacity_quantity")) // 100) * 100
        if quantity < 100:
            return GuardedCopy("observation", "仅观察：仓位边界不允许至少一手，不执行买入或补仓。", reason="仓位不足")
        return GuardedCopy("actionable", text, quantity, float(invalidation_price), "买入条件已确认")

    sellable = int(_eligibility_value(eligibility, "sellable_quantity"))
    if kind in {"risk_exit", "stop", "break_support"}:
        risk_ready = bool(
            _valid_trade_date(trade_date)
            and _evidence_value(evidence, "risk_complete")
            and _evidence_value(evidence, "risk_confirmed")
        )
        if risk_ready and sellable > 0:
            return GuardedCopy("actionable", text, sellable, 0.0, "风险卖出条件已确认")
        if risk_ready:
            return GuardedCopy(
                "next_day",
                "风险事实：风险条件已确认，但当日无可卖批次；受A股T+1限制，下一交易日复核处置。",
                reason="T+1限制",
            )
        return GuardedCopy("risk_fact", "风险观察：风险条件尚未经新鲜15分钟K线确认，今日不作卖出处置。", reason="风险证据不足")

    if kind in {"normal_exit", "exit", "resistance", "t_sell", "do_t"}:
        if not _valid_trade_date(trade_date):
            return GuardedCopy("observation", "仅观察：行情日期未确认，不执行卖出、减仓或波段操作。", reason="缺少行情日期")
        if (
            not _evidence_value(evidence, "complete")
            or _evidence_value(evidence, "m15_confirmations", 0) < 1
            or not _evidence_value(evidence, "m30_aligned")
            or sellable <= 0
        ):
            label = "不做T" if kind in {"t_sell", "do_t"} else "不执行卖出"
            return GuardedCopy("observation", f"仅观察：双周期条件或当日可卖批次不足，{label}、不执行减仓或波段操作。", reason="卖点证据不足")
        return GuardedCopy("actionable", text, sellable, 0.0, "双周期卖点已确认")

    return GuardedCopy("observation", "仅观察：当前没有可执行动作。", reason="未知动作类型")
