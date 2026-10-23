#!/usr/bin/env python3
"""统一过滤盘中买卖/做T文案，避免未经确认的动作指令。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any, Optional

# 仓位小的口径：持仓成本占组合总成本不超过这个百分比（约等于「买的也不多」）。
SMALL_POSITION_SHARE_PCT = 10.0


def position_cost_share(portfolio: Any, quantity: Any, cost: Any) -> Optional[float]:
    """持仓成本占组合总成本的百分比；算不出返回 None。

    不依赖 total_capital（用户常不填）和现价（组合里其他票拿不到），
    成本占比对「我买的也不多」这个直觉足够贴近。
    """
    try:
        qty = float(quantity or 0)
        cst = float(cost or 0)
    except (TypeError, ValueError):
        return None
    if qty <= 0 or cst <= 0:
        return None
    total = 0.0
    for item in ((portfolio or {}).get("positions") or []):
        try:
            other_qty = float(item.get("quantity") or 0)
            other_cost = float(item.get("cost") or 0)
        except (TypeError, ValueError):
            continue
        if other_qty > 0 and other_cost > 0:
            total += other_qty * other_cost
    if total <= 0:
        return None
    return qty * cst / total * 100.0


def allows_exit_directive(
    fundamental_level: Any,
    price_location: Any,
    position_share_pct: Optional[float] = None,
) -> bool:
    """「直接撤出」话术（清仓离场 / 不保留观察仓）的门槛：三者占一才放行。

    - 基本面亮红灯（ALERT），或
    - 价格处高位（HIGH），或
    - 仓位不小（成本占比 > 10%）。

    基本面未见恶化 + 价格不高 + 仓位小的持仓只提示风险事实，不给撤出指令
    （2026-10-22 招商证券：基本面好、位置低位、小仓位却被「不保留确定观察仓」
    直接劝退）。基本面未分析、仓位算不出按不允许处理——缺证据不喊撤出。
    """
    if str(fundamental_level or "").upper() == "ALERT":
        return True
    if str(price_location or "").upper() == "HIGH":
        return True
    return position_share_pct is not None and position_share_pct > SMALL_POSITION_SHARE_PCT


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


def _volume_value(volume_context: Any, name: str, default=None):
    if volume_context is None:
        return default
    if isinstance(volume_context, Mapping):
        return volume_context.get(name, default)
    return getattr(volume_context, name, default)


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
    max_quantity: int = 0,
    volume_context: Any = None,
) -> GuardedCopy:
    kind = action_kind_for_signal(signal_type, text)
    if kind == "risk_exit" and evidence is not None:
        critical = bool(
            _evidence_value(evidence, "risk_level", "NORMAL") == "CRITICAL"
            and _evidence_value(evidence, "risk_confirmations", 0) >= 3
        )
        if not critical:
            kind = "risk_reduce"
    if kind == "hold":
        return GuardedCopy("observation", str(text or "仅观察：当前没有可执行动作。"), reason="观察信号")
    return guard_actionable_copy(
        kind,
        text,
        eligibility=eligibility,
        evidence=evidence,
        trade_date=trade_date,
        invalidation_price=invalidation_price,
        max_quantity=max_quantity,
        volume_context=volume_context,
    )


def guard_actionable_copy(
    kind: str,
    text: str,
    eligibility: Any = None,
    evidence: Any = None,
    trade_date: Optional[str] = None,
    invalidation_price: float = 0.0,
    max_quantity: int = 0,
    volume_context: Any = None,
) -> GuardedCopy:
    kind = str(kind or "").lower().strip()
    text = str(text or "").strip()
    if kind in {"buy", "add"}:
        if not _valid_trade_date(trade_date):
            return GuardedCopy("observation", "仅观察：行情日期未确认，不执行买入或补仓。", reason="缺少行情日期")
        if "仅观察" in text:
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
        if bool(_volume_value(volume_context, "blowoff_risk", False)):
            return GuardedCopy(
                "observation",
                "仅观察：上涨放量出现冲高回落迹象，量价证据冲突，不执行买入或补仓。",
                reason="冲高回落风险",
            )
        if (
            _volume_value(volume_context, "stock_volume_confirming") is not True
            or _volume_value(volume_context, "market_expanding") is not True
        ):
            return GuardedCopy(
                "observation",
                "仅观察：个股或两市量能证据不足，不执行买入或补仓。",
                reason="量能证据不足",
            )
        quantity = (int(_eligibility_value(eligibility, "buy_capacity_quantity")) // 100) * 100
        if quantity < 100:
            return GuardedCopy("observation", "仅观察：仓位边界不允许至少一手，不执行买入或补仓。", reason="仓位不足")
        return GuardedCopy("actionable", text, quantity, float(invalidation_price), "买入条件已确认")

    ledger_consistent = bool(_eligibility_value(eligibility, "ledger_consistent", True))
    sellable = int(_eligibility_value(eligibility, "sellable_quantity"))
    if not ledger_consistent:
        return GuardedCopy(
            "observation",
            "仅观察：持仓账本数量不一致，暂不生成可卖股数。",
            reason="持仓账本不一致",
        )
    if kind in {"risk_exit", "risk_reduce", "stop", "break_support"}:
        risk_confirmations = int(_evidence_value(evidence, "risk_confirmations", 0))
        critical = bool(_evidence_value(evidence, "risk_level", "NORMAL") == "CRITICAL")
        risk_confirmations_ready = (
            risk_confirmations >= 3
            if bool(_evidence_value(evidence, "risk_level", "NORMAL") == "NORMAL")
            else risk_confirmations >= 2
        )
        risk_ready = bool(
            _valid_trade_date(trade_date)
            and _evidence_value(evidence, "risk_complete")
            and _evidence_value(evidence, "risk_confirmed")
            and risk_confirmations_ready
        )
        full_exit_ready = bool(
            kind == "risk_exit" and critical and risk_confirmations >= 3
        )
        if risk_ready and sellable > 0 and full_exit_ready:
            quantity = int(max_quantity or sellable)
            return GuardedCopy("actionable", text, min(quantity, sellable), 0.0, "历史结构失效且风险卖出条件已确认")
        if risk_ready and sellable > 0 and kind == "risk_reduce":
            quantity = int(max_quantity or max(100, ((sellable // 3) // 100) * 100))
            return GuardedCopy("actionable", text, min(quantity, sellable), 0.0, "连续风险确认，首批分批减仓")
        if risk_ready:
            return GuardedCopy(
                "next_day",
                "风险事实：风险条件已确认，但当日无可卖批次；受A股T+1限制，下一交易日复核处置。",
                reason="T+1限制",
            )
        return GuardedCopy(
            "risk_fact",
            "风险观察：等待连续两根新鲜15分钟K线及历史风险级别确认，今日不作卖出处置。",
            reason="风险证据不足",
        )

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
