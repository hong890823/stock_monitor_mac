#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""持仓监控的本地分析逻辑，不负责网络请求或 UI。"""

from datetime import datetime

from deep_loss_policy import (
    deep_loss_threshold_pct,
    floating_pnl_pct,
    is_deep_loss,
    suggested_add_quantity,
)
from entry_structure import EntryInput, evaluate as evaluate_entry
from continuation_hold_policy import format_continuation_hold_info
from fundamental_health import tendency as health_tendency
from intraday_tactics import IntradayTactics, limit_pct_for_board
from intraday_trade_policy import PositionEligibility
from situation_notes import situation_notes
from trade_action_guard import (
    allows_exit_directive,
    guard_actionable_copy,
    guard_intraday_signal,
    position_cost_share,
)
from volume_price import VolumePrice, tag as vp_tag


_tactics = IntradayTactics()

def _add_note(price, quantity, total_capital, available_capital, initial_entry_pct, max_position_pct):
    add_qty = suggested_add_quantity(
        price=price,
        holding_quantity=quantity,
        total_capital=total_capital,
        available_capital=available_capital,
        initial_entry_pct=initial_entry_pct,
        max_position_pct=max_position_pct,
    )
    if add_qty > 0:
        return f"约{add_qty:.0f}股"
    if total_capital <= 0:
        return "未填总资金，不给具体股数"
    if 0 < available_capital < price * 100:
        return "可用资金不足一手，先补资金或减少数量"
    return "数量不足一手（补仓口径本就减半），按整体情况小仓操作"


def _shallow_break_action(price, support, fundamental_health, high_exit_risk=False):
    """浅亏：不能快速收回再减，不在恐慌 tick 直接喊割。"""
    if high_exit_risk:
        return (
            "股价处20日高位且非深套，趋势已走坏，确认跌破支撑后清仓离场，"
            "执行可分批，不等反抽确认；若选择留底仓，跌破关键位后继续离场。"
        )
    base = "若跌破支撑后不能快速收回，可考虑分批减仓；不建议继续加仓摊平。"
    note = health_tendency(fundamental_health, deep=False)
    return f"{base}｜{note}" if note else base


def _deep_break_action(
    *, price, change_pct, cost, support, resistance, quantity, atr_pct,
    total_capital, available_capital, initial_entry_pct, max_position_pct,
    fundamental_health,
):
    """深套（浮亏 ≤ 波动率缩放阈值）破位：两套剧本，不喊先割一部分。

    基本面健康判断只标哪套剧本优先，不替用户拍板——程序判断得了财务与公告
    可见面，判断不了行业景气与订单。
    """
    pnl = floating_pnl_pct(cost, price)
    pnl_text = f"（浮亏 {pnl:+.1f}%）" if pnl is not None else ""
    res_text = f"压力 {resistance:.2f}" if resistance > 0 else "压力位"
    note = health_tendency(fundamental_health, deep=True) or (
        "基本面暂无数据：先按风险承受能力决定减仓或等待企稳，补充基本面后再判断。"
    )
    add_note = _add_note(
        price, quantity, total_capital, available_capital,
        initial_entry_pct, max_position_pct,
    )
    return (
        f"浮亏较深{pnl_text}，破位处多是恐慌盘，不建议在此割肉摊成本，"
        f"也不要在破位处直接补仓接飞刀。{note} "
        f"①逻辑/基本面已破坏：等反弹到 {res_text} 附近分批离场；"
        f"②逻辑还在：等缩量止跌或重新站回支撑 {support:.2f} 上方再考虑小仓补仓降成本"
        f"（{add_note}），反弹冲高可按盘中「冲高做T」做波段，再创新低则取消补仓计划。"
    )


def evaluate_position(position, technical, live_data, portfolio, fundamental_health=None,
                      leader_hold=False, leader_note=""):
    """根据技术分析、实时行情和持仓配置生成条件式信号。

    fundamental_health 为基本面健康分级（fundamental_health.FundamentalHealth），
    只标倾向不替用户拍板；缺省时文案提示「基本面未分析」。
    leader_hold / leader_note 为板块龙头封板或强承接（见 sector_leaders.LeaderGuard），
    此时乖离过大也不喊做T，改口为持有观察。
    """
    code = position.get("code", "")
    name = position.get("name", "")
    quantity = float(position.get("quantity", 0) or 0)
    cost = float(position.get("cost", 0) or 0)
    price = float(live_data.get("price", technical["price"]))
    total_capital = float(portfolio.get("total_capital", 0) or 0)
    available_capital = float(portfolio.get("available_capital", 0) or 0)
    max_position_pct = float(portfolio.get("max_position_pct", 20) or 20)
    initial_entry_pct = float(portfolio.get("initial_entry_pct", 5) or 5)
    market_value = quantity * price
    position_pct = market_value / total_capital * 100 if total_capital > 0 else None
    support = technical["support"]
    resistance = technical["resistance"]
    score = technical["score"]
    volume_ratio = technical.get("volume_ratio") or 0
    change_pct = technical.get("price_change", 0)
    limit_pct = limit_pct_for_board(code)
    atr_value = technical.get("atr")
    atr_pct = (float(atr_value) / price * 100) if atr_value and price > 0 else None
    deep = is_deep_loss(cost, price, atr_pct)
    recent_trend = (technical.get("entry_structure") or {}).get("recent_trend")
    trend_weak = bool((technical.get("entry_structure") or {}).get("trend_weak"))
    price_position = technical.get("price_position") or {}
    price_location = price_position.get("location_key")
    pnl_known = floating_pnl_pct(cost, price) is not None
    high_exit_risk = (
        pnl_known
        and not deep
        and str(price_location or "").upper() == "HIGH"
        and trend_weak
    )
    # 直接撤出话术的门槛：基本面亮红灯 / 价格处高位 / 仓位不小，三者占一。
    # 基本面未见恶化 + 价格不高 + 仓位小的持仓只提示风险事实（2026-10-22
    # 招商证券：基本面好、位置低位、小仓位却被「不保留确定观察仓」直接劝退）。
    fundamental_level = getattr(fundamental_health, "level", None)
    position_share_pct = position_cost_share(portfolio, quantity, cost)
    exit_directive_ok = allows_exit_directive(
        fundamental_level, price_location, position_share_pct,
    )

    # ── 盘中买卖点信号检测 ──
    intraday_signal = _tactics.check_signal(
        code=code, name=name, price=price, change_pct=change_pct,
        volume_ratio=volume_ratio if volume_ratio > 0 else None,
        limit_pct=limit_pct, support=support, resistance=resistance,
        score=score, quantity=quantity,
        cost=cost, atr_pct=atr_pct,
        total_capital=total_capital, available_capital=available_capital,
        initial_entry_pct=initial_entry_pct, max_position_pct=max_position_pct,
        fundamental_health=fundamental_health,
        recent_trend=recent_trend,
        trend_weak=trend_weak,
        price_location=price_location,
        leader_hold=leader_hold,
        leader_note=leader_note,
    )

    # ── 量价信号标签 ──
    vp_ctx = technical.get("price_position")
    if vp_ctx:
        vp_context = {
            "location": vp_ctx.get("location_key", "MID"),
            "rebound_from_low": vp_ctx.get("rebound_from_low", 0.0),
        }
    else:
        vp_context = None
    vp_signal = VolumePrice.of(volume_ratio if volume_ratio > 0 else None, change_pct, vp_context)
    vp_tag_text = vp_tag(vp_signal)

    # ── 今日情境分句：让同一批持仓的应对话术分开 ──
    # 只产出观察、不产出动作；动作仍由下面的分支给。
    situation = situation_notes(
        vp_signal=vp_signal,
        relative_strength=technical.get("relative_strength"),
        atr_pct=atr_pct,
        floating_pct=floating_pnl_pct(cost, price),
        deep_threshold=deep_loss_threshold_pct(atr_pct),
        location=(technical.get("price_position") or {}).get("location_key"),
    )

    # ── 入场结构（三态）：复用技术分析已算好的结果，避免两套口径 ──
    entry_info = technical.get("entry_structure") or {}
    continuation_hold = (
        technical.get("continuation_hold")
        or entry_info.get("continuation_hold")
        or {}
    )
    trade_permissions = entry_info.get("trade_permissions") or {}
    entry_state = entry_info.get("state", "NO_STRUCTURE")
    entry_can_buy = bool(entry_info.get("can_buy")) and bool(trade_permissions.get("allow_normal_buy", True))
    trend_weak = bool(entry_info.get("trend_weak"))
    bias_pct = float(entry_info.get("bias_pct", 0.0) or 0.0)
    prior_high = float(entry_info.get("prior_high", resistance) or resistance)
    stop_price = entry_info.get("stop_price")
    invalid_rule = entry_info.get("invalid_rule", "")
    entry_label = entry_info.get("state_label", "暂未形成有效入场结构")
    entry_reasons = entry_info.get("reasons") or []
    entry_blockers = entry_info.get("blockers") or []

    # 入场结构缺失时退回旧口径（历史结果缓存可能没有该字段）
    # 注意：即使旧口径识别出突破，也不给买入结论——与 entry_structure 的降级保持一致
    if not entry_info:
        entry_state = "BREAKOUT" if (resistance > 0 and price > resistance and volume_ratio >= 1.2) else "NO_STRUCTURE"
        entry_can_buy = False

    near_support = support > 0 and support * 0.97 <= price <= support * 1.03

    # ── 买卖点：持仓 / 无持仓分开，避免对持仓者发出「追高买入」 ──
    if quantity > 0:
        # 破位是风险事件，口径优先于「趋势转弱」的通用观察话术（深套股几乎都跌破 M20，
        # 否则剧本永远被转弱分支盖住）；转弱信息作为前缀补上。
        if support > 0 and price < support * (1 - 0.005):
            signal = "减仓风险"
            break_action = (
                _deep_break_action(
                    price=price, change_pct=change_pct, cost=cost, support=support,
                    resistance=resistance, quantity=quantity, atr_pct=atr_pct,
                    total_capital=total_capital, available_capital=available_capital,
                    initial_entry_pct=initial_entry_pct,
                    max_position_pct=max_position_pct,
                    fundamental_health=fundamental_health,
                )
                if deep
                else _shallow_break_action(
                    price=price, support=support,
                    fundamental_health=fundamental_health,
                    high_exit_risk=high_exit_risk,
                )
            )
            action = (
                ("已有效跌破 M20、趋势转弱，不加仓、不摊平。" + break_action)
                if trend_weak
                else break_action
            )
        elif high_exit_risk:
            signal = "减仓风险"
            action = (
                "股价处20日高位且非深套，趋势已走坏，建议准备清仓；"
                f"确认跌破支撑 {support:.2f} 后清仓离场，执行可分批，不等反抽确认。"
            )
        elif continuation_hold.get("state") in {"BREAK_DEFENSE", "DISTRIBUTION_RISK"}:
            signal = "减仓风险"
            action = format_continuation_hold_info(continuation_hold, exit_directive_ok)
        elif continuation_hold.get("state") == "PROFIT_PROTECTION":
            signal = "持有/止盈观察"
            action = format_continuation_hold_info(continuation_hold, exit_directive_ok)
        elif continuation_hold.get("state") in {
            "HOLD_CONTINUATION", "SECOND_WAVE_WATCH", "SECOND_WAVE_CONFIRMED",
        }:
            signal = "持有观察"
            action = format_continuation_hold_info(continuation_hold, exit_directive_ok)
        elif trend_weak:
            signal = "趋势转弱观察"
            action = (
                f"有效跌破 M20，趋势转弱观察：不加仓、不摊平"
                + (f"，止损参考 {stop_price:.2f}" if stop_price else "")
                + "；重新站回 M20 并企稳前不恢复加仓，连续两轮确认后再提示减仓。"
            )
        elif bias_pct >= 8:
            # 乖离过大时提示做 T，比「继续持有」更可执行；突破状态下做 T 也不冲突（减出部分、留底仓）
            # 板块龙头封板/强承接时改口持有观察——跟风冲高延续概率更高，别在这儿机械减仓
            if leader_hold:
                signal = "持有观察"
                action = (
                    f"价格偏离 M20 达 {bias_pct:+.1f}%，乖离过大"
                    + ("且量能放大" if volume_ratio and volume_ratio >= 1.3 else "")
                    + f"；但{leader_note or '板块龙头封板'}，暂缓做T、保留底仓观察延续。"
                    "龙头转弱后会提醒你恢复做T。"
                )
            else:
                signal = "冲高做T"
                action = (
                    f"价格偏离 M20 达 {bias_pct:+.1f}%，乖离过大"
                    + ("且量能放大" if volume_ratio and volume_ratio >= 1.3 else "")
                    + "，可考虑分批做 T（约减出 1/3 仓位），回踩 M20 附近再接回。"
                )
        elif entry_state == "BREAKOUT":
            signal = "持有观察"
            action = (
                f"已突破前高 {prior_high:.2f}，继续持有观察，不追高加仓"
                + (f"；失效位 {stop_price:.2f}，跌回即离场" if stop_price else "")
                + "。"
            )
        else:
            signal = "持有观察"
            action = "暂不做激进操作，观察 M20 支撑、前高压力和成交量是否继续配合。"
    else:
        # 无持仓：只有「回踩上行 M20」保留买入提示。
        # 突破结构已降级为观察信号（回测显示负超额），不再作为买入确认。
        if entry_state == "PULLBACK_M20" and entry_can_buy:
            signal = "买入观察"
            action = (
                "回踩上行 M20 并出现止跌迹象，入场结构成立"
                + (f"；失效位 {stop_price:.2f}" if stop_price else "")
                + f"，可等待企稳确认后考虑初始仓位约{initial_entry_pct:.0f}%。"
            )
        elif entry_state == "BREAKOUT":
            signal = "等待"
            action = (
                f"突破前高 {prior_high:.2f} 结构成立，但历史回测显示该信号跑输无条件买入，"
                f"仅作观察、不作为买入依据"
                + (f"；失效位 {stop_price:.2f} 仅供参考" if stop_price else "")
                + "。若要介入，等回踩上行 M20 企稳更稳妥。"
            )
        elif entry_state == "PULLBACK_M20":
            # 结构成立但被大盘过滤降级——不要说成「无结构」
            signal = "等待"
            action = (
                "回踩上行 M20 结构成立，但大盘（指数）位于其 M20 下方，"
                "个股信号降级为仅观察，暂不提示买入；等大盘转强或个股出现更明确确认再看。"
            )
        elif near_support:
            # 接近支撑/M20 但结构未成立（M20 下行、未止跌、无量）→ 明确不提示买入
            signal = "等待"
            action = (
                "接近支撑但暂未形成有效入场结构"
                + (f"（{'；'.join(entry_blockers[:2])}）" if entry_blockers else "")
                + "，不提示买入；等 M20 走平转上并出现止跌确认再看。"
            )
        else:
            signal = "等待"
            action = "暂未形成有效入场结构，不主动介入；等待回踩上行 M20 企稳。"

    # 动作 = 决策框架 + 今日情境分句。分开存：
    # - action_core 是框架本身（报告「应对摘要」取它的首句）
    # - situation_notes 是区分 12 只的关键（量价/相对强弱/波动率）
    # - action 是合成后的完整话术，下游（旧 dump、通知）直接用它
    action_core = action
    if situation:
        action = f"{action_core} 今日情境：{'；'.join(situation)}。"

    result = {
        "code": code,
        "name": name,
        "quantity": quantity,
        "cost": cost,
        "price": price,
        "market_value": market_value,
        "position_pct": position_pct,
        "signal": signal,
        "action": action,
        "action_core": action_core,
        "situation_notes": situation,
        "support": support,
        "resistance": resistance,
        "score": score,
        "volume_ratio": volume_ratio,
        "volume_price_tag": vp_tag_text,
        # 透传给组合聚合与盘中信号：
        # - benchmark_ok 是大盘过滤有没有把这只股的买入信号降级（组合过滤影响面要数它）
        # - price_change 让下游不用反查 prev_close 也能拿到今日涨跌幅
        # - atr 修 _check_intraday_signals 的空读 —— 它读 result.get("atr")，
        #   在这之前 result 里根本没有这个键，深套阈值静默退回固定 -20%，
        #   和 evaluate_position 自己用的 ATR 缩放阈值不一致（2026-10-12）
        "benchmark_ok": entry_info.get("benchmark_ok"),
        "price_change": change_pct,
        "trade_date": str(live_data.get("date") or "")[:10] or None,
        "atr": technical.get("atr"),
        # 直接撤出话术门槛的上下文（trade_action_guard.allows_exit_directive）：
        # 基本面/价格位置/仓位三者占一才允许「清仓离场、不保留观察仓」类话术。
        "fundamental_level": fundamental_level,
        "position_share_pct": position_share_pct,
        "exit_directive_ok": exit_directive_ok,
        # 数据日期如实透传：日线停在远古（停牌/退市/数据源给错）时，
        # 下面的技术位全是旧的，报告必须说出来而不是当今天的分析卖。
        "data_as_of": technical.get("data_as_of"),
        "data_age_days": technical.get("data_age_days"),
        "opening": technical.get("opening", {}),
        "volatility": technical.get("volatility", {}),
        "trend_strength": technical.get("trend_strength", {}),
        "relative_strength": technical.get("relative_strength"),
        "price_position": technical.get("price_position"),
        "entry_state": entry_state,
        "entry_label": entry_label,
        "entry_reasons": entry_reasons,
        "entry_blockers": entry_blockers,
        "entry_stop_price": stop_price,
        "entry_invalid_rule": invalid_rule,
        "entry_can_buy": entry_can_buy,
        "trade_permissions": trade_permissions,
        "trend_weak": trend_weak,
        "ma20": entry_info.get("ma20", technical.get("ma20")),
        "ma20_prev": entry_info.get("ma20_prev"),
        "closes_below_ma20": entry_info.get("closes_below_ma20", 0),
        "entry_structure": entry_info,  # 原始字典，供盘中信号检测取 M20 上下文
        "continuation_hold": continuation_hold,
        "continuation_state": continuation_hold.get("state"),
        "recent_trend": recent_trend,
        "analyzed_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    if intraday_signal:
        result["intraday_signal"] = {
            "type": intraday_signal.signal_type.value,
            "title": intraday_signal.title,
            "body": intraday_signal.body,
            "notification_body": intraday_signal.notification_body or intraday_signal.body,
        }
    return result


def _signal_meaning(signal):
    meanings = {
        "买入确认": "保留给未来可能的更高置信结构；当前突破已降级，不产生买入确认。",
        "买入观察": "回踩上行 M20 并出现止跌迹象，等企稳确认，不代表立即买入。",
        "持有观察": "当前没有明确的加仓或减仓信号，继续观察 M20、前高和量能。",
        "持有/止盈观察": "趋势出现改善或突破，持有同时防范短线过热。",
        "冲高做T": "乖离偏大，仅作观察，等待15/30分钟确认；不直接执行减仓或接回。",
        "趋势转弱观察": "有效跌破 M20，趋势转弱观察：不加仓不摊平，但不等同于立即清仓。",
        "减仓风险": "高位趋势走坏或价格跌破风险支撑，仅提示风险事实；是否处置等待盘中确认和T+1核验。",
        "等待": "暂未形成有效入场结构，不主动介入。",
    }
    return meanings.get(signal, "条件式技术信号，仅供观察。")


def format_portfolio_report(results, portfolio, analyzed_at=None):
    """格式化持仓监控缓存报告。"""
    analyzed_at = analyzed_at or datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        "📦 持仓信息 / 盘中技术监控",
        f"最近分析：{analyzed_at}",
        "",
    ]
    if not results:
        lines.append("尚未配置监控股票，或当前没有可用分析结果。")
    for result in results:
        position_text = "未持仓观察"
        if result["quantity"] > 0:
            position_text = f"持仓{result['quantity']:.0f}股"
            if result["cost"] > 0:
                position_text += f"，成本{result['cost']:.2f}"
        pct_text = ""
        if result["position_pct"] is not None:
            pct_text = f"，占总资金{result['position_pct']:.1f}%"
        kind = (
            "risk_exit"
            if result.get("signal") == "减仓风险"
            else "t_sell"
            if result.get("signal") == "冲高做T"
            else "buy"
            if result.get("signal") == "买入观察"
            else "hold"
        )
        guarded_action = guard_actionable_copy(
            kind,
            result["action"],
            PositionEligibility(
                total_quantity=int(result.get("quantity") or 0),
                sellable_quantity=0,
                buy_capacity_quantity=0,
            ),
            evidence=None,
            trade_date=result.get("trade_date") or result.get("data_as_of"),
        )
        lines.extend([
            f"{result['name']}（{result['code']}）",
            f"当前价 {result['price']:.2f}｜{position_text}{pct_text}",
            f"状态：{result['signal']}｜评分 {result['score']:+d}",
            f"状态含义：{_signal_meaning(result['signal'])}",
            f"支撑 {result['support']:.2f}｜压力 {result['resistance']:.2f}｜量比 {result['volume_ratio']:.2f}",
        ])
        entry_label = result.get("entry_label")
        if entry_label:
            lines.append(f"入场结构：{entry_label}")
        entry_reasons = result.get("entry_reasons") or []
        if entry_reasons:
            lines.append("  依据：" + "；".join(entry_reasons[:3]))
        entry_blockers = result.get("entry_blockers") or []
        if entry_blockers:
            lines.append("  未满足：" + "；".join(entry_blockers[:3]))
        if result.get("entry_stop_price"):
            lines.append(f"  失效位：{result['entry_stop_price']:.2f}｜{result.get('entry_invalid_rule', '')}")
        vp_tag_text = result.get("volume_price_tag", "")
        price_pos = result.get("price_position")
        pos_text = f"  位置：{price_pos['location']}" if price_pos else ""
        if vp_tag_text or pos_text:
            lines.append(f"量价：{vp_tag_text}{pos_text}")
        intraday = result.get("intraday_signal")
        if intraday:
            lines.append(f"🔔 盘中信号：{intraday['title']}")
            guarded_intraday = guard_intraday_signal(
                intraday.get("type"),
                f"{intraday.get('title') or ''} {intraday.get('body') or ''}",
                PositionEligibility(
                    total_quantity=int(result.get("quantity") or 0),
                    sellable_quantity=int(result.get("sellable_quantity") or 0),
                    today_locked_quantity=int(result.get("today_locked_quantity") or 0),
                    buy_capacity_quantity=0,
                ),
                evidence=None,
                trade_date=result.get("trade_date") or result.get("data_as_of"),
                invalidation_price=result.get("support") or 0.0,
            )
            lines.append(f"   {guarded_intraday.text}")
        lines.extend([
            "指标含义：评分综合趋势、动量和量价，不是上涨概率；量比高于1表示成交活跃。",
        ])
        opening = result.get("opening") or {}
        if opening:
            lines.append(
                f"开盘：昨收{opening.get('previous_close', 0):.2f} "
                f"今开{opening.get('open', 0):.2f} "
                f"跳空{opening.get('gap_pct', 0):+.2f}%｜{opening.get('structure', '')}"
            )
        volatility = result.get("volatility") or {}
        trend_strength = result.get("trend_strength") or {}
        if volatility.get("atr") is not None:
            adx = trend_strength.get("adx")
            adx_text = f"｜ADX{adx:.1f}" if adx is not None else ""
            lines.append(f"ATR {volatility['atr']:.2f}（{volatility.get('atr_pct', 0):.2f}%）{adx_text}")
        relative_strength = result.get("relative_strength")
        if relative_strength is not None:
            lines.append(f"近20日相对沪指：{relative_strength:+.2f}个百分点")
        lines.extend([
            f"应对：{guarded_action.text}",
            "",
        ])
    lines.extend([
        "说明：本报告只提供条件式技术信号，不自动下单；未持仓股票的建议不考虑个人成本。",
        "⚠️ 市场有风险，投资需谨慎，不构成投资建议。",
    ])
    return "\n".join(lines)
