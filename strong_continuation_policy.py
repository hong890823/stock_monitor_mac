#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""强势突破延续与动态破位风险的纯计算。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import Enum
import math
from typing import Any, Dict, Optional


class RiskLine(str, Enum):
    MA5 = "MA5"
    MA20 = "MA20"


class StrongContinuationState(str, Enum):
    MISSING = "数据不足"
    NOT_STRONG = "非强势"
    STRONG_CONTINUATION = "强势延续"
    EXTENDED_CONTINUATION = "延伸观察"
    M20_BREAK_RISK = "M20破位风险"
    M5_BREAK_RISK = "M5破位风险"
    REJECTED_BREAKOUT = "突破失败"
    DISTRIBUTION_RISK = "派发风险"


@dataclass(frozen=True)
class StrongContinuationInput:
    price: float
    ma5: float
    ma10: float
    ma20: float
    ma20_prev: float
    price_location: str
    bias_pct: float
    breakout_reference: Optional[float] = None
    volume_ratio: Optional[float] = None
    lower_pivots_rising: Optional[bool] = None
    support_holds: Optional[bool] = None
    market_ok: Optional[bool] = None
    sector_ok: Optional[bool] = None
    bar_complete: bool = True
    break_confirmed: bool = False


@dataclass(frozen=True)
class StrongContinuationAssessment:
    state: StrongContinuationState
    risk_line: Optional[RiskLine]
    risk_price: Optional[float]
    breakout_reference: Optional[float]
    risk_warning: bool
    risk_confirmed: bool
    risk_released: bool
    eligible_candidate: bool
    reasons: tuple[str, ...]
    blockers: tuple[str, ...]
    missing_data: tuple[str, ...]
    confidence: str


def _finite(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _required_positive(name: str, value: Any, missing: list[str]) -> Optional[float]:
    number = _finite(value)
    if number is None or number <= 0:
        missing.append(name)
        return None
    return number


def assess_strong_continuation(value: StrongContinuationInput) -> StrongContinuationAssessment:
    missing = []
    price = _required_positive("现价缺失或无效", value.price, missing)
    ma5 = _required_positive("MA5缺失或无效", value.ma5, missing)
    ma10 = _required_positive("MA10缺失或无效", value.ma10, missing)
    ma20 = _required_positive("MA20缺失或无效", value.ma20, missing)
    ma20_prev = _required_positive("MA20前值缺失或无效", value.ma20_prev, missing)
    bias = _finite(value.bias_pct)
    if bias is None:
        missing.append("乖离率缺失或无效")

    breakout_reference = _finite(value.breakout_reference)
    if breakout_reference is not None and breakout_reference <= 0:
        breakout_reference = None

    if missing:
        return StrongContinuationAssessment(
            state=StrongContinuationState.MISSING,
            risk_line=None,
            risk_price=None,
            breakout_reference=breakout_reference,
            risk_warning=False,
            risk_confirmed=False,
            risk_released=False,
            eligible_candidate=False,
            reasons=(),
            blockers=("核心趋势数据不足，不做默认放行",),
            missing_data=tuple(missing),
            confidence="低",
        )

    price_location = value.price_location if value.price_location in {"LOW", "MID", "HIGH"} else ""
    use_ma5 = price_location == "HIGH" or price_location == "" or bias >= 8.0
    risk_line = RiskLine.MA5 if use_ma5 else RiskLine.MA20
    risk_price = ma5 if risk_line is RiskLine.MA5 else ma20
    volume = _finite(value.volume_ratio)

    reasons = []
    blockers = []
    if price_location == "":
        reasons.append("价格位置缺失，按MA5采用更保守风险线")

    distribution = (
        value.support_holds is False
        and (
            value.lower_pivots_rising is False
            or (volume is not None and volume >= 1.5)
        )
    )
    if distribution:
        return StrongContinuationAssessment(
            state=StrongContinuationState.DISTRIBUTION_RISK,
            risk_line=risk_line,
            risk_price=risk_price,
            breakout_reference=breakout_reference,
            risk_warning=True,
            risk_confirmed=True,
            risk_released=False,
            eligible_candidate=False,
            reasons=("低点下移或放量滞涨", "关键支撑失守", "下杀/派发风险优先"),
            blockers=("不进入候选",),
            missing_data=(),
            confidence="高",
        )

    if breakout_reference is not None and price < breakout_reference:
        return StrongContinuationAssessment(
            state=StrongContinuationState.REJECTED_BREAKOUT,
            risk_line=risk_line,
            risk_price=risk_price,
            breakout_reference=breakout_reference,
            risk_warning=True,
            risk_confirmed=value.bar_complete,
            risk_released=False,
            eligible_candidate=False,
            reasons=("价格重新跌回突破参考位", "突破结构失效"),
            blockers=("不进入强势候选",),
            missing_data=(),
            confidence="高",
        )

    below_risk_line = price < risk_price
    if below_risk_line:
        confirmed = bool(
            value.break_confirmed
            and price <= risk_price * 0.995
        )
        return StrongContinuationAssessment(
            state=(
                StrongContinuationState.M5_BREAK_RISK
                if risk_line is RiskLine.MA5
                else StrongContinuationState.M20_BREAK_RISK
            ),
            risk_line=risk_line,
            risk_price=risk_price,
            breakout_reference=breakout_reference,
            risk_warning=True,
            risk_confirmed=confirmed,
            risk_released=False,
            eligible_candidate=False,
            reasons=(
                "破位确认" if confirmed else "风险预警",
                f"现价跌破{risk_line.value}参考线",
            ),
            blockers=("风险状态优先于机会候选",),
            missing_data=(),
            confidence="高" if confirmed else "中",
        )

    aligned = ma5 > ma10 > ma20 and ma20 > ma20_prev
    risk_released = bool(value.break_confirmed)
    if not aligned:
        return StrongContinuationAssessment(
            state=StrongContinuationState.NOT_STRONG,
            risk_line=risk_line,
            risk_price=risk_price,
            breakout_reference=breakout_reference,
            risk_warning=False,
            risk_confirmed=False,
            risk_released=risk_released,
            eligible_candidate=False,
            reasons=("未形成MA5>MA10>MA20且MA20上行的多头结构",),
            blockers=(),
            missing_data=(),
            confidence="高",
        )

    reasons.extend((
        "MA5>MA10>MA20且MA20上行",
        f"现价守住{risk_line.value}风险线",
        "风险解除" if risk_released else "风险线暂未失守",
    ))
    if breakout_reference is not None:
        reasons.append("价格站上突破参考位")
    if value.lower_pivots_rising is False:
        blockers.append("近期低点有下移迹象")
    if value.support_holds is False:
        blockers.append("关键支撑未确认守住")
    if value.market_ok is False:
        blockers.append("大盘环境不支持")
    if value.sector_ok is False:
        blockers.append("板块环境不支持")

    extended = price_location == "HIGH" or bias >= 8.0
    if extended:
        return StrongContinuationAssessment(
            state=StrongContinuationState.EXTENDED_CONTINUATION,
            risk_line=risk_line,
            risk_price=risk_price,
            breakout_reference=breakout_reference,
            risk_warning=False,
            risk_confirmed=False,
            risk_released=risk_released,
            eligible_candidate=False,
            reasons=tuple(reasons + ["价格偏高或乖离较大，只作延伸观察"]),
            blockers=tuple(blockers),
            missing_data=(),
            confidence="中",
        )

    if blockers:
        return StrongContinuationAssessment(
            state=StrongContinuationState.NOT_STRONG,
            risk_line=risk_line,
            risk_price=risk_price,
            breakout_reference=breakout_reference,
            risk_warning=False,
            risk_confirmed=False,
            risk_released=risk_released,
            eligible_candidate=False,
            reasons=tuple(reasons),
            blockers=tuple(blockers),
            missing_data=(),
            confidence="中",
        )

    return StrongContinuationAssessment(
        state=StrongContinuationState.STRONG_CONTINUATION,
        risk_line=risk_line,
        risk_price=risk_price,
        breakout_reference=breakout_reference,
        risk_warning=False,
        risk_confirmed=False,
        risk_released=risk_released,
        eligible_candidate=True,
        reasons=tuple(reasons),
        blockers=(),
        missing_data=(),
        confidence="高",
    )


def strong_continuation_to_dict(value: StrongContinuationAssessment) -> dict:
    result = asdict(value)
    result["state"] = value.state.name
    result["state_label"] = value.state.value
    if value.risk_line is not None:
        result["risk_line"] = value.risk_line.name
    return result


def format_strong_risk(info: Dict) -> str:
    if not info:
        return ""
    risk_price = _finite(info.get("risk_price"))
    price_text = f"，风险参考 {risk_price:.2f}" if risk_price is not None else ""
    if bool(info.get("risk_released")):
        line = str(info.get("risk_line") or "动态破位线")
        line_label = "五日线" if line in {"MA5", "M5"} else "二十日线" if line in {"MA20", "M20"} else line
        return f"风险解除：股价重新站回{line_label}{price_text}，此前下杀按假破位处理"

    state = str(info.get("state") or info.get("strong_state") or "")
    risk_line = str(info.get("risk_line") or "")
    if state in {"M5_BREAK_RISK", "M5破位风险"} or risk_line in {"MA5", "M5"}:
        risk_label = "跌破五日线风险"
    elif state in {"M20_BREAK_RISK", "M20破位风险"} or risk_line in {"MA20", "M20"}:
        risk_label = "跌破二十日线风险"
    elif state in {"REJECTED_BREAKOUT", "突破失败"}:
        risk_label = "突破失败/假突破风险"
    elif state in {"DISTRIBUTION_RISK", "派发风险"}:
        risk_label = "高位派发下杀风险"
    else:
        return ""
    confirmed = bool(info.get("confirmed", info.get("risk_confirmed", False)))
    status = "破位确认" if confirmed else "风险预警"
    return f"{status}：{risk_label}{price_text}；仅作风险提示和处置建议，非交易执行"
