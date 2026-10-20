#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""入场结构与择时：把「突破前高 / 回踩 M20」落成可执行的三态规则。

设计原则（相对口诀的三点收紧）：
1. **三态输出**：只在条件齐备时给出「可考虑」，其余一律输出「暂未形成有效入场结构」，
   不做模棱两可的点评。
2. **失败模式写进规则**：无量突破、跌破 M20 后的反抽、突破后快速回落都算失效，
   而不是等亏了才发现。
3. **每个结构都带失效定义**：给出止损参考位，否则只筛胜率不筛盈亏比。

参数全部集中在下方常量，便于后续回测调整；调用方不要各自拍脑袋定阈值。

只做本地标量计算，不依赖 pandas / 网络 / UI，方便单测。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Sequence


# ── 参数（可调） ─────────────────────────────────────────────────────────────

VOLUME_EXPAND_RATIO = 1.5     # 入场放量标准（比信号阈值 1.3 更严：入场要更强确认）
VOLUME_SHRINK_RATIO = 0.9     # 回踩缩量标准：量比低于此值视为缩量
MA20_SLOPE_DAYS = 5           # 判断 M20 方向的回看天数
MA20_FLAT_EPS = 0.001         # M20 斜率容差（相对幅度），低于此值视为走平
PULLBACK_NEAR_PCT = 0.025     # 「回踩到 M20 附近」的容差
INVALID_BREAK_PCT = 0.02      # 有效跌破的幅度容差
INVALID_CONFIRM_DAYS = 2      # 有效跌破需连续跌破的交易日数
BELOW_TOLERANCE = 0.005       # 「收在 M20 下方」的容差（0.5%，与 Android 版统一）
BIAS_FAR_PCT = 0.08           # 乖离过大阈值（高于 M20 超过该比例不再追）
BREAKOUT_HOLD_DAYS = 2        # 突破后需在前高上方维持的交易日数
LOOKBACK_CLOSES = 5           # 统计「近期是否站上前高/跌破 M20」的回看窗口

# 止损改用 ATR 自适应（2026-09-30 回测结论）：
# 固定 2% 的失效位实际被打了 73.9%，且跳空把实际亏损放大到 −5%；
# 而 2% 是相对前高算的，突破后入场价已在前高之上，真实风险被系统性低估。
ATR_PERIOD = 14               # ATR 计算周期
ATR_STOP_MULT = 1.5           # 止损距离 = 入场价 − 1.5 × ATR14

# 「放量突破前高」在 2022-07~2026-08 的三个独立股票宇宙上均为负超额
# （−1.42 / −0.73 / −0.67 pp，放量阈值越高越差），故降级为观察信号，不再作为买入触发。
BREAKOUT_CAN_BUY = False
BREAKOUT_NOTE = "历史回测显示该信号跑输无条件买入，仅作观察，非买入依据"


# ── 枚举 ────────────────────────────────────────────────────────────────────

class EntryState(Enum):
    """入场结构三态。"""
    BREAKOUT = "放量突破前高"
    PULLBACK_M20 = "回踩上行M20"
    NO_STRUCTURE = "暂未形成有效入场结构"


class Ma20Slope(Enum):
    """M20 方向。"""
    UP = "上行"
    FLAT = "走平"
    DOWN = "下行"


class VolumeState(Enum):
    """量能状态。"""
    EXPAND = "放量"
    SHRINK = "缩量"
    NEUTRAL = "中性"
    UNKNOWN = "暂缺"


@dataclass(frozen=True)
class RecentTrendContext:
    change_3d_pct: Optional[float] = None
    change_5d_pct: Optional[float] = None
    up_days_3d: Optional[int] = None
    up_days_5d: Optional[int] = None
    closes_above_ma20_5d: Optional[int] = None
    volume_3d_vs_20d: Optional[float] = None
    closes_above_prior_high_5d: int = 0


# ── 输入 ────────────────────────────────────────────────────────────────────

@dataclass
class EntryInput:
    """入场结构判断所需的标量输入。

    所有字段都可由日线+实时行情直接算出，调用方负责归一化。
    """
    price: float                          # 现价（或最新收盘）
    prior_high: float                     # 前高（不含当日的近 N 日最高价）
    ma20: float                           # 当前 MA20
    ma20_prev: float                      # MA20_SLOPE_DAYS 日前的 MA20
    volume_ratio: Optional[float]         # 量比（对 5 日均量；None 表示暂缺）
    closes_above_prior_high: int = 0      # 近 LOOKBACK_CLOSES 日收盘站上前高的天数
    closes_below_ma20: int = 0            # 连续收盘跌破自身 MA20 的天数（自最新往回数）
    today_change_pct: float = 0.0         # 当日涨跌幅（%）
    holding: bool = False                 # 是否已有持仓
    benchmark_ok: Optional[bool] = None   # 大盘是否位于其 M20 上方；None=未知
    atr: Optional[float] = None           # ATR14；None 时由 compute_entry_input 计算
    recent_trend: Optional[RecentTrendContext] = None


# ── 输出 ────────────────────────────────────────────────────────────────────

@dataclass
class EntryStructure:
    """入场结构判断结果。"""
    state: EntryState
    state_label: str                 # 与 state.value 一致，便于直接拼进报告
    reasons: List[str] = field(default_factory=list)   # 成立依据
    blockers: List[str] = field(default_factory=list)  # 未成立 / 失效原因
    ma20_slope: Ma20Slope = Ma20Slope.FLAT
    volume_state: VolumeState = VolumeState.UNKNOWN
    bias_pct: float = 0.0            # 乖离率：(price - ma20) / ma20 * 100
    bias_state: str = "正常"
    trend_weak: bool = False         # 有效跌破 M20（趋势转弱观察）
    can_buy: bool = False            # 是否构成可考虑买入的状态
    stop_price: Optional[float] = None   # 失效 / 止损参考位
    invalid_rule: str = ""
    action: str = ""

    @property
    def state_text(self) -> str:
        return self.state_label


# ── 内部：状态归类 ──────────────────────────────────────────────────────────

def _classify_ma20_slope(now: float, prev: float) -> Ma20Slope:
    if now <= 0 or prev <= 0:
        return Ma20Slope.FLAT
    change = (now - prev) / prev
    if change > MA20_FLAT_EPS:
        return Ma20Slope.UP
    if change < -MA20_FLAT_EPS:
        return Ma20Slope.DOWN
    return Ma20Slope.FLAT


def _classify_volume(volume_ratio: Optional[float]) -> VolumeState:
    if volume_ratio is None or volume_ratio <= 0:
        return VolumeState.UNKNOWN
    if volume_ratio >= VOLUME_EXPAND_RATIO:
        return VolumeState.EXPAND
    if volume_ratio <= VOLUME_SHRINK_RATIO:
        return VolumeState.SHRINK
    return VolumeState.NEUTRAL


def _classify_bias(price: float, ma20: float) -> tuple:
    if ma20 <= 0 or price <= 0:
        return 0.0, "正常"
    bias = (price - ma20) / ma20 * 100
    if bias >= BIAS_FAR_PCT * 100:
        return bias, "乖离过大"
    if bias <= -BIAS_FAR_PCT * 100:
        return bias, "深跌乖离"
    return bias, "正常"


def _atr_stop(price: float, atr: Optional[float]) -> Optional[float]:
    """ATR 自适应止损位：入场价 − ATR_STOP_MULT × ATR14。

    相对固定百分比的好处是随波动率缩放——高波动股（创业板）不会被轻易洗出，
    低波动股也不会留过大风险敞口。回测显示固定 2% 的触发率高达 73.9%，
    且跳空把实际亏损放大到 −5%（因为 2% 是相对前高算的，而突破后入场价已在前高之上）。
    """
    if not atr or atr <= 0 or price <= 0:
        return None
    stop = price - ATR_STOP_MULT * atr
    return stop if stop > 0 else None


# ── 主入口 ──────────────────────────────────────────────────────────────────

def compute_entry_input(
    closes: Sequence[float],
    highs: Sequence[float],
    lows: Sequence[float],
    volumes: Sequence[float],
    *,
    price: Optional[float] = None,
    volume_ratio: Optional[float] = None,
    today_change_pct: Optional[float] = None,
    holding: bool = False,
    benchmark_ok: Optional[bool] = None,
    atr: Optional[float] = None,
) -> Optional[EntryInput]:
    """从日线序列算出入场结构输入。

    这是「前高 / M20 方向 / 连续跌破天数」的**唯一口径**：
    `stock_analysis.analyze_history` 与历史回测都必须走这里，
    否则回测测的是另一套规则。

    一切统计只使用序列中已有的数据，不含未来函数；
    调用方负责把序列截断到「当前能看到的最后一根 K 线」。

    Args:
        closes/highs/lows/volumes: 日线序列，按时间升序，末位是当前这根 K 线。
        price: 现价；None 时用最后一根收盘价（回测里就是当日收盘）。
        volume_ratio: 量比；None 时用「末根成交量 ÷ 前 20 日均量」。
        today_change_pct: 当日涨跌幅%；None 时用最后两根收盘价算。
        holding: 是否已持仓。
        benchmark_ok: 大盘是否位于其 MA20 上方；None=未知。

    Returns:
        EntryInput；序列太短时返回 None。
    """
    if closes is None or len(closes) < 25:
        return None

    n = len(closes)
    if not (len(highs) == len(lows) == len(volumes) == n):
        raise ValueError("compute_entry_input: OHLCV 序列长度不一致")

    # 只取尾部窗口做计算：回测里这个函数会被调用上万次，
    # 每次全量转换整个序列会把复杂度推到 O(n²)。
    # streak 回溯是唯一的例外，但它通常只走几步。
    tail = max(30, ATR_PERIOD + 10)   # ATR 需要足够窗口收敛
    c = [float(v) for v in closes[-tail:]]
    h = [float(v) for v in highs[-tail:]]
    l = [float(v) for v in lows[-tail:]]
    v = [float(v) for v in volumes[-tail:]]
    m = len(c)

    # ── 现价 ──
    if price is None:
        price = c[-1]

    # ── 当日涨跌幅 ──
    if today_change_pct is None:
        if m >= 2 and c[-2] > 0:
            today_change_pct = (c[-1] / c[-2] - 1) * 100
        else:
            today_change_pct = 0.0

    # ── 前高：不含当日的近 20 日最高价 ──
    prior_window = h[:-1][-20:] if m > 1 else h[-20:]
    prior_high = max(prior_window) if prior_window else h[-1]

    # ── MA20 及其 5 日前值（判方向）──
    ma20 = sum(c[-20:]) / 20
    if m >= 25:
        ma20_prev = sum(c[-25:-5]) / 20
    else:
        ma20_prev = ma20

    # ── 近 5 日收盘站上前高的天数 ──
    closes_above_prior_high = sum(1 for value in c[-5:] if value > prior_high)

    # ── 连续收盘跌破自身 MA20 的天数（自最新往回数）──
    # 「收在 M20 下方」用 0.5% 容差：贴着 M20 下方 0.1% 震两天不该算「有效跌破」，
    # 否则回踩信号会被同一条规则压掉（与 Android 版统一口径）。
    closes_below_ma20 = 0
    for index in range(m - 1, 19, -1):
        ref = sum(c[index - 19:index + 1]) / 20
        if c[index] < ref * (1 - BELOW_TOLERANCE):
            closes_below_ma20 += 1
        else:
            break

    # ── 量比：默认对前 20 日均量（不含当日）──
    if volume_ratio is None:
        prior_volumes = [x for x in (v[:-1][-20:] if m > 1 else v[-20:]) if x > 0]
        if prior_volumes and v[-1] > 0:
            volume_ratio = v[-1] / (sum(prior_volumes) / len(prior_volumes))

    # ── ATR14（Wilder 平滑），供止损距离用 ──
    if atr is None:
        atr = _wilder_atr(h, l, c, ATR_PERIOD)

    recent_values = c[:-1] + [price] if m > 1 else [price]

    def change_over(days: int) -> Optional[float]:
        if len(recent_values) <= days:
            return None
        start = recent_values[-days - 1]
        return (recent_values[-1] / start - 1) * 100 if start > 0 else None

    def up_days(days: int) -> Optional[int]:
        if len(recent_values) <= days:
            return None
        changes = [
            recent_values[index] - recent_values[index - 1]
            for index in range(len(recent_values) - days, len(recent_values))
        ]
        return sum(1 for value in changes if value > 0)

    closes_above_ma20_5d = 0
    for index in range(max(19, len(recent_values) - 5), len(recent_values)):
        reference = sum(recent_values[index - 19:index + 1]) / 20
        if recent_values[index] > reference * (1 + BELOW_TOLERANCE):
            closes_above_ma20_5d += 1

    recent_volumes = [value for value in v[-3:] if value > 0]
    prior_volumes = [value for value in v[:-3][-20:] if value > 0]
    volume_3d_vs_20d = (
        (sum(recent_volumes) / len(recent_volumes)) /
        (sum(prior_volumes) / len(prior_volumes))
        if recent_volumes and prior_volumes
        else None
    )

    return EntryInput(
        price=price,
        prior_high=prior_high,
        ma20=ma20,
        ma20_prev=ma20_prev,
        volume_ratio=volume_ratio,
        closes_above_prior_high=closes_above_prior_high,
        closes_below_ma20=closes_below_ma20,
        today_change_pct=today_change_pct,
        holding=holding,
        benchmark_ok=benchmark_ok,
        atr=atr,
        recent_trend=RecentTrendContext(
            change_3d_pct=change_over(3),
            change_5d_pct=change_over(5),
            up_days_3d=up_days(3),
            up_days_5d=up_days(5),
            closes_above_ma20_5d=closes_above_ma20_5d,
            volume_3d_vs_20d=volume_3d_vs_20d,
            closes_above_prior_high_5d=closes_above_prior_high,
        ),
    )


def _wilder_atr(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = ATR_PERIOD,
) -> Optional[float]:
    """Wilder ATR（与 stock_analysis._calc_indicators 的 alpha=1/period 同口径）。"""
    n = len(closes)
    if n < 2 or len(highs) != n or len(lows) != n:
        return None
    trs = []
    for i in range(1, n):
        trs.append(max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        ))
    if not trs:
        return None
    # 指数平滑 alpha = 1/period，与 stock_analysis 保持一致
    alpha = 1.0 / period
    value = trs[0]
    for tr in trs[1:]:
        value = alpha * tr + (1 - alpha) * value
    return value

def evaluate(entry: EntryInput) -> EntryStructure:
    """判断当前处于哪一种入场结构，并给出失效定义。"""
    price = float(entry.price or 0)
    prior_high = float(entry.prior_high or 0)
    ma20 = float(entry.ma20 or 0)
    slope = _classify_ma20_slope(ma20, float(entry.ma20_prev or 0))
    volume_state = _classify_volume(entry.volume_ratio)
    bias_pct, bias_state = _classify_bias(price, ma20)

    reasons: List[str] = []
    blockers: List[str] = []

    # ── 有效跌破 M20：趋势转弱（与入场结构分开，持仓侧用） ──
    trend_weak = False
    if ma20 > 0 and price > 0:
        if price < ma20 * (1 - INVALID_BREAK_PCT):
            trend_weak = True
        elif entry.closes_below_ma20 >= INVALID_CONFIRM_DAYS:
            trend_weak = True

    above_prior_high = prior_high > 0 and price > prior_high
    near_ma20 = ma20 > 0 and abs(price - ma20) / ma20 <= PULLBACK_NEAR_PCT

    state = EntryState.NO_STRUCTURE

    # ── 0) 失败模式优先：这两种形态直接判无结构，不给入场结论 ──────────
    # 「突破前高后快速回落」：近几日曾站上前高，现已跌回 → 突破失败
    if entry.closes_above_prior_high > 0 and not above_prior_high and prior_high > 0:
        blockers.append("突破前高后快速回落，突破失败")
        if slope == Ma20Slope.DOWN:
            blockers.append("M20 仍在下行，回落压力更大")

    # 「跌破 M20 后只是反抽」：已连续跌破 M20，今日的小幅回升不算重新站稳
    if entry.closes_below_ma20 >= INVALID_CONFIRM_DAYS and ma20 > 0 and price < ma20:
        blockers.append("跌破 M20 后只是反抽，未重新站稳")

    if blockers:
        # 失败模式成立时不进入形态判断，直接落到「暂未形成有效入场结构」
        if entry.benchmark_ok is False:
            blockers.append("大盘（指数）位于其 M20 下方，个股信号可信度下降，仅作观察")
        stop_price = None
        invalid_rule = ""
        if trend_weak:
            stop_price = _atr_stop(price, entry.atr)
            if stop_price is None:
                stop_price = ma20 * (1 - INVALID_BREAK_PCT) if ma20 > 0 else None
            invalid_rule = "趋势转弱观察：不宜加仓摊平，重新站回 M20 并企稳前不恢复买入"
        action = (
            "已有效跌破 M20，趋势转弱观察：不加仓、不接飞刀，等重新站回 M20 再看。"
            if trend_weak
            else "暂未形成有效入场结构，不介入；等待放量突破前高或回踩上行 M20。"
        )
        return EntryStructure(
            state=EntryState.NO_STRUCTURE,
            state_label=EntryState.NO_STRUCTURE.value,
            reasons=reasons,
            blockers=blockers,
            ma20_slope=slope,
            volume_state=volume_state,
            bias_pct=bias_pct,
            bias_state=bias_state,
            trend_weak=trend_weak,
            can_buy=False,
            stop_price=stop_price,
            invalid_rule=invalid_rule,
            action=action,
        )

    # ── 1) 放量突破前高 ────────────────────────────────────────────────
    if above_prior_high:
        ok_volume = volume_state == VolumeState.EXPAND
        ok_slope = slope != Ma20Slope.DOWN
        ok_hold = entry.closes_above_prior_high >= 1  # 当下仍在前高上方

        if ok_volume and ok_slope and ok_hold:
            state = EntryState.BREAKOUT
            reasons.append(f"股价有效站上前期高点 {prior_high:.2f}")
            reasons.append(f"成交量达放量标准（量比 {entry.volume_ratio:.2f} ≥ {VOLUME_EXPAND_RATIO}）")
            reasons.append(f"M20 {slope.value}，未明显向下")
        else:
            if not ok_volume:
                if entry.volume_ratio is None:
                    blockers.append("量比暂缺，无法确认是否放量")
                else:
                    blockers.append(
                        f"无量突破（量比 {entry.volume_ratio:.2f} < {VOLUME_EXPAND_RATIO}），"
                        "突破可靠性不足"
                    )
            if not ok_slope:
                blockers.append("M20 仍在下行，突破容易变成下跌中继")
            if not ok_hold:
                blockers.append("未能站稳前高上方")

    # ── 2) 回踩上行 M20 ───────────────────────────────────────────────
    elif near_ma20:
        ok_slope = slope == Ma20Slope.UP
        ok_volume = volume_state == VolumeState.SHRINK
        ok_not_broken = not trend_weak
        ok_stabilize = price >= ma20 or entry.today_change_pct > 0

        if ok_slope and ok_volume and ok_not_broken and ok_stabilize:
            state = EntryState.PULLBACK_M20
            reasons.append(f"M20 {slope.value}（{ma20:.2f}）")
            reasons.append(f"股价回落到 M20 附近（乖离 {bias_pct:+.2f}%）")
            reasons.append(f"回踩过程缩量（量比 {entry.volume_ratio:.2f}）")
            reasons.append("未有效跌破 M20")
            if price >= ma20:
                reasons.append("已重新站回 M20 上方")
            else:
                reasons.append("回踩当日收红，出现止跌迹象")
        else:
            if not ok_slope:
                blockers.append(f"M20 当前{slope.value}，回踩不构成支撑")
            if not ok_volume:
                if volume_state == VolumeState.UNKNOWN:
                    blockers.append("量比暂缺，无法确认回踩是否缩量")
                else:
                    blockers.append(
                        f"回踩未缩量（量比 {entry.volume_ratio:.2f} > {VOLUME_SHRINK_RATIO}），"
                        "可能是主动抛压"
                    )
            if not ok_not_broken:
                blockers.append("已有效跌破 M20，回踩不成立")
            if not ok_stabilize:
                blockers.append("尚无止跌迹象（未站回 M20 且当日收绿）")

    # ── 3) 未形成结构 ─────────────────────────────────────────────────
    else:
        if slope == Ma20Slope.DOWN:
            blockers.append("M20 下行，趋势方向不支持入场")
        if prior_high > 0 and price < prior_high and volume_state == VolumeState.EXPAND:
            blockers.append("放量但未突破前高，方向不明")
        if not blockers:
            blockers.append("价格既不在前高上方，也未回落到 M20 附近")

    # ── 大盘过滤：指数在 M20 下方时降级为「仅观察」 ──
    if entry.benchmark_ok is False and state != EntryState.NO_STRUCTURE:
        blockers.append("大盘（指数）位于其 M20 下方，个股信号可信度下降，仅作观察")

    # ── can_buy：只有「回踩上行 M20」保留买入触发 ──
    # 突破结构已降级为观察信号（见 BREAKOUT_CAN_BUY 注释）
    if state == EntryState.BREAKOUT:
        if BREAKOUT_NOTE not in blockers:
            blockers.append(BREAKOUT_NOTE)
    can_buy = (
        state == EntryState.PULLBACK_M20
        and entry.benchmark_ok is not False
    )

    # ── 失效定义 / 止损参考：ATR 自适应，不再是固定百分比 ──
    atr_value = entry.atr
    stop_price = _atr_stop(price, atr_value)
    stop_kind = f"入场价 − {ATR_STOP_MULT}×ATR14（ATR {atr_value:.2f}）" if atr_value else None

    if state == EntryState.BREAKOUT:
        if stop_kind:
            invalid_rule = (
                f"跌破 {stop_kind}（约 {stop_price:.2f}），"
                f"或 {BREAKOUT_HOLD_DAYS} 日内跌回前高 {prior_high:.2f} 下方，突破失效"
            )
        else:
            stop_price = prior_high * (1 - INVALID_BREAK_PCT) if prior_high > 0 else None
            invalid_rule = (
                f"跌破前高 {prior_high:.2f} 下方 {INVALID_BREAK_PCT * 100:.0f}%"
                f"（约 {stop_price:.2f}）或 {BREAKOUT_HOLD_DAYS} 日内跌回前高下方，突破失效"
                if stop_price else "跌回前高下方即突破失效"
            )
    elif state == EntryState.PULLBACK_M20:
        if stop_kind:
            invalid_rule = (
                f"跌破 {stop_kind}（约 {stop_price:.2f}），"
                f"或有效跌破 M20 {ma20:.2f}（连续 {INVALID_CONFIRM_DAYS} 日收于下方），回踩失效"
            )
        else:
            stop_price = ma20 * (1 - INVALID_BREAK_PCT) if ma20 > 0 else None
            invalid_rule = (
                f"有效跌破 M20（连续 {INVALID_CONFIRM_DAYS} 日收于 {ma20:.2f} 下方，"
                f"或单日跌破 {INVALID_BREAK_PCT * 100:.0f}% 至约 {stop_price:.2f}），回踩失效"
                if stop_price else "有效跌破 M20 即回踩失效"
            )
    elif trend_weak:
        if not stop_kind:
            stop_price = ma20 * (1 - INVALID_BREAK_PCT) if ma20 > 0 else None
        invalid_rule = "趋势转弱观察：不宜加仓摊平，重新站回 M20 并企稳前不恢复买入"
    else:
        # 无结构时不给止损位——没有入场就没有离场计划
        stop_price = None
        invalid_rule = ""

    # ── 一句话建议 ──
    if state == EntryState.BREAKOUT:
        action = (
            "突破结构成立，但历史回测显示该信号跑输无条件买入，仅作观察，非买入依据"
            + ("；大盘偏弱时更不宜据此介入。" if entry.benchmark_ok is False else "。")
        )
    elif state == EntryState.PULLBACK_M20 and can_buy:
        action = (
            f"回踩上行 M20 且缩量企稳，属可考虑买入状态；分批观察，"
            f"跌破 {stop_price:.2f} 或有效跌破 M20 离场。"
            if stop_price else "回踩上行 M20 且缩量企稳，属可考虑买入状态；分批观察，有效跌破 M20 离场。"
        )
    elif state == EntryState.PULLBACK_M20 and not can_buy:
        action = "回踩结构成立但大盘偏弱，适合等待止跌后分批观察，不急于介入。"
    elif trend_weak:
        action = "已有效跌破 M20，趋势转弱观察：不加仓、不接飞刀，等重新站回 M20 再看。"
    else:
        action = "暂未形成有效入场结构，不介入；等待放量突破前高或回踩上行 M20。"

    return EntryStructure(
        state=state,
        state_label=state.value,
        reasons=reasons,
        blockers=blockers,
        ma20_slope=slope,
        volume_state=volume_state,
        bias_pct=bias_pct,
        bias_state=bias_state,
        trend_weak=trend_weak,
        can_buy=can_buy,
        stop_price=stop_price,
        invalid_rule=invalid_rule,
        action=action,
    )


# ── 基本面 × 技术位置 组合判断 ──────────────────────────────────────────────

class FundamentalBucket(Enum):
    """基本面档位，由财务质量评分/风险映射而来。"""
    GOOD = "较好"
    NEUTRAL = "中性"
    WEAK = "偏弱"
    DETERIORATING = "恶化"


def combine_fundamental_and_technical(
    bucket: FundamentalBucket,
    entry: EntryStructure,
) -> str:
    """基本面结论 + 当前技术位置 → 一句话买点判断。

    基本面报告不再只回答「公司值不值得研究」，还要回答「当前技术位置是否适合考虑」。
    """
    state = entry.state
    if bucket == FundamentalBucket.GOOD:
        if state == EntryState.BREAKOUT:
            return (
                "基本面较好，且出现放量突破前高——业绩逻辑与技术形态一度共振，"
                "但历史回测显示突破信号跑输无条件买入，故只作观察、不作为买点；"
                "若真要介入，等回踩上行 M20 企稳再考虑，比追突破的期望值更高。"
            )
        if state == EntryState.PULLBACK_M20:
            return (
                "基本面较好，股价回踩上行 M20——更适合等待止跌确认后分批观察，"
                "不必抢在回踩过程中一次性建仓。"
            )
        return (
            "基本面较好，但技术面没有入场结构——公司可能不错，当前不是理想买点，"
            "先把标的放进观察清单，等回踩上行 M20 结构出现。"
        )

    if bucket == FundamentalBucket.NEUTRAL:
        if state == EntryState.BREAKOUT:
            return (
                "基本面中性，技术面出现突破——突破不构成买入依据（回测跑输），"
                "业绩端也没有安全垫，双向都缺确认，暂不介入。"
            )
        if state == EntryState.PULLBACK_M20:
            return (
                "基本面中性，回踩上行 M20——可作技术性观察，"
                "建议同时跟踪下一份财报能否把基本面档位抬上去。"
            )
        return "基本面中性且技术面无入场结构，暂不介入，双向都缺确认。"

    if bucket == FundamentalBucket.WEAK:
        if state == EntryState.BREAKOUT:
            return (
                "基本面偏弱且出现突破——突破本身已不是买入依据，叠加基本面偏弱，"
                "不建议参与；不要把技术反弹包装成中长期买入逻辑。"
            )
        if state == EntryState.PULLBACK_M20:
            return (
                "基本面偏弱，即使回踩 M20 也缺乏基本面支撑，"
                "不建议主动介入，等基本面出现改善信号再说。"
            )
        return "基本面偏弱且无入场结构，不介入。"

    # DETERIORATING
    if entry.trend_weak or state == EntryState.NO_STRUCTURE:
        return (
            "基本面恶化且价格已跌破 M20 / 无入场结构——风险等级提高，"
            "避免仅因为『价格便宜』而接盘；这类标的优先考虑回避而非抄底。"
        )
    return (
        "基本面恶化，技术面虽有结构但容错极低——不建议参与，"
        "技术反弹难以对冲基本面下行风险。"
    )


def fundamental_bucket_from_score(score: int, has_alert_risk: bool = False) -> FundamentalBucket:
    """把财务质量评分映射成基本面档位。"""
    if has_alert_risk and score < 55:
        return FundamentalBucket.DETERIORATING
    if score >= 70:
        return FundamentalBucket.GOOD
    if score >= 55:
        return FundamentalBucket.NEUTRAL
    if score >= 40:
        return FundamentalBucket.WEAK
    return FundamentalBucket.DETERIORATING


# ── 报告格式化 ──────────────────────────────────────────────────────────────

def format_entry_structure(entry: EntryStructure) -> str:
    """把入场结构格式化成技术分析报告里的一个小节。"""
    lines = [
        f"当前状态：{entry.state_label}",
    ]
    if entry.reasons:
        lines.append("成立依据：" + "；".join(entry.reasons))
    if entry.blockers:
        lines.append("未满足/失效：" + "；".join(entry.blockers))
    lines.append(
        f"M20 方向：{entry.ma20_slope.value}｜量能：{entry.volume_state.value}"
        f"｜乖离：{entry.bias_pct:+.2f}%（{entry.bias_state}）"
    )
    if entry.invalid_rule:
        lines.append(f"失效定义：{entry.invalid_rule}")
    if entry.stop_price:
        lines.append(f"止损参考位：{entry.stop_price:.2f}")
    lines.append(f"应对：{entry.action}")
    return "\n".join(lines)
