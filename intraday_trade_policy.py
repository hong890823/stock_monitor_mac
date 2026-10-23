#!/usr/bin/env python3
"""盘中确定性动作与A股T+1交易资格。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum

from intraday_bars import TimeframeEvidence


LOT = 100


class DecisionAction(Enum):
    WAIT = "等待确认"
    BUY_SMALL = "小仓买入"
    RISK_EXIT = "风险离场"
    RISK_REDUCE = "分批减仓"
    NORMAL_EXIT = "分批卖出"
    T_SELL = "做T减仓"
    HOLD = "持有观察"
    NEXT_DAY_REVIEW = "下一交易日处置"


@dataclass(frozen=True)
class PositionEligibility:
    total_quantity: int = 0
    sellable_quantity: int = 0
    today_locked_quantity: int = 0
    buy_capacity_quantity: int = 0
    ledger_consistent: bool = True


@dataclass(frozen=True)
class TradeDecision:
    action: DecisionAction
    message: str
    max_sell_quantity: int = 0
    max_buy_quantity: int = 0
    invalidation_price: float = 0.0


def _wait(message: str) -> TradeDecision:
    return TradeDecision(action=DecisionAction.WAIT, message=message)


def _next_day_review(eligibility: PositionEligibility, reason: str) -> TradeDecision:
    locked = max(0, int(eligibility.today_locked_quantity))
    lock_text = f"今日新增{locked}股" if locked else "今日无可卖仓位"
    return TradeDecision(
        action=DecisionAction.NEXT_DAY_REVIEW,
        message=f"{reason}；{lock_text}受A股T+1限制，下一交易日优先复核处置。",
    )


def _value(source, key, default=None):
    if isinstance(source, Mapping):
        return source.get(key, default)
    return getattr(source, key, default)


def evaluate_trade_action(
    intent: str,
    evidence: TimeframeEvidence,
    eligibility: PositionEligibility,
    invalidation_price: float,
    permissions=None,
    trend=None,
    high_position=None,
    volume_context=None,
) -> TradeDecision:
    intent = str(intent or "").lower().strip()
    sellable = max(0, int(eligibility.sellable_quantity))
    invalidation = float(invalidation_price or 0.0)
    if not eligibility.ledger_consistent:
        return _wait("持仓账本数量不一致，暂不生成可卖股数；核对券商持仓后再执行。")
    if intent in {"buy", "add", "reentry", "t_buy", "t_sell", "do_t"} and permissions is not None:
        allowed = {
            "buy": _value(permissions, "allow_normal_buy", False),
            "add": _value(permissions, "allow_add", False),
            "reentry": _value(permissions, "allow_reentry", False),
            "t_buy": _value(permissions, "allow_reentry", False),
            "t_sell": _value(permissions, "allow_t", False),
            "do_t": _value(permissions, "allow_t", False),
        }.get(intent, False)
        if not allowed:
            blockers = "；".join(_value(permissions, "blockers", ()) or ())
            return _wait((blockers or "趋势或高位结构不允许该动作") + "，仅观察，不执行。")

    if intent in {"buy", "add"}:
        if invalidation <= 0:
            return _wait("缺少明确失效价，仅观察，不执行买入。")
        if (
            not evidence.complete
            or evidence.m15_confirmations < 2
            or not evidence.m30_aligned
            or not evidence.clearance_confirmed
        ):
            return _wait("15/30分钟站稳距离不足，仅观察，不执行买入。")
        if evidence.last_bar_rejected:
            return _wait("15分钟K线出现明显上影拒绝，仅观察，不执行买入。")
        if bool(_value(volume_context, "blowoff_risk", False)):
            return _wait("上涨放量但出现冲高回落迹象，量价证据冲突，仅观察，不执行买入。")
        if _value(volume_context, "stock_volume_confirming", None) is not True:
            return _wait("个股量能证据不足，仅观察，不执行买入。")
        if _value(volume_context, "market_expanding", None) is not True:
            return _wait("两市成交量证据不足或未放大，仅观察，不执行买入。")
        if not evidence.complete or evidence.m15_confirmations < 2 or not evidence.m30_aligned:
            return _wait("15/30分钟条件未完整确认，仅观察，不执行买入。")
        buy_quantity = (int(eligibility.buy_capacity_quantity) // LOT) * LOT
        if buy_quantity < LOT:
            return _wait("仓位边界不允许至少一手，仅观察，不执行买入。")
        return TradeDecision(
            action=DecisionAction.BUY_SMALL,
            message=(
                f"15/30分钟条件已确认，可小仓买入约{buy_quantity}股；"
                f"失效价{invalidation:.2f}，重新跌破则停止执行；"
                "今日新增仓位受A股T+1限制，当日不可卖出。"
            ),
            max_buy_quantity=buy_quantity,
            invalidation_price=invalidation,
        )

    if intent in {"risk_exit", "stop", "break_support"}:
        if not evidence.risk_complete or not evidence.risk_confirmed:
            return _wait("风险条件尚未由已完成15分钟K线确认，继续观察。")
        if evidence.risk_confirmations < 2:
            return _wait("首次破位仅作风险观察，等待连续两根15分钟K线确认。")
        if evidence.risk_level == "NORMAL" and evidence.risk_confirmations < 3:
            return _wait("历史风险级别一般，等待第三根15分钟K线确认后再分批减仓。")
        if sellable <= 0:
            return _next_day_review(eligibility, "15分钟风险条件已确认")
        absorption = bool(_value(volume_context, "absorption_confirmed", False))
        if absorption and evidence.risk_level != "CRITICAL":
            return TradeDecision(
                action=DecisionAction.RISK_REDUCE,
                message=(
                    "放量下杀后出现承接迹象，首批仅减100股，继续观察是否收复支撑；"
                    "今日新增仓位受A股T+1限制，不纳入本次卖出。"
                ),
                max_sell_quantity=LOT,
            )
        if evidence.risk_level == "CRITICAL" and evidence.risk_confirmations >= 3:
            return TradeDecision(
                action=DecisionAction.RISK_EXIT,
                message=(
                    f"连续3根15分钟K线确认且历史结构失效，使用今日可卖{sellable}股分批离场；"
                    "今日新增仓位受A股T+1限制，不纳入本次卖出。"
                ),
                max_sell_quantity=sellable,
            )
        reduce_quantity = max(LOT, ((sellable // 3) // LOT) * LOT)
        reduce_quantity = min(sellable, reduce_quantity)
        return TradeDecision(
            action=DecisionAction.RISK_REDUCE,
            message=(
                f"连续15分钟风险已确认，首批最多减{reduce_quantity}股；"
                "今日新增仓位受A股T+1限制，不纳入本次卖出。"
            ),
            max_sell_quantity=reduce_quantity,
        )

    if intent in {"normal_exit", "exit", "resistance"}:
        if not evidence.complete or evidence.m15_confirmations < 1 or not evidence.m30_aligned:
            return _wait("15/30分钟卖点尚未共同确认，仅观察，不执行卖出。")
        if sellable <= 0:
            return _next_day_review(eligibility, "普通卖点已确认")
        return TradeDecision(
            action=DecisionAction.NORMAL_EXIT,
            message=(
                f"15/30分钟卖点已确认，可使用原有可卖{sellable}股分批卖出；"
                "今日新增仓位受A股T+1限制，不纳入本次卖出。"
            ),
            max_sell_quantity=sellable,
        )

    if intent in {"t_sell", "do_t"}:
        if not evidence.complete or evidence.m15_confirmations < 1 or not evidence.m30_aligned:
            return _wait("15/30分钟做T条件尚未共同确认，仅观察，不做T。")
        if sellable <= 0:
            return _next_day_review(eligibility, "普通做T条件已确认")
        planned = (int(eligibility.total_quantity) // 3 // LOT) * LOT
        max_sell = min(sellable, planned if planned > 0 else sellable)
        return TradeDecision(
            action=DecisionAction.T_SELL,
            message=(
                f"15/30分钟条件已确认，可用原有可卖仓位做T，最多减{max_sell}股；"
                "今日新增仓位受A股T+1限制，不纳入本次做T。"
            ),
            max_sell_quantity=max_sell,
            invalidation_price=invalidation,
        )

    return TradeDecision(
        action=DecisionAction.HOLD,
        message="当前维持持有观察，不新增确定性买卖动作。",
    )
