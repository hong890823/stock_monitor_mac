#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OBV 量价背离判定：股价创阶段新高/新低时，OBV 有没有同步确认。

经典定义（两种背离）：
1. **顶背离**：股价创阶段新高，但 OBV 高点未同步创出本轮新高 —— 价升量能没跟上，
   上涨靠存量博弈，后劲偏弱，是风险预警；
2. **底背离**：股价创阶段新低，但 OBV 低点未再创新低 —— 下跌中资金不再持续出逃，
   有企稳可能。

注意「OBV 近 N 日净变化为正」只说明区间资金总体流向，**不等于** OBV 跟上了最新股价高点，
所以这里对照的是两个阶段高低点当日的 OBV 值，数字可直接在行情软件里逐点核对。

参数全部集中在下方常量；只做本地标量计算，不依赖 pandas / 网络 / UI，方便单测。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Sequence


# ── 参数（可调） ─────────────────────────────────────────────────────────────

SWING_WINDOW = 2      # 分型判定：±2 根 K 线内最高/最低才算阶段高低点
MIN_PEAK_GAP = 3      # 两个摆动点最小间隔，滤掉毛刺
FORMING_BARS = 3      # 窗口极值出现在最近 3 根内 → 进行中高/低点（确认窗未走完）
LOOKBACK = 120        # 搜寻前后高/低点的窗口
MIN_BARS = 30         # 有效日线不足此数 → 数据不足


# ── 枚举 ────────────────────────────────────────────────────────────────────

class DivergenceState(Enum):
    """量价背离判定结果。"""
    TOP = "顶背离"
    BOTTOM = "底背离"
    SYNC = "量价同步"
    NEUTRAL = "暂无背离"
    CONFLICT = "信号打架"
    INSUFFICIENT = "数据不足"


# ── 输出 ────────────────────────────────────────────────────────────────────

@dataclass
class ObvPoint:
    """阶段高/低点对照点：让用户能在行情软件里逐点核对。

    price/date 是价格高（低）点本身；obv/obv_date 是该段行情的 OBV 峰（谷）值
    ——判定比的是 OBV 自己的峰谷，不是价格高点当日的读数。
    """
    index: int
    date: str
    price: float
    obv: float
    obv_date: str = ""


@dataclass
class ObvDivergence:
    state: DivergenceState
    forming: bool = False                   # 极值是否进行中（确认窗未走完）
    score_delta: int = 0
    prior: Optional[ObvPoint] = None        # 前期高/低点
    current: Optional[ObvPoint] = None      # 本轮高/低点
    obv_confirmed_new_high: Optional[bool] = None   # 本轮 OBV 峰值是否 ≥ 前期峰值
    obv_confirmed_new_low: Optional[bool] = None    # 本轮 OBV 谷值是否 ≤ 前期谷值
    reason: str = ""                        # 判定行
    meaning: str = ""                       # 含义行
    short: str = ""                         # 一行式结论（进摘要）
    method: str = field(default="")         # 口径说明

    @property
    def label(self) -> str:
        return self.state.value


# ── 内部：OBV 与摆动点 ──────────────────────────────────────────────────────

def compute_obv(closes: Sequence[float], volumes: Sequence[float]) -> List[float]:
    """经典累计 OBV：涨加量、跌减量、平盘不变。"""
    obv: List[float] = []
    running = 0.0
    for i in range(len(closes)):
        if i > 0:
            if closes[i] > closes[i - 1]:
                running += float(volumes[i])
            elif closes[i] < closes[i - 1]:
                running -= float(volumes[i])
        obv.append(running)
    return obv


def _swing_points(ext: Sequence[float], is_high: bool, start: int, end: int) -> List[int]:
    """分型极值点（全局下标）：ext[i] 是 [i±SWING_WINDOW] 区间最高/最低，间距 ≥ MIN_PEAK_GAP。"""
    pts: List[int] = []
    for i in range(max(start, SWING_WINDOW), end - SWING_WINDOW):
        seg = ext[i - SWING_WINDOW:i + SWING_WINDOW + 1]
        if is_high:
            if ext[i] != max(seg):
                continue
        else:
            if ext[i] != min(seg):
                continue
        if not pts or i - pts[-1] >= MIN_PEAK_GAP:
            pts.append(i)
        else:
            better = ext[i] > ext[pts[-1]] if is_high else ext[i] < ext[pts[-1]]
            if better:
                pts[-1] = i
    return pts


def _pick_pair(ext, is_high, start, end, now_level):
    """返回 (p1, p2, forming)：p2 = 本轮高/低点，p1 = 其前的上一个高/低点。

    三档出口：
    1. 近 FORMING_BARS 根的极值超过了最近确认摆动点 → 正在创新高/新低，
       p2 取该极值（forming=True，确认窗未走完），p1 = 最近确认摆动点；
    2. 否则 p2 = 最近确认摆动点，p1 = 上一个确认摆动点（经典双高点对照）；
    3. 摆动点不足（如单边行情没有回撤）→ p2 退化为窗口极值，
       p1 退化为 p2 之前的窗口极值；仍不足则返回 None。

    `now_level` 只覆盖末根价格，用于实时价参与「是否创新高」判断；
    确认摆动点仍按日线原始值判定，避免实时价改动历史摆动结构。
    """
    confirmed = _swing_points(ext, is_high, start, end)
    levels = list(ext)
    levels[end - 1] = now_level

    def _extreme(lo, hi):
        # 取 [lo, hi) 的极值下标，并列取更近的
        best = lo
        for i in range(lo + 1, hi):
            if is_high:
                if levels[i] >= levels[best]:
                    best = i
            else:
                if levels[i] <= levels[best]:
                    best = i
        return best

    recent_start = max(start, end - FORMING_BARS)
    r_idx = _extreme(recent_start, end)
    last_swing = confirmed[-1] if confirmed else None
    beyond = (
        last_swing is not None and (
            levels[r_idx] > levels[last_swing] if is_high
            else levels[r_idx] < levels[last_swing]
        )
    )

    if beyond:
        return last_swing, r_idx, True
    if len(confirmed) >= 2:
        return confirmed[-2], confirmed[-1], False

    p2 = _extreme(start, end)
    forming = p2 >= end - FORMING_BARS
    end_prior = p2 - MIN_PEAK_GAP + 1
    p1 = _extreme(start, end_prior) if end_prior > start else None
    return p1, p2, forming


def _point(index: int, price: float, obv_val: float, obv_index: int, dates, n: int) -> ObvPoint:
    def _date(i):
        if dates is not None and i < len(dates):
            return str(dates[i])
        back = n - 1 - i
        return "今日" if back == 0 else ("昨日" if back == 1 else f"{back}个交易日前")

    return ObvPoint(
        index=index, date=_date(index), price=float(price),
        obv=float(obv_val), obv_date=_date(obv_index),
    )


def _leg_obv_extreme(obv, lows, highs, p1, p2, start, n, is_high, swings_opposite):
    """两波行情各自的 OBV 峰（谷）值对照。

    本轮段起点 = 两个价格高（低）点之间的回撤低（反弹高）点；
    前期段起点 = 前一个相反方向摆动点，找不到则用窗口起点。
    返回 (前期值, 前期idx, 本轮值, 本轮idx)；段为空返回 None 四元组。
    """
    if is_high:
        leg2_start = min(range(p1, p2 + 1), key=lambda i: lows[i])
        prior = [p for p in swings_opposite if p < p1]
        leg1_start = prior[-1] if prior else start
        seg1, seg2 = obv[leg1_start:p1 + 1], obv[leg2_start:p2 + 1]
        if not seg1 or not seg2:
            return None, None, None, None
        i1 = leg1_start + seg1.index(max(seg1))
        i2 = leg2_start + seg2.index(max(seg2))
        return max(seg1), i1, max(seg2), i2
    else:
        leg2_start = max(range(p1, p2 + 1), key=lambda i: highs[i])
        prior = [p for p in swings_opposite if p < p1]
        leg1_start = prior[-1] if prior else start
        seg1, seg2 = obv[leg1_start:p1 + 1], obv[leg2_start:p2 + 1]
        if not seg1 or not seg2:
            return None, None, None, None
        i1 = leg1_start + seg1.index(min(seg1))
        i2 = leg2_start + seg2.index(min(seg2))
        return min(seg1), i1, min(seg2), i2


# ── 判定 ────────────────────────────────────────────────────────────────────

def evaluate(
    closes: Sequence[float],
    highs: Sequence[float],
    lows: Sequence[float],
    volumes: Sequence[float],
    dates: Optional[Sequence[str]] = None,
    current_price: Optional[float] = None,
) -> ObvDivergence:
    """判定量价背离。

    Args:
        closes/highs/lows/volumes: 日线序列（升序，末根为最新，可能为盘中未完成bar）
        dates: 与日线对齐的日期字符串（可选，缺省用相对位置标注）
        current_price: 实时价（可选）。仅参与「是否创新高/新低」判断，OBV 对照仍取日线

    Returns:
        ObvDivergence。
    """
    n = len(closes)
    if (n < MIN_BARS or len(highs) != n or len(lows) != n or len(volumes) != n):
        return ObvDivergence(
            state=DivergenceState.INSUFFICIENT,
            reason="数据不足 —— 有效日线不足，无法判定背离",
            meaning="",
            method=_method_text(),
        )

    obv = compute_obv(closes, volumes)
    start = max(0, n - LOOKBACK)

    now_high = float(highs[-1])
    now_low = float(lows[-1])
    if current_price is not None:
        now_high = max(now_high, float(current_price))
        now_low = min(now_low, float(current_price))

    p1h, p2h, forming_h = _pick_pair(highs, True, start, n, now_high)
    p1l, p2l, forming_l = _pick_pair(lows, False, start, n, now_low)

    def _price_at(p, is_high_side):
        if is_high_side and p == n - 1 and current_price is not None:
            return now_high
        if (not is_high_side) and p == n - 1 and current_price is not None:
            return now_low
        return float(highs[p] if is_high_side else lows[p])

    # ── 顶背离：股价创新高，但本轮 OBV 峰值低于前期 OBV 峰值 ──
    # 比的是 OBV 自己的峰（教科书画法），不是价格高点当日的读数
    swings_low = _swing_points(lows, False, start, n)
    swings_high = _swing_points(highs, True, start, n)
    top_div = top_sync = top_new = False
    top_reason = ""
    top_ext = (None, None, None, None)   # 前期OBV峰, idx, 本轮OBV峰, idx
    if p1h is not None and p2h is not None:
        price1, price2 = _price_at(p1h, True), _price_at(p2h, True)
        top_new = price2 > price1
        if top_new:
            v1, i1, v2, i2 = _leg_obv_extreme(obv, lows, highs, p1h, p2h, start, n, True, swings_low)
            top_ext = (v1, i1, v2, i2)
            if v1 is not None and v2 is not None:
                if v2 > v1:
                    top_sync = True
                    top_reason = "量价同步 —— 股价创新高的同时 OBV 峰值同步创新高"
                else:
                    top_div = True
                    top_reason = "顶背离 —— 股价创阶段新高，但本轮 OBV 峰值低于前期 OBV 峰值"

    # ── 底背离：股价创新低，但本轮 OBV 谷值未再创新低 ──
    bot_div = bot_sync = bot_new = False
    bot_reason = ""
    bot_ext = (None, None, None, None)
    if p1l is not None and p2l is not None:
        price1, price2 = _price_at(p1l, False), _price_at(p2l, False)
        bot_new = price2 < price1
        if bot_new:
            v1, i1, v2, i2 = _leg_obv_extreme(obv, lows, highs, p1l, p2l, start, n, False, swings_high)
            bot_ext = (v1, i1, v2, i2)
            if v1 is not None and v2 is not None:
                if v2 < v1:
                    bot_sync = True
                    bot_reason = "量价同步 —— 股价创新低的同时 OBV 谷值同步创新低"
                else:
                    bot_div = True
                    bot_reason = "底背离 —— 股价创阶段新低，但本轮 OBV 谷值未再创新低"

    # ── 核对条件2：OBV 有没有同步创出本轮新高/新低 ──
    obv_new_high = None if top_ext[2] is None else top_ext[2] > top_ext[0]
    obv_new_low = None if bot_ext[2] is None else bot_ext[2] < bot_ext[0]

    # ── 组合判定 ──
    forming = forming_h or forming_l
    prior_pt = current_pt = None
    side_high = True

    if top_div and bot_div:
        state = DivergenceState.CONFLICT
        reason = "信号打架 —— 顶背离与底背离条件同时出现"
        meaning = "多空量价信号矛盾，暂不作方向判断"
        short = "量价背离：信号打架（顶背离与底背离同时出现）"
        score_delta = 0
    elif top_div:
        state = DivergenceState.TOP
        reason = top_reason
        meaning = "价升量能未跟上，上涨主要靠存量博弈，后劲偏弱，属风险预警（不等于立刻见顶）"
        short = "量价背离：顶背离（股价创新高 OBV 未同步）"
        score_delta = -1
        prior_pt, current_pt, side_high = p1h, p2h, True
    elif bot_div:
        state = DivergenceState.BOTTOM
        reason = bot_reason
        meaning = "下跌中资金不再持续出逃，有企稳可能（不等于反转）"
        short = "量价背离：底背离（股价创新低 OBV 未再创新低）"
        score_delta = 1
        prior_pt, current_pt, side_high = p1l, p2l, False
    elif top_sync:
        state = DivergenceState.SYNC
        reason = top_reason
        meaning = "量能配合上涨，暂无顶背离迹象"
        short = "量价背离：量价同步（价涨量能跟上）"
        score_delta = 0
        prior_pt, current_pt, side_high = p1h, p2h, True
    elif bot_sync:
        state = DivergenceState.SYNC
        reason = bot_reason
        meaning = "量价一致走低，暂无底背离迹象"
        short = "量价背离：量价同步（价跌量能跟随）"
        score_delta = 0
        prior_pt, current_pt, side_high = p1l, p2l, False
    else:
        state = DivergenceState.NEUTRAL
        score_delta = 0
        if p1h is None and p1l is None:
            reason = "暂无背离 —— 缺少可比的前期高/低点，无法判定背离"
            meaning = "背离需要两个可比的阶段高/低点；当前不构成判定前提"
        elif not top_new and not bot_new:
            reason = "暂无背离 —— 股价未创阶段新高/新低"
            meaning = "背离只在创新高/新低时才谈；当前不构成判定前提"
        else:
            reason = "暂无背离 —— 量价未出现背离组合"
            meaning = "价与 OBV 同向，暂无背离信号"
        short = "量价背离：暂无背离"

    prior_point = current_point = None
    if prior_pt is not None and current_pt is not None:
        ext = top_ext if side_high else bot_ext
        v1, i1, v2, i2 = ext
        # 对照点显示价格高/低点 + 该段 OBV 峰/谷值（判定用的就是这组数）
        prior_point = _point(
            prior_pt, _price_at(prior_pt, side_high),
            v1 if v1 is not None else obv[prior_pt],
            i1 if i1 is not None else prior_pt, dates, n,
        )
        current_point = _point(
            current_pt, _price_at(current_pt, side_high),
            v2 if v2 is not None else obv[current_pt],
            i2 if i2 is not None else current_pt, dates, n,
        )

    return ObvDivergence(
        state=state,
        forming=forming,
        score_delta=score_delta,
        prior=prior_point,
        current=current_point,
        obv_confirmed_new_high=obv_new_high,
        obv_confirmed_new_low=obv_new_low,
        reason=reason + ("（高点进行中，尚未走完确认窗）" if forming and state not in (
            DivergenceState.INSUFFICIENT, DivergenceState.NEUTRAL) else ""),
        meaning=meaning,
        short=short,
        method=_method_text(),
    )


def _method_text() -> str:
    return (
        f"±{SWING_WINDOW}根K线分型找阶段高低点、{LOOKBACK}日窗口、"
        "对照前后两段行情的OBV峰/谷值；本程序内部口径"
    )
