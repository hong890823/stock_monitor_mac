#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""第一波之后的持有延续、部分兑现和二波守候判断。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
import math
from typing import Any, Optional


class ContinuationHoldState(str, Enum):
    MISSING = "证据不足"
    HOLD_CONTINUATION = "持有延续"
    PROFIT_PROTECTION = "部分兑现保护"
    SECOND_WAVE_WATCH = "二波守候"
    SECOND_WAVE_CONFIRMED = "二波确认"
    BREAK_DEFENSE = "破位防守"
    DISTRIBUTION_RISK = "派发风险"


@dataclass(frozen=True)
class ContinuationHoldInput:
    strong_state: str
    high_state: str
    risk_line: Optional[str] = None
    risk_price: Optional[float] = None
    bias_pct: Optional[float] = None
    volume_ratio: Optional[float] = None
    first_wave: bool = False
    timeframe_ok: Optional[bool] = None


@dataclass(frozen=True)
class ContinuationHoldAssessment:
    state: ContinuationHoldState
    can_trim: bool
    keep_observation_position: bool
    candidate_eligible: bool
    risk_line: Optional[str]
    risk_price: Optional[float]
    reasons: tuple[str, ...]
    blockers: tuple[str, ...]


_STRONG_ALIASES = {
    "MISSING": "MISSING",
    "数据不足": "MISSING",
    "NOT_STRONG": "NOT_STRONG",
    "非强势": "NOT_STRONG",
    "STRONG_CONTINUATION": "STRONG_CONTINUATION",
    "强势延续": "STRONG_CONTINUATION",
    "EXTENDED_CONTINUATION": "EXTENDED_CONTINUATION",
    "延伸观察": "EXTENDED_CONTINUATION",
    "M20_BREAK_RISK": "M20_BREAK_RISK",
    "M20破位风险": "M20_BREAK_RISK",
    "M5_BREAK_RISK": "M5_BREAK_RISK",
    "M5破位风险": "M5_BREAK_RISK",
    "REJECTED_BREAKOUT": "REJECTED_BREAKOUT",
    "突破失败": "REJECTED_BREAKOUT",
    "DISTRIBUTION_RISK": "DISTRIBUTION_RISK",
    "派发风险": "DISTRIBUTION_RISK",
}

_HIGH_ALIASES = {
    "MISSING": "MISSING",
    "数据不足": "MISSING",
    "NOT_HIGH": "NOT_HIGH",
    "非高位": "NOT_HIGH",
    "HIGH_PENDING": "HIGH_PENDING",
    "高位待定": "HIGH_PENDING",
    "DISTRIBUTION_RISK": "DISTRIBUTION_RISK",
    "高位派发风险": "DISTRIBUTION_RISK",
    "HIGH_CONSOLIDATION": "HIGH_CONSOLIDATION",
    "高位整理蓄势": "HIGH_CONSOLIDATION",
    "SECOND_WAVE_CANDIDATE": "SECOND_WAVE_CANDIDATE",
    "二波候选": "SECOND_WAVE_CANDIDATE",
    "SECOND_WAVE_CONFIRMED": "SECOND_WAVE_CONFIRMED",
    "二波确认": "SECOND_WAVE_CONFIRMED",
}


def _finite(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def assess_continuation_hold(value: ContinuationHoldInput) -> ContinuationHoldAssessment:
    strong = _STRONG_ALIASES.get(str(value.strong_state or "").upper(), "MISSING")
    high = _HIGH_ALIASES.get(str(value.high_state or "").upper(), "MISSING")
    risk_line = value.risk_line if value.risk_line in {"MA5", "M20"} else None
    risk_price = _finite(value.risk_price)

    if strong == "MISSING":
        return ContinuationHoldAssessment(
            ContinuationHoldState.MISSING, False, False, False,
            risk_line, risk_price, (), ("强势延续证据不足，不输出持有或兑现结论",),
        )
    if strong == "DISTRIBUTION_RISK" or high == "DISTRIBUTION_RISK":
        return ContinuationHoldAssessment(
            ContinuationHoldState.DISTRIBUTION_RISK, False, False, False,
            risk_line, risk_price,
            ("派发或下杀风险优先",), ("不保留为确定观察仓",),
        )
    if strong in {"M5_BREAK_RISK", "M20_BREAK_RISK", "REJECTED_BREAKOUT"}:
        label = (
            "跌破五日线" if strong == "M5_BREAK_RISK"
            else "跌破二十日线" if strong == "M20_BREAK_RISK"
            else "跌回突破参考位"
        )
        return ContinuationHoldAssessment(
            ContinuationHoldState.BREAK_DEFENSE, False, False, False,
            risk_line, risk_price,
            (f"{label}，结构失效优先于盈利兑现",), ("进入破位防守",),
        )
    if high == "SECOND_WAVE_CONFIRMED" and value.timeframe_ok is not False:
        return ContinuationHoldAssessment(
            ContinuationHoldState.SECOND_WAVE_CONFIRMED, False, True, True,
            risk_line, risk_price,
            ("二波突破已确认", "继续以动态风险线守住观察仓"), (),
        )
    if high in {"SECOND_WAVE_CANDIDATE", "HIGH_CONSOLIDATION"}:
        return ContinuationHoldAssessment(
            ContinuationHoldState.SECOND_WAVE_WATCH, False, True, False,
            risk_line, risk_price,
            ("整理蓄势或二波候选", "等待突破和量能确认"), (),
        )
    if strong == "EXTENDED_CONTINUATION" or (_finite(value.bias_pct) or 0.0) >= 8.0:
        return ContinuationHoldAssessment(
            ContinuationHoldState.PROFIT_PROTECTION, True, True, False,
            risk_line, risk_price,
            ("第一波冲高或乖离偏大，但结构尚未失效", "可分批兑现并保留观察仓"), (),
        )
    if strong == "STRONG_CONTINUATION":
        return ContinuationHoldAssessment(
            ContinuationHoldState.HOLD_CONTINUATION, False, True, True,
            risk_line, risk_price,
            ("均线和突破结构未失效", "不因短期盈利退出"), (),
        )
    return ContinuationHoldAssessment(
        ContinuationHoldState.MISSING, False, False, False,
        risk_line, risk_price, (), ("当前不属于可确认的强势延续结构",),
    )


def format_continuation_hold(value: ContinuationHoldAssessment) -> str:
    if value.state is ContinuationHoldState.HOLD_CONTINUATION:
        return "持有延续：结构未失效，保留观察仓；不因短期盈利退出。"
    if value.state is ContinuationHoldState.PROFIT_PROTECTION:
        return "部分兑现：第一波冲高或乖离偏大，可分批兑现；结构未破，保留观察仓等待二波。"
    if value.state is ContinuationHoldState.SECOND_WAVE_WATCH:
        return "二波守候：整理结构仍在，保留观察仓，等待突破和量能确认。"
    if value.state is ContinuationHoldState.SECOND_WAVE_CONFIRMED:
        return "二波确认：延续结构成立，保留观察仓并继续跟踪动态风险线。"
    if value.state is ContinuationHoldState.BREAK_DEFENSE:
        reference = f"，风险参考 {value.risk_price:.2f}" if value.risk_price is not None else ""
        return f"破位防守：结构失效优先于盈利兑现{reference}。"
    if value.state is ContinuationHoldState.DISTRIBUTION_RISK:
        return "派发风险：出现下杀或承接转弱证据，优先风险处置，不保留确定观察仓。"
    return "证据不足：只观察，不输出持有延续、部分兑现或二波结论。"


def continuation_hold_to_dict(value: ContinuationHoldAssessment) -> dict:
    result = asdict(value)
    result["state"] = value.state.name
    result["state_label"] = value.state.value
    return result


def continuation_hold_from_dict(value: dict) -> ContinuationHoldAssessment:
    info = value or {}
    try:
        state = ContinuationHoldState[str(info.get("state") or "MISSING")]
    except KeyError:
        state = ContinuationHoldState.MISSING
    return ContinuationHoldAssessment(
        state=state,
        can_trim=bool(info.get("can_trim")),
        keep_observation_position=bool(info.get("keep_observation_position")),
        candidate_eligible=bool(info.get("candidate_eligible")),
        risk_line=info.get("risk_line"),
        risk_price=_finite(info.get("risk_price")),
        reasons=tuple(info.get("reasons") or ()),
        blockers=tuple(info.get("blockers") or ()),
    )


def format_continuation_hold_info(value: dict) -> str:
    return format_continuation_hold(continuation_hold_from_dict(value))
