#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""盘中买卖点 / 做 T 提醒。

每15秒跑一次，只判断「有没有刚刚触发」，不重算任何指标。
触发看的是跃迁而不是状态：只有从「没触发」变成「触发」才算一次，
调用方另外做冷却与每日上限。

阈值按板块涨跌停幅度缩放（主板3%、创业板/科创板6%、北交所9%）。

来源：Android 版 IntradayTactics.kt
"""

from __future__ import annotations
import time
from enum import Enum
from dataclasses import dataclass, field
from collections.abc import Mapping
from typing import Optional

from deep_loss_policy import (
    floating_pnl_pct,
    is_deep_loss,
    suggested_add_quantity,
)
from market_environment import (
    STRONG_TURNOVER_YI,
    MarketEnvironmentStatus,
    evaluate_market_environment,
)


# ── 信号类型 ─────────────────────────────────────────────────────────────────

class IntradaySignalType(Enum):
    """盘中信号类型。"""
    BREAK_SUPPORT = "跌破支撑"
    EXIT_RISK     = "离场风险"
    RECLAIM_SUPPORT = "站回支撑"
    EXIT_WINDOW   = "反弹至压力"
    BREAKOUT      = "放量突破"
    T_SELL        = "冲高做T"
    T_HOLD        = "强势延续观察"
    T_HOLD_LEADER = "龙头封板暂缓"
    T_HOLD_CAUTION = "高成交额谨慎观察"
    T_BUY         = "回踩接回"
    PULLBACK      = "回踩支撑"
    TREND_WEAK    = "趋势转弱观察"
    TRADE_WAIT    = "交易条件观察"
    NEXT_DAY_REVIEW = "T+1次日处置"


# ── 信号 ─────────────────────────────────────────────────────────────────────

@dataclass
class IntradaySignal:
    """一条盘中信号。"""
    signal_type: IntradaySignalType
    title: str
    body: str
    notification_body: str = ""


# ── 常量 ─────────────────────────────────────────────────────────────────────

T_RATIO            = 0.3      # 做T阈值 = 板块涨跌停幅度 × 0.3
T_PART             = 1 / 3    # 建议减出仓位比例
STRONG_MARKET_PCT  = 0.0      # 大盘至少不跌
STRONG_SECTOR_PCT  = 0.0      # 所属板块至少不跌
LOT                = 100      # 一手
BREAK_TOLERANCE    = 0.005    # 跌破支撑容差
PULLBACK_TOLERANCE = 0.02     # 回踩容差
EXIT_NEAR_PCT      = 0.02     # 反弹至压力附近的容差（深套红灯的离场窗口）
COOLDOWN_SECONDS   = 600      # 10分钟冷却
MAX_SIGNALS_PER_DAY = 8       # 每只股票每日最多8次
SHARED_PUSH_MAX_PER_DAY = 8   # 每只股票每日「可操作」提醒合计上限（风险提醒不占）
INITIAL_ENTRY_PCT  = 30       # 初始仓位百分比（突破且未持仓时）
VALID_BREAK_PCT    = 0.02     # 有效跌破 M20 的幅度容差
VALID_BREAK_DAYS   = 2        # 有效跌破 M20 需连续跌破的交易日数
BIAS_FAR_PCT       = 0.08     # 乖离过大阈值（高于 M20 超过该比例提示做T）


def _share_hint(shares: float) -> str:
    if shares > 0:
        return f"约{shares:.0f}股"
    return "少量"


# ── 板块涨跌停幅度 ─────────────────────────────────────────────────────────

def limit_pct_for_board(code: str) -> float:
    """根据股票代码前缀返回涨跌停幅度（%）。"""
    c = code.lower()
    # 北交所
    if c.startswith("bj"):
        return 30.0
    # 创业板 / 科创板
    if c.startswith("sz300") or c.startswith("sz301") or c.startswith("sh688"):
        return 20.0
    # 主板
    return 10.0


def _recent_value(recent_trend, key: str):
    if isinstance(recent_trend, Mapping):
        return recent_trend.get(key)
    return getattr(recent_trend, key, None)


def _recent_has_data(recent_trend) -> bool:
    keys = (
        "change_3d_pct",
        "change_5d_pct",
        "up_days_3d",
        "up_days_5d",
        "closes_above_ma20_5d",
        "volume_3d_vs_20d",
        "closes_above_prior_high_5d",
    )
    return any(_recent_value(recent_trend, key) is not None for key in keys)


def _mapping_value(value, key, default=None):
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _enum_value(value):
    return getattr(value, "value", value)


def _permission_allows(permissions, key):
    if permissions is None:
        return True
    return bool(_mapping_value(permissions, key, False))


def _opportunity_blocker(permissions, trend, high_position, intent):
    major_trend = str(_enum_value(_mapping_value(trend, "major_trend", "")) or "")
    if major_trend in {"DOWN", "大趋势向下"}:
        return "大趋势向下，仅观察，不执行普通买入、接回或做T"

    high_state = str(_enum_value(_mapping_value(high_position, "state", "")) or "").upper()
    if high_state in {
        "MISSING",
        "HIGH_PENDING",
        "DISTRIBUTION_RISK",
        "SECOND_WAVE_CANDIDATE",
    }:
        return "高位结构未确认安全，仅观察，不执行普通买入、接回或做T"

    permission_key = {
        "buy": "allow_normal_buy",
        "add": "allow_add",
        "reentry": "allow_reentry",
        "t": "allow_t",
    }[intent]
    if not _permission_allows(permissions, permission_key):
        blockers = _mapping_value(permissions, "blockers", ()) or ()
        return "；".join(blockers) or "趋势、市场或15/30分钟条件未同时通过，仅观察"
    return None


# ── 盘中策略引擎 ────────────────────────────────────────────────────────────

class IntradayTactics:
    """盘中买卖点 / 做 T 策略引擎。

    每只股票的内部状态：
    - triggered: set[IntradaySignalType]  已触发但尚未解除的信号（跃迁检测）
    - last_signal_time: dict[IntradaySignalType, float]  上次触发时间戳
    - daily_count: dict[IntradaySignalType, int]  当日触发次数
    """

    def __init__(self) -> None:
        # code -> state dict
        self._state: dict[str, dict] = {}

    # ── 需要两轮确认的信号类型 ──────────────────────────────────────────────
    # 破位/高位离场是风险信号，立即触发；其他信号需要连续两轮确认避免假信号
    CONFIRM_REQUIRED = {
        IntradaySignalType.BREAKOUT,      # 放量突破：避免瞬时冲高、假突破
        IntradaySignalType.T_SELL,        # 冲高做T：避免瞬时拉升导致误卖
        IntradaySignalType.T_HOLD,        # 强势环境观察：避免瞬时环境抖动
        IntradaySignalType.T_HOLD_LEADER, # 龙头封板/强承接：避免盘口抖动误抑制
        IntradaySignalType.T_HOLD_CAUTION,  # 高成交额但持续性未确认
        IntradaySignalType.T_BUY,         # 回踩接回：避免价格短暂触及支撑就提示买回
        IntradaySignalType.PULLBACK,      # 回踩支撑：买入建议不紧急，应优先减少误报
        IntradaySignalType.TREND_WEAK,    # 趋势转弱：不等同于立即清仓，先确认再提醒
        IntradaySignalType.RECLAIM_SUPPORT,  # 站回支撑：补仓窗口不紧急，确认站稳再提
        IntradaySignalType.EXIT_WINDOW,      # 反弹至压力：离场窗口同样两轮确认
    }

    # 风险提醒永远发送、不占共享预算；预算只挡机会类提示（做T / 站回 / 离场窗口等）。
    RISK_SIGNALS = {
        IntradaySignalType.BREAK_SUPPORT,
        IntradaySignalType.EXIT_RISK,
    }

    # ── 内部：获取或初始化状态 ────────────────────────────────────────────

    def _ensure_state(self, code: str) -> dict:
        if code not in self._state:
            self._state[code] = {
                "triggered": set(),
                "pending": {},  # {signal_type: first_detected_time}
                "last_signal_time": {},
                "defer_until": {},
                "daily_count": {},
                "shared_count": 0,        # 机会类提醒的每日合计（风险提醒不占）
                "broken_support": False,  # 今日是否跌破过支撑
                "reclaimed_support": False,  # 跌破后是否重新站回过
            }
        return self._state[code]

    # ── 核心评估（无副作用） ─────────────────────────────────────────────

    def evaluate(
        self,
        code: str,
        name: str,
        price: float,
        change_pct: float,
        volume_ratio: float | None,
        limit_pct: float,
        support: float,
        resistance: float,
        score: int,
        quantity: int = 0,
        ma20: float | None = None,
        ma20_prev: float | None = None,
        closes_below_ma20: int = 0,
        cost: float = 0.0,
        atr_pct: float | None = None,
        total_capital: float = 0.0,
        available_capital: float = 0.0,
        initial_entry_pct: float = 5.0,
        max_position_pct: float = 20.0,
        fundamental_health=None,
        broken_support: bool = False,
        reclaimed_support: bool = False,
        predicted_market_turnover: float | None = None,
        market_change_pct: float | None = None,
        sector_change_pct: float | None = None,
        leader_hold: bool = False,
        leader_note: str = "",
        daily_high_price: float | None = None,
        market_index_price: float | None = None,
        market_index_high_price: float | None = None,
        industry_breadth: float | None = None,
        concept_breadth: float | None = None,
        recent_market_turnovers: list[float] | tuple[float, ...] | None = None,
        recent_trend=None,
        trend_weak: bool = False,
        price_location: str | None = None,
        trade_permissions=None,
        trend=None,
        high_position=None,
    ) -> IntradaySignal | None:
        """判断当前行情是否触发信号（不关心跃迁/冷却）。

        ma20 / ma20_prev / closes_below_ma20 为可选的 M20 上下文：
        - ma20_prev 是 MA20_SLOPE_DAYS 日前的 MA20，用于判断 M20 方向
        - closes_below_ma20 是连续收在自身 MA20 下方的天数
        三者都缺省时退回旧口径（只看支撑/压力），兼容旧调用方。

        cost / atr_pct / 资金字段用于深套决策框架与补仓数量；fundamental_health
        为基本面健康分级（见 fundamental_health.py），只标倾向不替用户拍板；
        broken_support / reclaimed_support 由调用方按当日行情维护。
        recent_trend 是近3/5个交易日的趋势、站稳M20天数和量能上下文。
        trend_weak / price_location 用于高位非深套的快速离场风险升级。
        trade_permissions / trend / high_position 只限制普通机会，风险分支优先。
        """
        if price <= 0:
            return None

        expanded = volume_ratio is not None and volume_ratio >= 1.3
        holding = quantity > 0
        t_threshold = limit_pct * T_RATIO
        deep = is_deep_loss(cost, price, atr_pct)
        high_price = str(price_location or "").upper() == "HIGH"
        non_deep_known = floating_pnl_pct(cost, price) is not None and not deep
        high_exit_risk = holding and high_price and non_deep_known and trend_weak
        red = getattr(fundamental_health, "level", None) == "ALERT"

        near_resistance = (
            resistance > 0 and abs(price - resistance) / price < 0.02
        )

        # ── M20 上下文（可选） ──
        ma20_rising = None
        if ma20 is not None and ma20_prev is not None and ma20 > 0 and ma20_prev > 0:
            ma20_rising = ma20 > ma20_prev * (1 + 0.001)
        near_ma20 = ma20 is not None and ma20 > 0 and abs(price - ma20) / ma20 <= PULLBACK_TOLERANCE
        # 有效跌破 M20：单日跌破 2%，或连续 2 日收在下方
        ma20_broken = False
        if ma20 is not None and ma20 > 0:
            if price < ma20 * (1 - VALID_BREAK_PCT):
                ma20_broken = True
            elif closes_below_ma20 >= VALID_BREAK_DAYS:
                ma20_broken = True

        environment = evaluate_market_environment(
            predicted_turnover_yi=predicted_market_turnover,
            market_change_pct=market_change_pct,
            market_price=market_index_price,
            market_high_price=market_index_high_price,
            industry_breadth=industry_breadth,
            concept_breadth=concept_breadth,
            recent_full_day_turnovers_yi=recent_market_turnovers or (),
        )
        stock_near_high = (
            daily_high_price is not None
            and daily_high_price > 0
            and (daily_high_price - price) / daily_high_price < 0.01
        )
        recent_change_5d = _recent_value(recent_trend, "change_5d_pct")
        recent_up_days_5d = _recent_value(recent_trend, "up_days_5d")
        recent_above_ma20_5d = _recent_value(recent_trend, "closes_above_ma20_5d")
        recent_volume_3d = _recent_value(recent_trend, "volume_3d_vs_20d")
        recent_above_prior_high = bool(
            _recent_value(recent_trend, "closes_above_prior_high_5d")
        )
        recent_weak = (
            recent_change_5d is not None
            and recent_change_5d <= 0
            and recent_up_days_5d is not None
            and recent_up_days_5d <= 1
        )
        recent_breakout = (
            recent_above_prior_high
            and (recent_change_5d is None or recent_change_5d >= 0)
        )
        recent_extended = (
            recent_change_5d is not None
            and recent_change_5d > 0
            and recent_up_days_5d is not None
            and recent_up_days_5d >= 3
            and recent_above_ma20_5d is not None
            and recent_above_ma20_5d >= 3
            and (recent_volume_3d is None or recent_volume_3d >= 0.8)
        )
        sector_supportive = sector_change_pct is not None and sector_change_pct >= STRONG_SECTOR_PCT
        strong_market = expanded and stock_near_high and sector_supportive and environment.strong
        cautious_market = expanded and stock_near_high and sector_supportive and environment.cautious

        # 1. 跌破支撑：高位非深套快速离场；其余浅亏「反抽确认」再减；
        # 深套给两套剧本，不喊先割一部分。
        if holding and support > 0 and price < support * (1 - BREAK_TOLERANCE):
            action = self._break_action(
                name=name, price=price, change_pct=change_pct, support=support,
                resistance=resistance, quantity=quantity, cost=cost, atr_pct=atr_pct,
                total_capital=total_capital, available_capital=available_capital,
                initial_entry_pct=initial_entry_pct, max_position_pct=max_position_pct,
                fundamental_health=fundamental_health, deep=deep,
                reclaimed_support=reclaimed_support, high_exit_risk=high_exit_risk,
            )
            if high_exit_risk:
                notification_body = (
                    f"支撑{support:.2f}｜分批清仓，不等反抽"
                )
            elif deep:
                res_action = (
                    f"反弹至压力{resistance:.2f}分批离场"
                    if resistance > 0
                    else "反弹至压力分批离场"
                )
                notification_body = (
                    f"支撑{support:.2f}｜不割肉；{res_action}，站回后小仓补"
                )
            else:
                shares = int(quantity * T_PART / LOT) * LOT
                cut_action = (
                    f"再度破位减约{shares:.0f}股"
                    if reclaimed_support and shares > 0
                    else f"反抽不站回减约{shares:.0f}股"
                    if shares > 0
                    else "反抽不站回再定减仓"
                )
                notification_body = f"支撑{support:.2f}｜{cut_action}"
            return IntradaySignal(
                signal_type=IntradaySignalType.BREAK_SUPPORT,
                title=f"⚠️ {name} 跌破支撑",
                body=action,
                notification_body=notification_body,
            )

        # 1a. 跌破后重新站回支撑：深套剧本②的补仓窗口（红灯则准备离场）
        if (
            holding
            and broken_support
            and support > 0
            and support <= price <= support * (1 + PULLBACK_TOLERANCE)
        ):
            blocker = _opportunity_blocker(
                trade_permissions, trend, high_position, "reentry"
            )
            if blocker:
                return IntradaySignal(
                    signal_type=IntradaySignalType.TRADE_WAIT,
                    title=f"🔎 {name} 站回支撑待确认",
                    body=f"{blocker}；站回支撑仅记录为观察事实。",
                    notification_body=blocker,
                )
            if deep and red:
                notification_body = (
                    f"支撑{support:.2f}｜不补仓；"
                    + (
                        f"反弹至压力{resistance:.2f}分批离场"
                        if resistance > 0
                        else "反弹至压力分批离场"
                    )
                )
            elif deep:
                notification_body = f"支撑{support:.2f}｜小仓补，跌破取消"
            else:
                notification_body = f"支撑{support:.2f}｜企稳后接回"
            return IntradaySignal(
                signal_type=IntradaySignalType.RECLAIM_SUPPORT,
                title=f"📗 {name} 站回支撑",
                body=self._reclaim_action(
                    price=price, change_pct=change_pct, support=support,
                    resistance=resistance, quantity=quantity, cost=cost,
                    total_capital=total_capital, available_capital=available_capital,
                    initial_entry_pct=initial_entry_pct,
                    max_position_pct=max_position_pct, deep=deep, red=red,
                ),
                notification_body=notification_body,
            )

        # 1b. 深套 + 基本面红灯的反弹至压力：剧本①的分批离场窗口
        if (
            holding and deep and red
            and resistance > 0
            and abs(price - resistance) / resistance <= EXIT_NEAR_PCT
        ):
            shares = int(quantity * T_PART / LOT) * LOT
            share_hint = _share_hint(shares)
            t_text = (
                f"若你判断逻辑未变，可按「冲高做T」先减{share_hint}。"
                if shares > 0
                else "若你判断逻辑未变，可按「冲高做T」减少量。"
            )
            return IntradaySignal(
                signal_type=IntradaySignalType.EXIT_WINDOW,
                title=f"🔴 {name} 反弹至压力",
                body=(
                    f"现价{price:.2f}（{change_pct:+.1f}%）反弹至压力{resistance:.2f}附近。"
                    f"剧本①的分批离场窗口：可考虑分批离场（不必割在破位恐慌位）；{t_text}"
                ),
                notification_body=(
                    f"压力{resistance:.2f}｜分批离场"
                    + (f"，先减{share_hint}" if shares > 0 else "")
                ),
            )

        if high_exit_risk:
            return IntradaySignal(
                signal_type=IntradaySignalType.EXIT_RISK,
                title=f"🔴 {name} 高位离场风险",
                body=(
                    f"现价{price:.2f}，股价处20日高位且非深套，趋势已走坏。"
                    "建议准备清仓；确认跌破支撑后清仓离场，执行可分批，"
                    f"不等反抽确认；若选择留底仓，跌破支撑{support:.2f}后继续离场。"
                ),
                notification_body=f"支撑{support:.2f}｜分批清仓，不等反抽",
            )

        # 1b. 趋势转弱观察（有效跌破 M20，不等同于立即清仓）
        if holding and ma20_broken:
            if broken_support and reclaimed_support:
                body = (
                    f"现价{price:.2f}，支撑{support:.2f}已收回但 M20 {ma20:.2f} 未收复；"
                    "暂不加仓，继续观察 M20。"
                )
                notification_body = (
                    f"支撑{support:.2f}已收｜暂不加仓，再看M20"
                )
            elif ma20_rising is False:
                body = (
                    f"现价{price:.2f} 有效跌破 M20 {ma20:.2f}，且 M20 仍在下行；"
                    "暂不加仓，收回 M20 并企稳后再评估。"
                )
                notification_body = f"M20 {ma20:.2f}下行｜暂不加仓"
            else:
                body = (
                    f"现价{price:.2f} 有效跌破 M20 {ma20:.2f}；"
                    "暂不加仓，收回 M20 并企稳后再评估。"
                )
                notification_body = f"低于M20 {ma20:.2f}｜暂不加仓"
            return IntradaySignal(
                signal_type=IntradaySignalType.TREND_WEAK,
                title=f"📉 {name} 趋势转弱观察",
                body=body,
                notification_body=notification_body,
            )

        # 2. 放量突破（仅作观察，非买入依据）
        # 回测（2022-07~2026-08，三个独立宇宙）显示突破信号跑输无条件买入，
        # 且放量阈值越高越差，故不提示买入仓位；M20 下行时更不可靠。
        if resistance > 0 and price > resistance and expanded:
            major_trend = str(
                _enum_value(_mapping_value(trend, "major_trend", "")) or ""
            )
            conflict = str(
                _enum_value(_mapping_value(trend, "conflict", "")) or ""
            )
            if major_trend in {"DOWN", "大趋势向下"}:
                label = (
                    "下跌趋势中的反弹"
                    if conflict in {"DOWN_MAJOR_REBOUND", "下跌趋势中的反弹"}
                    else "下跌趋势中的反抽"
                )
                return IntradaySignal(
                    signal_type=IntradaySignalType.TRADE_WAIT,
                    title=f"🔎 {name} {label}",
                    body=(
                        f"现价{price:.2f}短时突破压力{resistance:.2f}，但大趋势向下，"
                        f"只能识别为{label}；仅观察，不执行普通买入、接回或做T。"
                    ),
                    notification_body=f"{label}｜仅观察，不追涨",
                )
            blocker = _opportunity_blocker(
                trade_permissions, trend, high_position, "buy"
            )
            if blocker and not holding:
                return IntradaySignal(
                    signal_type=IntradaySignalType.TRADE_WAIT,
                    title=f"🔎 {name} 突破条件待确认",
                    body=f"{blocker}；突破仅记录为观察事实，不执行买入。",
                    notification_body=blocker,
                )
            body = f"现价{price:.2f} 突破压力{resistance:.2f}，量比{volume_ratio:.1f}"
            if ma20_rising is False:
                body += "；且 M20 仍在下行，突破可靠性不足"
            if holding:
                body += "。可继续持有观察，不追高"
            else:
                body += "。仅作观察，非买入依据（回测显示跑输无条件买入）；等回踩上行 M20 更稳妥"
            return IntradaySignal(
                signal_type=IntradaySignalType.BREAKOUT,
                title=f"🔥 {name} 放量突破",
                body=body,
                notification_body=(
                    f"压力{resistance:.2f}｜"
                    + (
                        "仅观察；M20下行"
                        if ma20_rising is False
                        else "持有观察，不追高"
                        if holding
                        else "仅观察，等回踩M20"
                    )
                ),
            )

        # 3. 冲高做T（乖离过大时也要提示）
        bias_far = ma20 is not None and ma20 > 0 and (price - ma20) / ma20 >= BIAS_FAR_PCT
        if holding and (
            (change_pct >= t_threshold and (expanded or near_resistance))
            or (bias_far and expanded)
        ):
            blocker = _opportunity_blocker(
                trade_permissions, trend, high_position, "t"
            )
            if blocker and not leader_hold:
                return IntradaySignal(
                    signal_type=IntradaySignalType.TRADE_WAIT,
                    title=f"🔎 {name} 做T条件待确认",
                    body=f"{blocker}；冲高只记录为观察事实，不执行做T。",
                    notification_body=blocker,
                )
            # 板块龙头封板/强承接：跟风冲高延续概率更高，先于一切环境判断暂缓做T。
            # 龙头确认退潮、或当日再炸板，才会恢复（见 sector_leaders.LeaderGuard）。
            if leader_hold:
                label = leader_note or "龙头封板"
                return IntradaySignal(
                    signal_type=IntradaySignalType.T_HOLD_LEADER,
                    title=f"📈 {name} {label}",
                    body=(
                        f"涨幅{change_pct:+.2f}%，{label}：跟风冲高延续概率更高，"
                        "暂缓做T，优先保留底仓观察延续；"
                        "龙头转弱或再炸板后会提醒你恢复做T"
                    ),
                    notification_body=f"{label}｜持有，暂缓做T",
                )
            if recent_breakout and not bias_far and not strong_market:
                return None
            if recent_weak and not bias_far and not near_resistance:
                return None
            if strong_market:
                return IntradaySignal(
                    signal_type=IntradaySignalType.T_HOLD,
                    title=f"📈 {name} 强势环境延续观察",
                    body=(
                        f"涨幅{change_pct:+.2f}%，{environment.summary()}，"
                        f"所属板块{sector_change_pct:+.2f}%且个股接近日高；"
                        "当前未明显冲高回落，暂缓做T，优先保留底仓观察延续；"
                        "若随后回落或跌破关键位再处理"
                    ),
                    notification_body="强势延续｜持有，暂缓做T",
                )
            if cautious_market:
                return IntradaySignal(
                    signal_type=IntradaySignalType.T_HOLD_CAUTION,
                    title=f"📊 {name} 高成交额谨慎观察",
                    body=(
                        f"涨幅{change_pct:+.2f}%，{environment.summary()}，"
                        f"所属板块{sector_change_pct:+.2f}%且个股接近日高；"
                        "先不急于做T，但持续性尚未确认，不把它当成强趋势信号；"
                        "若冲高回落或关键位转弱再重新评估"
                    ),
                    notification_body="量能待确认｜暂缓做T",
                )
            shares = int(quantity * T_PART / LOT) * LOT
            share_hint = _share_hint(shares)
            return IntradaySignal(
                signal_type=IntradaySignalType.T_SELL,
                title=f"📈 {name} 冲高做T",
                body=(
                    f"涨幅{change_pct:+.2f}%，"
                    f"建议减出约1/3仓位（{share_hint}），回踩后再接回"
                    + (
                        f"；近5日{recent_change_5d:+.1f}%且站稳M20 {recent_above_ma20_5d}天"
                        if recent_extended and recent_change_5d is not None
                        and recent_above_ma20_5d is not None
                        else ""
                    )
                ),
                notification_body=(
                    f"减{share_hint}｜回踩接回"
                ),
            )

        # 4. 回踩接回（持仓）
        if (
            holding
            and change_pct <= -t_threshold
            and support > 0
            and support <= price <= support * (1 + PULLBACK_TOLERANCE)
        ):
            blocker = _opportunity_blocker(
                trade_permissions, trend, high_position, "reentry"
            )
            if blocker:
                return IntradaySignal(
                    signal_type=IntradaySignalType.TRADE_WAIT,
                    title=f"🔎 {name} 回踩接回待确认",
                    body=f"{blocker}；回踩只记录为观察事实，不执行接回。",
                    notification_body=blocker,
                )
            if recent_weak:
                return None
            if _recent_has_data(recent_trend) and volume_ratio is not None and volume_ratio > 1.0:
                return None
            return IntradaySignal(
                signal_type=IntradaySignalType.T_BUY,
                title=f"📉 {name} 回踩接回",
                body=(
                    f"回踩支撑{support:.2f}附近，量比回落至{volume_ratio:.1f}，"
                    "可考虑接回之前减出的仓位"
                    if volume_ratio is not None
                    else f"回踩支撑{support:.2f}附近，可考虑接回之前减出的仓位"
                ),
                notification_body=f"支撑{support:.2f}｜接回之前减仓",
            )

        # 5. 回踩上行 M20（未持仓）：M20 下行或未止跌 → 不提示买入
        if (
            not holding
            and score >= 2
            and support > 0
            and support <= price <= support * (1 + PULLBACK_TOLERANCE)
        ):
            blocker = _opportunity_blocker(
                trade_permissions, trend, high_position, "buy"
            )
            if blocker:
                return IntradaySignal(
                    signal_type=IntradaySignalType.TRADE_WAIT,
                    title=f"🔎 {name} 回踩条件待确认",
                    body=f"{blocker}；回踩只记录为观察事实，不执行买入。",
                    notification_body=blocker,
                )
            # M20 上下文可得时严格执行；不可得时退回旧口径
            if ma20_rising is False:
                return None  # M20 下行：不提示买入
            if ma20_broken:
                return None  # 已有效跌破：不提示买入
            if recent_weak:
                return None
            # 止跌迹象：重新站回 M20，或当日收红
            if ma20 is not None and ma20 > 0 and price < ma20 and change_pct <= 0:
                return None  # 未止跌：不提示买入
            body = (
                f"回踩支撑{support:.2f}附近，评分{score}分"
                + ("，M20 上行" if ma20_rising else "")
                + "，可等待止跌确认后分批观察"
            )
            return IntradaySignal(
                signal_type=IntradaySignalType.PULLBACK,
                title=f"📍 {name} 回踩上行M20",
                body=body,
                notification_body=f"支撑{support:.2f}｜等止跌，分批观察",
            )

        return None

    # ── 破位 / 站回的文案 ────────────────────────────────────────────────

    @staticmethod
    def _add_note(
        price: float,
        quantity: float,
        total_capital: float,
        available_capital: float,
        initial_entry_pct: float,
        max_position_pct: float,
    ) -> str:
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
        if 0 < available_capital < price * LOT:
            return "可用资金不足一手，先补资金或减少数量"
        return "数量不足一手（补仓口径本就减半），按整体情况小仓操作"

    def _break_action(
        self, *, name, price, change_pct, support, resistance, quantity, cost,
        atr_pct, total_capital, available_capital, initial_entry_pct,
        max_position_pct, fundamental_health, deep, reclaimed_support,
        high_exit_risk=False,
    ) -> str:
        """浅亏改「反抽确认」；深套给两套剧本，不喊先割一部分。

        浅亏的减仓时机是「反抽站不回支撑」；站回会再提醒（站回支撑），
        反抽失败再度破位才是减仓点。深套看基本面倾向定剧本优先级，不替用户拍板。
        """
        from fundamental_health import tendency as health_tendency

        pnl = floating_pnl_pct(cost, price)
        cost_text = f"，成本{cost:.2f} {pnl:+.1f}%" if pnl is not None else ""
        head = f"现价{price:.2f}（{change_pct:+.1f}%{cost_text}）跌破支撑{support:.2f}。"
        if not deep:
            if high_exit_risk:
                return head + (
                    "股价处20日高位且非深套，趋势已走坏，确认跌破支撑后清仓离场，"
                    "执行可分批，不等反抽确认；若选择留底仓，跌破关键位后继续离场。"
                )
            shares = int(quantity * T_PART / LOT) * LOT
            share_hint = _share_hint(shares)
            if shares > 0:
                if reclaimed_support:
                    cut = (
                        f"反抽站不回支撑{support:.2f}后再度破位：持仓{quantity:.0f}股，"
                        f"可减{share_hint}控风险。"
                    )
                else:
                    cut = (
                        f"破位瞬间多是恐慌盘，先别急着割：反抽站不回{support:.2f}再减{share_hint}"
                        f"控风险（持仓{quantity:.0f}股）；重新站回会再提醒。"
                    )
            else:
                cut = "持仓较少，按整体仓位考虑是否减少量，不必为凑整反而多卖。"
            text = head + cut
            note = health_tendency(fundamental_health, deep=False)
            return f"{text}｜{note}" if note else text

        res_text = f"压力{resistance:.2f}" if resistance > 0 else "压力位"
        note = health_tendency(fundamental_health, deep=True) or (
            "基本面暂无数据：先按风险承受能力决定减仓或等待企稳，补充基本面后再判断。"
        )
        add_note = self._add_note(
            price, quantity, total_capital, available_capital,
            initial_entry_pct, max_position_pct,
        )
        return (
            head
            + "浮亏较深，破位处多是恐慌盘，不建议在此割肉摊成本，也不要在破位处直接补仓接飞刀。"
            + f"{note} ①逻辑/基本面已破坏：等反弹到{res_text}附近分批离场；"
            + f"②逻辑还在：等缩量止跌或重新站回支撑{support:.2f}上方再考虑小仓补仓降成本"
            + f"（{add_note}），反弹冲高可按「冲高做T」做波段，再创新低则取消补仓计划。"
        )

    def _reclaim_action(
        self, *, price, change_pct, support, resistance, quantity, cost,
        total_capital, available_capital, initial_entry_pct,
        max_position_pct, deep, red,
    ) -> str:
        head = f"现价{price:.2f}（{change_pct:+.1f}%）重新站回支撑{support:.2f}。"
        if deep and red:
            res_text = f"压力{resistance:.2f}" if resistance > 0 else "压力位"
            return head + (
                f"基本面亮红灯：站回不补仓，站稳后等反弹到{res_text}按剧本①分批离场；"
                "若你判断逻辑未变再小仓试。"
            )
        if deep:
            add_note = self._add_note(
                price, quantity, total_capital, available_capital,
                initial_entry_pct, max_position_pct,
            )
            return head + (
                f"剧本②的补仓窗口：可考虑小仓补仓降成本（{add_note}），"
                f"跌破{support:.2f}则取消；反弹冲高可按「冲高做T」做波段。"
            )
        return head + "破位风险暂解除；此前若有减仓，企稳后可考虑接回。"

    # ── 带防刷检查的入口 ─────────────────────────────────────────────────

    def defer_signal(
        self,
        code: str,
        signal_type: IntradaySignalType,
        retry_after_seconds: float = COOLDOWN_SECONDS,
    ) -> None:
        """策略层要求等待时释放跃迁状态，到冷却结束后允许重新确认。"""
        state = self._ensure_state(code)
        state["triggered"].discard(signal_type)
        state["pending"].pop(signal_type, None)
        state["last_signal_time"].pop(signal_type, None)
        state["defer_until"][signal_type] = (
            time.time() + max(0.0, float(retry_after_seconds))
        )

    def check_signal(
        self,
        code: str,
        name: str,
        price: float,
        change_pct: float,
        volume_ratio: float | None,
        limit_pct: float,
        support: float,
        resistance: float,
        score: int,
        quantity: int = 0,
        confirmation_rounds: int | None = None,
        ma20: float | None = None,
        ma20_prev: float | None = None,
        closes_below_ma20: int = 0,
        cost: float = 0.0,
        atr_pct: float | None = None,
        total_capital: float = 0.0,
        available_capital: float = 0.0,
        initial_entry_pct: float = 5.0,
        max_position_pct: float = 20.0,
        fundamental_health=None,
        predicted_market_turnover: float | None = None,
        market_change_pct: float | None = None,
        sector_change_pct: float | None = None,
        leader_hold: bool = False,
        leader_note: str = "",
        daily_high_price: float | None = None,
        market_index_price: float | None = None,
        market_index_high_price: float | None = None,
        industry_breadth: float | None = None,
        concept_breadth: float | None = None,
        recent_market_turnovers: list[float] | tuple[float, ...] | None = None,
        recent_trend=None,
        trend_weak: bool = False,
        price_location: str | None = None,
        trade_permissions=None,
        trend=None,
        high_position=None,
    ) -> IntradaySignal | None:
        """评估信号并做跃迁 / 两轮确认 / 冷却 / 每日上限过滤。

        信号触发策略：
        - 跌破支撑：一轮触发，立即提醒（风险信号，不延迟、不占共享预算）
        - 趋势转弱观察（有效跌破 M20）：连续两轮确认后提醒，
          且不等同于立即清仓——只提示「趋势转弱观察」
        - 其他信号：根据 confirmation_rounds 参数决定确认轮数
          - 默认2轮（约30秒）
          - 可通过 signal_confidence 动态调整

        Args:
            confirmation_rounds: 需要的确认轮数（None使用默认值2）
            ma20 / ma20_prev / closes_below_ma20: M20 上下文，缺省时退回旧口径
            cost / atr_pct / 资金字段 / fundamental_health: 深套决策框架的上下文
            trade_permissions / trend / high_position: 原样转发给 evaluate 的趋势策略上下文
        """
        state = self._ensure_state(code)
        now = time.time()

        # 支撑状态机：破过位、之后又站回过，决定「首次破位 / 站回 / 反抽失败」文案。
        if support > 0:
            if price < support * (1 - BREAK_TOLERANCE):
                state["broken_support"] = True
            elif state["broken_support"] and price >= support:
                state["reclaimed_support"] = True

        signal = self.evaluate(
            code, name, price, change_pct, volume_ratio,
            limit_pct, support, resistance, score, quantity,
            ma20=ma20, ma20_prev=ma20_prev, closes_below_ma20=closes_below_ma20,
            cost=cost, atr_pct=atr_pct,
            total_capital=total_capital, available_capital=available_capital,
            initial_entry_pct=initial_entry_pct, max_position_pct=max_position_pct,
            fundamental_health=fundamental_health,
            broken_support=state["broken_support"],
            reclaimed_support=state["reclaimed_support"],
            predicted_market_turnover=predicted_market_turnover,
            market_change_pct=market_change_pct,
            sector_change_pct=sector_change_pct,
            leader_hold=leader_hold,
            leader_note=leader_note,
            daily_high_price=daily_high_price,
            market_index_price=market_index_price,
            market_index_high_price=market_index_high_price,
            industry_breadth=industry_breadth,
            concept_breadth=concept_breadth,
            recent_market_turnovers=recent_market_turnovers,
            recent_trend=recent_trend,
            trend_weak=trend_weak,
            price_location=price_location,
            trade_permissions=trade_permissions,
            trend=trend,
            high_position=high_position,
        )
        if quantity <= 0 and signal is not None and signal.signal_type != IntradaySignalType.PULLBACK:
            signal = None

        # 无信号 → 清除所有已触发状态、待确认状态和冷却（允许后续重新触发）
        if signal is None:
            state["triggered"].clear()
            state["pending"].clear()
            state["last_signal_time"].clear()
            state["defer_until"].clear()
            return None

        st = signal.signal_type

        # evaluate 按优先级只返回一个信号：换了类型说明旧类型的条件已消失，
        # 从 triggered / pending / 冷却里清掉，允许后续重新触发
        # （例如 跌破 → 站回 → 再度破位 必须能再次提醒）。与「条件消失即重新武装」
        # 的原契约一致——只是现在条件消失的那一刻可能被另一个信号占据。
        for other in list(state["triggered"]):
            if other != st:
                state["triggered"].discard(other)
        for other in list(state["pending"]):
            if other != st:
                state["pending"].pop(other, None)
        for other in list(state["last_signal_time"]):
            if other != st:
                state["last_signal_time"].pop(other, None)
        for other in list(state["defer_until"]):
            if other != st:
                state["defer_until"].pop(other, None)

        defer_until = state["defer_until"].get(st)
        if defer_until is not None:
            if now < defer_until:
                return None
            state["defer_until"].pop(st, None)

        # 已经触发过（无跃迁）→ 静默
        if st in state["triggered"]:
            return None

        # 冷却检查
        last = state["last_signal_time"].get(st)
        if last is not None and (now - last) < COOLDOWN_SECONDS:
            return None

        # 每日上限检查
        if state["daily_count"].get(st, 0) >= MAX_SIGNALS_PER_DAY:
            return None

        # 机会类提醒共享每日预算：风险提醒（破位）永远发，预算只挡机会类提示。
        if st not in self.RISK_SIGNALS and state["shared_count"] >= SHARED_PUSH_MAX_PER_DAY:
            return None

        # ── 跌破支撑：一轮触发，立即提醒 ──
        if st == IntradaySignalType.BREAK_SUPPORT:
            state["triggered"].add(st)
            state["pending"].pop(st, None)
            state["last_signal_time"][st] = now
            state["daily_count"][st] = state["daily_count"].get(st, 0) + 1
            return signal

        # ── 其他信号：需要多轮确认 ──
        if st in self.CONFIRM_REQUIRED:
            # 确定需要的确认轮数
            required_rounds = confirmation_rounds if confirmation_rounds is not None else 2
            required_rounds = max(1, required_rounds)  # 至少1轮

            if st not in state["pending"]:
                # 第一轮：记录待确认，设置需要的轮数
                state["pending"][st] = {"start_time": now, "rounds": 1, "required": required_rounds}
                return None
            else:
                # 后续轮次：检查是否达到所需轮数
                pending_info = state["pending"][st]
                pending_info["rounds"] += 1

                if pending_info["rounds"] >= pending_info["required"]:
                    # 达到所需轮数，确认触发
                    state["triggered"].add(st)
                    state["pending"].pop(st)
                    state["last_signal_time"][st] = now
                    state["daily_count"][st] = state["daily_count"].get(st, 0) + 1
                    if st not in self.RISK_SIGNALS:
                        state["shared_count"] += 1
                    return signal
                else:
                    # 还需要更多轮次
                    return None

        # 其他情况（不应到达）
        state["triggered"].add(st)
        state["last_signal_time"][st] = now
        state["daily_count"][st] = state["daily_count"].get(st, 0) + 1
        if st not in self.RISK_SIGNALS:
            state["shared_count"] += 1
        return signal

    # ── 每日重置 ─────────────────────────────────────────────────────────

    def reset_daily(self) -> None:
        """清除所有股票的每日触发计数（开盘时调用）。"""
        for state in self._state.values():
            state["daily_count"].clear()
            state["triggered"].clear()
            state["pending"].clear()
            state["defer_until"].clear()
            state["shared_count"] = 0
            state["broken_support"] = False
            state["reclaimed_support"] = False
