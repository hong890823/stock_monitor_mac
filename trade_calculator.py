#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""单次买卖成本计算，不写回持仓配置。"""

from datetime import datetime
import math


TARGET_COST_LOT = 100
TARGET_COST_TIER_WEIGHTS = (0.40, 0.35, 0.25)


def calculate_trade(holding_shares, average_cost, side, trade_shares, trade_price,
                    fees=0, current_price=None):
    holding_shares = float(holding_shares)
    average_cost = float(average_cost)
    trade_shares = float(trade_shares)
    trade_price = float(trade_price)
    fees = float(fees or 0)
    if holding_shares < 0 or average_cost < 0 or trade_shares <= 0 or trade_price <= 0 or fees < 0:
        raise ValueError("股数、价格和费用必须是有效的非负数字，操作股数和价格必须大于0")
    side = side.strip().lower()
    if side not in ("买入", "卖出", "buy", "sell"):
        raise ValueError("操作类型只能是买入或卖出")
    is_buy = side in ("买入", "buy")
    if not is_buy and trade_shares > holding_shares:
        raise ValueError("卖出股数不能大于当前持仓股数")

    before_cost_total = holding_shares * average_cost
    gross_amount = trade_shares * trade_price
    realized_pnl = 0.0
    if is_buy:
        after_shares = holding_shares + trade_shares
        after_cost_total = before_cost_total + gross_amount + fees
        after_average_cost = after_cost_total / after_shares if after_shares else 0
    else:
        after_shares = holding_shares - trade_shares
        after_cost_total = after_shares * average_cost
        after_average_cost = average_cost if after_shares else 0
        realized_pnl = (trade_price - average_cost) * trade_shares - fees

    floating_pnl = None
    floating_pct = None
    if current_price is not None and after_shares > 0:
        current_price = float(current_price)
        floating_pnl = (current_price - after_average_cost) * after_shares
        floating_pct = (current_price / after_average_cost - 1) * 100 if after_average_cost else None

    return {
        "side": "买入" if is_buy else "卖出",
        "holding_shares": holding_shares,
        "average_cost": average_cost,
        "trade_shares": trade_shares,
        "trade_price": trade_price,
        "fees": fees,
        "gross_amount": gross_amount,
        "after_shares": after_shares,
        "after_cost_total": after_cost_total,
        "after_average_cost": after_average_cost,
        "realized_pnl": realized_pnl,
        "current_price": current_price,
        "floating_pnl": floating_pnl,
        "floating_pct": floating_pct,
        "calculated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


def format_trade_report(result, stock_label=None, fee_note=None):
    label = stock_label or "未指定股票"
    side = result["side"]
    lines = [
        "💰 买卖成本计算",
        f"对象：{label}",
        f"计算时间：{result['calculated_at']}",
        "",
        "一、操作信息",
        f"操作：{side} {result['trade_shares']:.0f}股 @ {result['trade_price']:.2f}",
        f"交易金额：{result['gross_amount']:.2f}元",
        f"交易费用：{result['fees']:.2f}元",
        "",
        "二、操作后持仓",
        f"剩余/持有股数：{result['after_shares']:.0f}股",
        f"操作后平均成本：{result['after_average_cost']:.4f}元",
        f"操作后持仓成本总额：{result['after_cost_total']:.2f}元",
    ]
    if side == "卖出":
        lines.append(f"本次已实现盈亏：{result['realized_pnl']:+.2f}元")
    if result["floating_pnl"] is not None:
        lines.extend([
            "",
            "三、按当前价估算",
            f"当前价格：{result['current_price']:.2f}元",
            f"剩余持仓浮盈/浮亏：{result['floating_pnl']:+.2f}元",
            f"浮盈/浮亏比例：{result['floating_pct']:+.2f}%",
        ])
    else:
        lines.extend([
            "",
            "三、浮盈/浮亏",
            "未输入股票名称/代码，未获取当前价，本次只计算操作后成本。",
        ])
    if fee_note:
        lines.append(f"费用说明：{fee_note}")
    lines.extend([
        "",
        "说明：本计算不写回持仓配置；未计入的佣金、印花税、过户费等会影响实际结果。",
        "⚠️ 仅作计算参考，不构成投资建议。",
    ])
    return "\n".join(lines)


def _build_target_cost_triggers(current_price, target_cost, technical, entry):
    max_trigger = min(current_price, target_cost * 0.995)
    stop_price = entry.get("stop_price")
    try:
        stop_price = float(stop_price) if stop_price is not None else None
    except (TypeError, ValueError):
        stop_price = None
    invalidation = stop_price if stop_price and stop_price > 0 else target_cost * 0.82
    floor_price = max(invalidation * 1.01, target_cost * 0.82)
    if max_trigger <= floor_price:
        return []

    volatility = technical.get("volatility") or {}
    try:
        atr = float(volatility.get("atr") or 0)
    except (TypeError, ValueError):
        atr = 0
    try:
        ma20 = float(technical.get("ma20") or entry.get("ma20") or 0)
    except (TypeError, ValueError):
        ma20 = 0
    try:
        boll_mid = float(volatility.get("boll_mid") or 0)
        boll_lower = float(volatility.get("boll_lower") or 0)
    except (TypeError, ValueError):
        boll_mid = boll_lower = 0

    candidates = [max_trigger]
    for value in (
        ma20,
        float(technical.get("support") or 0),
        boll_mid,
        boll_lower,
        stop_price * 1.02 if stop_price else 0,
    ):
        if floor_price < value < max_trigger:
            candidates.append(float(value))

    spacing = max_trigger * 0.015
    if atr > 0:
        spacing = min(spacing, atr * 0.35)
    spacing = max(spacing, max_trigger * 0.003)
    triggers = []
    for value in sorted(set(candidates), reverse=True):
        if not triggers or triggers[-1] - value >= spacing:
            triggers.append(value)
        if len(triggers) == 3:
            break

    if len(triggers) < 3:
        upper = triggers[-1] if triggers else max_trigger
        remaining = 3 - len(triggers)
        for index in range(remaining, 0, -1):
            value = upper - (upper - floor_price) * index / (remaining + 1)
            triggers.append(value)
    return sorted(triggers[:3], reverse=True)


def _target_cost_capital_limits(holding_shares, current_price, capital_constraints):
    constraints = capital_constraints or {}
    warnings = []
    try:
        total_capital = float(constraints.get("total_capital") or 0)
    except (TypeError, ValueError):
        total_capital = 0
    try:
        available_capital = float(constraints.get("available_capital") or 0)
    except (TypeError, ValueError):
        available_capital = 0
    try:
        max_position_pct = float(constraints.get("max_position_pct") or 0)
    except (TypeError, ValueError):
        max_position_pct = 0

    available_capital_source = None
    if total_capital > 0 and available_capital <= 0:
        invested_cost = 0.0
        for position in constraints.get("positions") or []:
            if not isinstance(position, dict):
                continue
            try:
                quantity = max(0.0, float(position.get("quantity") or 0))
                cost = max(0.0, float(position.get("cost") or 0))
            except (TypeError, ValueError):
                continue
            invested_cost += quantity * cost
        available_capital = max(0.0, total_capital - invested_cost)
        available_capital_source = "按持仓成本记录估算"

    max_shares = math.inf
    if total_capital > 0:
        if max_position_pct <= 0:
            warnings.append("最大仓位比例未配置，仅按可用资金约束")
        else:
            position_headroom = total_capital * max_position_pct / 100 - holding_shares * current_price
            max_shares = min(max_shares, max(0, math.floor(position_headroom / current_price / TARGET_COST_LOT) * TARGET_COST_LOT))
        cash_shares = math.floor(available_capital / current_price / TARGET_COST_LOT) * TARGET_COST_LOT
        max_shares = min(max_shares, max(0, cash_shares))
    else:
        warnings.append("总资金未配置，本次补仓量未受资金和最大仓位约束")

    return {
        "total_capital": total_capital if total_capital > 0 else None,
        "available_capital": available_capital if total_capital > 0 else None,
        "available_capital_source": available_capital_source,
        "max_position_pct": max_position_pct if max_position_pct > 0 else None,
        "max_add_shares": max_shares,
        "warnings": warnings,
    }


def _build_target_cost_ladder(
    holding_shares,
    average_cost,
    target_cost,
    current_price,
    required_buy_shares,
    fees,
    triggers,
    capital_limits,
):
    if required_buy_shares is None or not triggers:
        return [], average_cost
    total_lots = math.ceil(required_buy_shares / TARGET_COST_LOT)
    raw_lots = [total_lots * weight for weight in TARGET_COST_TIER_WEIGHTS]
    lots = [math.floor(value) for value in raw_lots]
    for index in sorted(range(len(lots)), key=lambda item: raw_lots[item] - lots[item], reverse=True):
        if sum(lots) >= total_lots:
            break
        lots[index] += 1

    max_shares = capital_limits["max_add_shares"]
    cumulative_shares = 0
    cumulative_amount = 0.0
    cumulative_fees = 0.0
    ladder = []
    for index, (trigger, lot_count) in enumerate(zip(triggers, lots), start=1):
        shares = lot_count * TARGET_COST_LOT
        remaining_capacity = max(0, max_shares - cumulative_shares)
        shares = min(shares, remaining_capacity)
        shares = math.floor(shares / TARGET_COST_LOT) * TARGET_COST_LOT
        if shares <= 0:
            break
        amount = shares * trigger
        planned_total_shares = sum(item * TARGET_COST_LOT for item in lots)
        fee_share = fees * (shares / planned_total_shares) if planned_total_shares else 0
        cumulative_shares += shares
        cumulative_amount += amount
        cumulative_fees += fee_share
        after_cost = (
            (holding_shares * average_cost + cumulative_amount + cumulative_fees)
            / (holding_shares + cumulative_shares)
        )
        ladder.append({
            "level": index,
            "trigger_price": trigger,
            "shares": shares,
            "amount": amount,
            "cumulative_shares": cumulative_shares,
            "after_average_cost": after_cost,
            "condition": "止跌确认后执行；跌破失效位取消后续档位",
        })
    projected_cost = ladder[-1]["after_average_cost"] if ladder else average_cost
    return ladder, projected_cost


def _build_target_cost_t_plan(
    holding_shares,
    average_cost,
    current_price,
    technical,
    entry,
    sellable_quantity,
):
    if sellable_quantity is None:
        return {
            "eligible": False,
            "sellable_quantity": None,
            "total_sell_shares": 0,
            "sell_batches": [],
            "buyback_price": None,
            "buyback_condition": "可卖数量未核验，只作冲高观察",
            "invalidation_rule": "当日不能确认可卖仓位，不执行做T卖出",
            "estimated_realized_pnl": 0.0,
            "estimated_sell_fees": 0.0,
            "fee_note": "卖出费用未单独输入",
            "estimated_net_cost_per_share": average_cost,
            "estimated_rebuy_average_cost": average_cost,
        }
    sellable_quantity = max(0, sellable_quantity)
    total_sell_shares = math.floor(min(holding_shares, sellable_quantity) / 3 / TARGET_COST_LOT) * TARGET_COST_LOT
    if total_sell_shares <= 0:
        return {
            "eligible": False,
            "sellable_quantity": sellable_quantity,
            "total_sell_shares": 0,
            "sell_batches": [],
            "buyback_price": None,
            "buyback_condition": "可卖仓位不足一手，暂不执行做T",
            "invalidation_rule": "待下一交易日出现完整一手可卖仓位后再评估",
            "estimated_realized_pnl": 0.0,
            "estimated_sell_fees": 0.0,
            "fee_note": "卖出费用未单独输入",
            "estimated_net_cost_per_share": average_cost,
            "estimated_rebuy_average_cost": average_cost,
        }

    volatility = technical.get("volatility") or {}
    try:
        atr = float(volatility.get("atr") or current_price * 0.02)
    except (TypeError, ValueError):
        atr = current_price * 0.02
    first_shares = math.ceil(total_sell_shares / 2 / TARGET_COST_LOT) * TARGET_COST_LOT
    first_shares = min(first_shares, total_sell_shares)
    second_shares = total_sell_shares - first_shares
    sell_batches = [{
        "level": 1,
        "trigger_price": current_price,
        "shares": first_shares,
        "condition": "现价接近/突破压力时先减第一批",
    }]
    if second_shares > 0:
        sell_batches.append({
            "level": 2,
            "trigger_price": current_price + max(atr, current_price * 0.02),
            "shares": second_shares,
            "condition": "继续冲高且量能未明显衰竭时减第二批",
        })

    sell_amount = sum(item["shares"] * item["trigger_price"] for item in sell_batches)
    average_sell_price = sell_amount / total_sell_shares
    estimated_sell_fees = 0.0
    realized_pnl = (average_sell_price - average_cost) * total_sell_shares - estimated_sell_fees

    try:
        ma20 = float(technical.get("ma20") or entry.get("ma20") or 0)
    except (TypeError, ValueError):
        ma20 = 0
    try:
        support = float(technical.get("support") or 0)
    except (TypeError, ValueError):
        support = 0
    buyback_price = max(ma20, support) if ma20 > 0 or support > 0 else current_price * 0.96
    remaining_shares = holding_shares - total_sell_shares
    estimated_rebuy_average_cost = (
        (remaining_shares * average_cost + total_sell_shares * buyback_price)
        / holding_shares
    ) if holding_shares else average_cost
    estimated_net_cost = average_cost - realized_pnl / holding_shares
    stop_price = entry.get("stop_price")
    try:
        stop_price = float(stop_price) if stop_price is not None else None
    except (TypeError, ValueError):
        stop_price = None
    invalidation_rule = (
        f"跌破{stop_price:.2f}取消接回并按风险减仓"
        if stop_price else "跌破有效支撑取消接回并按风险减仓"
    )
    return {
        "eligible": True,
        "sellable_quantity": sellable_quantity,
        "total_sell_shares": total_sell_shares,
        "sell_batches": sell_batches,
        "buyback_price": buyback_price,
        "buyback_condition": "回踩MA20/支撑且量比不高于0.90、出现止跌后接回",
        "invalidation_rule": invalidation_rule,
        "estimated_realized_pnl": realized_pnl,
        "estimated_sell_fees": estimated_sell_fees,
        "fee_note": "卖出费用未单独输入，预计已实现盈亏为税前毛参考",
        "estimated_net_cost_per_share": estimated_net_cost,
        "estimated_rebuy_average_cost": estimated_rebuy_average_cost,
    }


def plan_target_cost(
    holding_shares,
    average_cost,
    target_cost,
    current_price,
    technical=None,
    fees=0,
    sellable_quantity=None,
    capital_constraints=None,
):
    """根据当前持仓和技术分析，规划达到更低目标成本的条件式策略。

    目标成本是操作后的持仓平均成本，不是建议的买入价。函数只计算
    参考方案，不写回持仓、不生成交易记录，也不替用户决定是否执行。
    """
    try:
        holding_shares = float(holding_shares)
        average_cost = float(average_cost)
        target_cost = float(target_cost)
        current_price = float(current_price)
        fees = float(fees or 0)
    except (TypeError, ValueError) as error:
        raise ValueError("持仓、成本、目标价和现价必须是数字") from error
    if holding_shares <= 0 or average_cost <= 0 or target_cost <= 0 or current_price <= 0:
        raise ValueError("持仓股数、当前成本、目标价和现价必须大于0")
    if target_cost >= average_cost:
        raise ValueError("目标持仓成本必须低于当前平均成本")
    if fees < 0:
        raise ValueError("交易费用不能为负数")
    if sellable_quantity is not None:
        try:
            sellable_quantity = float(sellable_quantity)
        except (TypeError, ValueError) as error:
            raise ValueError("当日可卖数量必须是数字") from error
        if sellable_quantity < 0 or sellable_quantity > holding_shares:
            raise ValueError("当日可卖数量不能小于0或超过持仓")

    technical = technical or {}
    support = float(technical.get("support") or 0)
    resistance = float(technical.get("resistance") or 0)
    score = float(technical.get("score") or 0)
    volume_ratio = technical.get("volume_ratio")
    try:
        volume_ratio = float(volume_ratio) if volume_ratio is not None else None
    except (TypeError, ValueError):
        volume_ratio = None
    entry = technical.get("entry_structure") or {}
    trend_weak = bool(entry.get("trend_weak"))
    can_buy = bool(entry.get("can_buy"))
    state_label = str(entry.get("state_label") or "当前技术结构")
    stop_price = entry.get("stop_price")

    required_buy_shares = 0.0
    buy_price = current_price
    if current_price < target_cost:
        denominator = target_cost - buy_price
        required_buy_shares = (
            (holding_shares * (average_cost - target_cost) + fees) / denominator
            if denominator > 0 else math.inf
        )

    capital_limits = _target_cost_capital_limits(
        holding_shares, current_price, capital_constraints
    )
    buy_triggers = _build_target_cost_triggers(
        current_price, target_cost, technical, entry
    )
    buy_ladder, projected_average_cost = _build_target_cost_ladder(
        holding_shares,
        average_cost,
        target_cost,
        current_price,
        required_buy_shares,
        fees,
        buy_triggers,
        capital_limits,
    )
    target_reached = (
        required_buy_shares is not None
        and projected_average_cost <= target_cost + 1e-9
    )
    rally_t_plan = _build_target_cost_t_plan(
        holding_shares,
        average_cost,
        current_price,
        technical,
        entry,
        sellable_quantity,
    ) if resistance > 0 and current_price >= resistance * 0.99 else None

    if resistance > 0 and current_price >= resistance * 0.99:
        if sellable_quantity == 0:
            action = "冲高观察"
            strategy = (
                f"现价接近/突破压力{resistance:.2f}，不在高位补仓；当日无可卖仓位，"
                "不要卖出今日新买入部分，等待下一交易日再评估做T。"
            )
        else:
            action = "冲高做T"
            sellable_text = (
                f"最多参考{sellable_quantity:.0f}股可卖仓位"
                if sellable_quantity is not None else "仅限原有可卖仓位"
            )
            strategy = (
                f"现价接近/突破压力{resistance:.2f}，不在高位补仓；可在{sellable_text}范围内"
                "先分批卖出，等待回踩支撑或重新站稳后回踩接回，以降低持仓成本。"
            )
    elif current_price >= target_cost:
        action = "等待回落"
        strategy = (
            f"现价{current_price:.2f}尚未低于目标成本{target_cost:.2f}，不追高补仓；"
            f"等待回落至目标价下方并出现止跌确认，或沿支撑{support:.2f}分批评估。"
        )
    elif trend_weak or score <= -2 or not can_buy:
        action = "等待确认"
        strategy = (
            f"当前为{state_label}，趋势/结构尚未确认，不建议补仓摊平；"
            f"先等待重新站回支撑{support:.2f}并出现止跌、量价改善后再评估。"
        )
    elif (
        required_buy_shares
        and capital_limits["total_capital"] is not None
        and capital_limits["max_add_shares"] <= 0
    ):
        action = "资金/仓位受限"
        strategy = (
            "当前可用资金或个股仓位上限没有新增空间，不生成补仓梯度；"
            "请先释放可用资金或降低持仓市值，达到目标成本不能靠突破仓位上限完成。"
        )
    elif buy_ladder and not target_reached:
        action = "受限梯度补仓"
        planned_shares = sum(item["shares"] for item in buy_ladder)
        strategy = (
            f"当前技术位置允许按梯度观察补仓，但资金/仓位上限只允许计划{planned_shares:.0f}股；"
            f"按该受限计划成交后预计平均成本{projected_average_cost:.4f}元，"
            f"不能直接达到目标{target_cost:.2f}元。只执行已列档位，不为达标突破资金上限。"
        )
    elif support > 0 and current_price <= support * 1.05:
        action = "分批买入"
        lot_quantity = math.ceil(required_buy_shares / 100) * 100 if required_buy_shares else 0
        strategy = (
            f"现价位于支撑{support:.2f}附近，且技术结构允许观察买入；"
            f"达到目标成本约需按当前价补{required_buy_shares:.0f}股，A股按整手可参考{lot_quantity:.0f}股，"
            "建议拆成至少两批，首批不超过计划数量的一半；跌破失效位则停止补仓。"
        )
    else:
        action = "等待回落"
        strategy = (
            f"目标成本需要低于现价，先等回落至支撑{support:.2f}附近并止跌，"
            "确认后再分批补仓，不在中途追价。"
        )

    return {
        "holding_shares": holding_shares,
        "average_cost": average_cost,
        "target_cost": target_cost,
        "current_price": current_price,
        "fees": fees,
        "sellable_quantity": sellable_quantity,
        "support": support,
        "resistance": resistance,
        "score": score,
        "volume_ratio": volume_ratio,
        "state_label": state_label,
        "stop_price": stop_price,
        "required_buy_shares": required_buy_shares if math.isfinite(required_buy_shares) else None,
        "buy_ladder": buy_ladder,
        "projected_average_cost": projected_average_cost,
        "target_reached": target_reached,
        "capital_limits": capital_limits,
        "t_plan": rally_t_plan,
        "action": action,
        "strategy": strategy,
        "calculated_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


def format_target_cost_report(result, stock_label=None):
    """把目标成本策略结果格式化为可读报告。"""
    label = stock_label or "未指定股票"
    required = result.get("required_buy_shares")
    required_text = f"约{required:.0f}股" if required is not None else "当前价不适合直接计算"
    stop_price = result.get("stop_price")
    stop_text = f"{float(stop_price):.2f}" if stop_price else "未提供"
    volume_ratio = result.get("volume_ratio")
    volume_text = f"{volume_ratio:.2f}" if volume_ratio is not None else "未提供"
    lines = [
        "🎯 目标持仓成本策略",
        f"对象：{label}",
        f"分析时间：{result['calculated_at']}",
        "",
        "一、目标与当前状态",
        f"当前持仓：{result['holding_shares']:.0f}股｜当前平均成本：{result['average_cost']:.4f}元",
        f"当前价格：{result['current_price']:.2f}元｜目标持仓成本：{result['target_cost']:.2f}元",
        f"达到目标的理论补仓量：{required_text}（按当前价、不含滑点）",
        (
            f"当日可卖：{result['sellable_quantity']:.0f}股"
            if result.get("sellable_quantity") is not None
            else "当日可卖：未核验，做T只作条件观察"
        ),
        f"技术状态：{result['state_label']}｜评分 {result['score']:+.0f}｜量比 {volume_text}",
        f"支撑：{result['support']:.2f}｜压力：{result['resistance']:.2f}｜失效位：{stop_text}",
        "",
        "二、策略建议",
        f"结论：{result['action']}",
        result["strategy"],
        "",
        "三、梯度补仓计划",
        (
            f"目标达成：{'可达到目标' if result.get('target_reached') else '受约束后未达到目标'}｜"
            f"预计成交后平均成本：{result['projected_average_cost']:.4f}元"
        ),
    ]
    capital_limits = result.get("capital_limits") or {}
    max_add_shares = capital_limits.get("max_add_shares")
    max_text = "不限制" if max_add_shares == math.inf else f"{float(max_add_shares):.0f}股"
    lines.append(
        f"资金/仓位约束：最多可补{max_text}｜"
        f"总资金 {capital_limits.get('total_capital') or '未配置'}｜"
        f"可用资金 {capital_limits.get('available_capital') or '未配置'}｜"
        f"最大仓位 {capital_limits.get('max_position_pct') or '未配置'}%"
    )
    if capital_limits.get("available_capital_source"):
        lines.append(f"可用资金口径：{capital_limits['available_capital_source']}")
    for item in result.get("buy_ladder") or []:
        lines.append(
            f"第{item['level']}档｜触发 {item['trigger_price']:.2f}｜"
            f"买入 {item['shares']:.0f}股｜金额 {item['amount']:.2f}元｜"
            f"成交后平均成本 {item['after_average_cost']:.4f}元"
        )
    if not result.get("buy_ladder"):
        lines.append("当前没有满足价格、失效位和资金约束的补仓档位。")
    for warning in capital_limits.get("warnings") or []:
        lines.append(f"约束提示：{warning}")
    t_plan = result.get("t_plan")
    if t_plan:
        lines.extend([
            "",
            "四、冲高做T执行计划",
            f"可卖仓位：{t_plan['sellable_quantity']:.0f}股｜计划做T：{t_plan['total_sell_shares']:.0f}股",
            "卖出梯度：",
        ])
        for item in t_plan.get("sell_batches") or []:
            lines.append(
                f"第{item['level']}批｜触发 {item['trigger_price']:.2f}｜"
                f"卖出 {item['shares']:.0f}股｜{item['condition']}"
            )
        if t_plan.get("buyback_price") is not None:
            lines.append(f"接回参考：{t_plan['buyback_price']:.2f}元")
        lines.extend([
            f"接回条件：{t_plan['buyback_condition']}",
            f"失效处理：{t_plan['invalidation_rule']}",
            "",
            "成本口径：",
            f"预计做T已实现盈亏：{t_plan['estimated_realized_pnl']:+.2f}元",
            f"费用口径：{t_plan['fee_note']}",
            f"账面平均成本：接回后约 {t_plan['estimated_rebuy_average_cost']:.4f}元",
            f"做T后实际净成本：约 {t_plan['estimated_net_cost_per_share']:.4f}元/股",
            "实际净成本用于反映做T收益，不等同于券商账面持仓成本。",
        ])
    lines.extend([
        "",
        "说明：目标持仓成本按各档成交价和输入费用估算，未计入滑点、涨跌停和后续价格变化；",
        "策略只提供条件式参考，不自动下单，也不保证一定回到目标成本。",
        "⚠️ 市场有风险，投资需谨慎，不构成投资建议。",
    ])
    return "\n".join(lines)
