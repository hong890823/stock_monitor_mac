#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""下探回升（V 型收复）提醒。

日内跌出恐慌坑后被买回来——尤其是回升段量能不缩的收复——是「抄底资金」进场的
典型痕迹，也是深套持仓补仓 / 做 T 的观察窗口。

已有的预警只管「跌下去」（market_alert 大盘、个股跌幅预警），这里补上对称的另一半。
触发看**跃迁**不看状态：一段下探只提醒一次，只有创出更深的新低（又一波恐慌）才重新
武装；再叠加每日限次。「资金承接」看回升段成交节奏，不看全天累计——上午恐慌量会把
缩量反弹也抬成「放量收复」。

来源：Android 版 DipReboundEngine.kt
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List, Optional

from trade_action_guard import guard_actionable_copy

# 大盘指数：日内低点较昨收跌到这个幅度才算「下探够深」（上证 4000 点时约 28 点）。
INDEX_DIP_PCT = -0.7

# 创业板波动本来就大，-0.7% 只是日常噪声，阈值更深。
CHINEXT_DIP_PCT = -1.2

# 大盘自低点至少回升这么多（百分点）。
INDEX_REBOUND_PCT = 0.5

# 持仓个股：日内低点较昨收跌到这个幅度才算被砸（无 ATR 时的兜底）。
STOCK_DIP_PCT = -3.0

# 个股下探阈值按 ATR% 缩放的系数：约 1.2 个 ATR 日的砸盘才算被砸。
STOCK_DIP_ATR_MULT = 1.2

# 缩放后的个股下探阈值上下限。
STOCK_DIP_MIN_PCT = -5.0
STOCK_DIP_MAX_PCT = -2.0

# 个股回升门槛 = 下探幅度的这个比例（默认下探 -3% 时正好是 2%）。
STOCK_REBOUND_OF_DIP = 2.0 / 3.0

# 个股自低点至少回升这么多（百分点）；无 ATR 时的兜底。
STOCK_REBOUND_PCT = 2.0

# 无论大盘还是个股，至少收复日内跌幅的这个比例，防止跌 4% 只反弹 0.5% 就喊反转。
RECOVER_RATIO = 0.5

# 「资金承接」的判定：回升段每分钟成交额 ≥ 下跌段的这个倍数。
LEG_PACE_RATIO = 1.1

# 退回累计量比口径时的放量门槛。
VOLUME_CONFIRM_RATIO = 1.3

# 每个代码每天最多提醒几次，防止深 V 来回刷屏。
MAX_PER_DAY = 3


def dip_threshold_pct(code: str, stock: bool, atr_pct: Optional[float] = None) -> float:
    if stock:
        try:
            value = float(atr_pct) if atr_pct is not None else None
        except (TypeError, ValueError):
            value = None
        if value is not None and math.isfinite(value) and value > 0:
            scaled = -STOCK_DIP_ATR_MULT * value
            return max(STOCK_DIP_MIN_PCT, min(STOCK_DIP_MAX_PCT, scaled))
        return STOCK_DIP_PCT
    return CHINEXT_DIP_PCT if code.lower().startswith("sz399006") else INDEX_DIP_PCT


def rebound_threshold_pct(stock: bool, dip_threshold: Optional[float] = None) -> float:
    if not stock:
        return INDEX_REBOUND_PCT
    if dip_threshold is not None and dip_threshold < 0:
        return -dip_threshold * STOCK_REBOUND_OF_DIP
    return STOCK_REBOUND_PCT


@dataclass
class Sample:
    """一轮检测输入。cumulative_turnover 为当日累计成交额（元），
    volume_ratio 为同期累计倍数（只作算不出节奏时的退路）。"""
    code: str
    name: str
    price: float
    change_pct: float
    volume_ratio: Optional[float] = None
    cumulative_turnover: Optional[float] = None
    timestamp: float = 0.0
    atr_pct: Optional[float] = None
    stock: bool = False


@dataclass
class Alert:
    code: str
    title: str
    body: str
    vibrate: bool
    stock: bool


class _State:
    __slots__ = (
        "day_low_pct", "day_low_ts", "day_low_cum",
        "first_ts", "first_cum", "emitted_low_pct", "emitted_count",
    )

    def __init__(self, day_low_pct: float):
        self.day_low_pct = day_low_pct
        self.day_low_ts: Optional[float] = None
        self.day_low_cum: Optional[float] = None
        self.first_ts: Optional[float] = None
        self.first_cum: Optional[float] = None
        self.emitted_low_pct: Optional[float] = None
        self.emitted_count = 0


class DipReboundEngine:
    """下探回升提醒引擎。一段下探只提醒一次，每日限次。"""

    def __init__(self) -> None:
        self._day: Optional[str] = None
        self._states: dict[str, _State] = {}

    def update(self, samples: List[Sample], day: str) -> List[Alert]:
        if self._day != day:
            self._states.clear()
            self._day = day
        alerts: List[Alert] = []
        for sample in samples:
            alert = self._evaluate(sample)
            if alert is not None:
                alerts.append(alert)
        return alerts

    def reset(self) -> None:
        self._day = None
        self._states.clear()

    def _evaluate(self, sample: Sample) -> Optional[Alert]:
        try:
            price = float(sample.price)
            change_pct = float(sample.change_pct)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(price) or price <= 0:
            return None
        if not math.isfinite(change_pct):
            return None
        state = self._states.get(sample.code)
        if state is None:
            state = _State(change_pct)
            self._states[sample.code] = state
        if state.first_ts is None:
            state.first_ts = sample.timestamp
            state.first_cum = sample.cumulative_turnover
        if change_pct < state.day_low_pct:
            state.day_low_pct = change_pct
            state.day_low_ts = sample.timestamp
            state.day_low_cum = sample.cumulative_turnover

        day_low = state.day_low_pct
        dip_threshold = dip_threshold_pct(sample.code, sample.stock, sample.atr_pct)
        if day_low > dip_threshold:
            return None
        recovered = change_pct - day_low
        if recovered < rebound_threshold_pct(sample.stock, dip_threshold):
            return None
        if recovered < (0.0 - day_low) * RECOVER_RATIO:
            return None
        if state.emitted_low_pct is not None and day_low >= state.emitted_low_pct:
            return None
        if state.emitted_count >= MAX_PER_DAY:
            return None
        state.emitted_low_pct = day_low
        state.emitted_count += 1
        leg = self._leg_pace_ratio(sample, state)
        return self._build_alert(sample, day_low, recovered, leg)

    @staticmethod
    def _leg_pace_ratio(sample: Sample, state: _State) -> Optional[float]:
        """回升段每分钟成交额 ÷ 下跌段（今日起点到低点）每分钟成交额。"""
        low_ts = state.day_low_ts
        low_cum = state.day_low_cum
        now_cum = sample.cumulative_turnover
        first_ts = state.first_ts
        if low_ts is None or low_cum is None or now_cum is None or first_ts is None:
            return None
        leg_minutes = (sample.timestamp - low_ts) / 60.0
        before_minutes = (low_ts - first_ts) / 60.0
        if leg_minutes <= 0.5 or before_minutes <= 0.5:
            return None
        leg_pace = max(now_cum - low_cum, 0.0) / leg_minutes
        before_pace = max(low_cum - (state.first_cum or 0.0), 0.0) / before_minutes
        # 没有下跌段基线（如开在低点附近才启动）：当作算不出，退回累计量比口径。
        if before_pace <= 0:
            return None
        return leg_pace / before_pace

    @staticmethod
    def _volume_text(sample: Sample, leg: Optional[float], strong: bool) -> str:
        if leg is not None:
            return (
                f"回升段成交节奏约为下跌段的 {min(leg, 9.9):.1f} 倍，"
                + ("量能不缩" if strong else "量能缩了")
            )
        ratio = sample.volume_ratio
        if sample.stock:
            return f"量比 {ratio:.2f}（累计口径），{'放量回升' if strong else '量能一般'}" if ratio is not None else "量比暂缺"
        return (
            f"两市成交额约为近 20 日同期中位数的 {ratio:.1f} 倍（累计口径），"
            + ("放量收复" if strong else "量能一般")
        ) if ratio is not None else "成交额数据暂缺"

    def _build_alert(self, sample: Sample, day_low: float, recovered: float, leg: Optional[float]) -> Alert:
        if leg is not None:
            strong = leg >= LEG_PACE_RATIO
        else:
            ratio = sample.volume_ratio
            strong = ratio is not None and ratio >= VOLUME_CONFIRM_RATIO
        prev_close = sample.price / (1 + sample.change_pct / 100.0)
        low_price = prev_close * (1 + day_low / 100.0)
        head = (
            f"{sample.name}日内最低 {low_price:.2f}（{day_low:+.1f}%），"
            f"现价 {sample.price:.2f}（{sample.change_pct:+.1f}%）自低点回升 {recovered:.1f}%"
        )
        if sample.stock and strong:
            title = f"💰 {sample.name}·深跌回升（放量）"
            takeaway = (
                "回升段有资金承接迹象——可对照个股剧本②留意补仓/做T窗口；"
                "仍以收回支撑、企稳为准，再创新低则取消。"
            )
        elif sample.stock:
            title = f"📈 {sample.name}·深跌回升"
            takeaway = "回升段量能偏弱，先按超跌反弹观察，补仓仍等企稳确认。"
        elif strong:
            title = f"💰 {sample.name}·下探回升（放量）"
            takeaway = (
                "回升段有资金承接迹象——深套持仓可对照个股剧本②留意补仓/做T窗口"
                "（个股以收回支撑、企稳为准）。"
            )
        else:
            title = f"📈 {sample.name}·下探回升"
            takeaway = "回升段量能偏弱，先按超跌反弹观察；深套持仓若要补仓仍等个股企稳确认。"
        guarded_takeaway = guard_actionable_copy("buy", takeaway, trade_date=None)
        observation_state = "深跌回升" if strong else "超跌反弹"
        body = (
            f"{head}；{self._volume_text(sample, leg, strong)}。"
            f"{guarded_takeaway.text}（观察状态：{observation_state}）"
        )
        return Alert(code=sample.code, title=title, body=body, vibrate=strong, stock=sample.stock)
