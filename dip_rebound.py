#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""下探回升（V 型收复）提醒。

日内跌出恐慌坑后被买回来——尤其是回升段量能不缩的收复——是「抄底资金」进场的
典型痕迹，也是深套持仓补仓 / 做 T 的观察窗口。

已有的预警只管「跌下去」（market_alert 大盘、个股跌幅预警），这里补上对称的另一半。
触发看**跃迁**不看状态：一段下探只提醒一次，只有创出更深的新低（又一波恐慌）才重新
武装；再叠加每日限次。「资金承接」看回升段成交节奏，不看全天累计——上午恐慌量会把
缩量反弹也抬成「放量收复」。

两级信号（仅个股）：回升刚起步就发「反弹观察」低门槛预警（info 级、不振动、文案带
诱多警示与「再创新低即取消」），站稳确认门槛后升级为「深跌回升」。大盘指数维持单级
确认语义不变。防诱多只调措辞不拦截——预警的价值恰恰在提醒「别信」；硬门槛只有三个：
真下探、回升持续（≥2 个采样且 ≥30 秒）、每日限次。再创新低静默作废观察并重新武装
（文案已承诺取消，连环新低发取消通知只会刷屏）。

来源：Android 版 DipReboundEngine.kt
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import List, Optional, Tuple

# 大盘指数：日内低点较昨收跌到这个幅度才算「下探够深」（上证 4000 点时约 28 点）。
INDEX_DIP_PCT = -0.7

# 创业板波动本来就大，-0.7% 只是日常噪声，阈值更深。
CHINEXT_DIP_PCT = -1.2

# 大盘自低点至少回升这么多（百分点）。
INDEX_REBOUND_PCT = 0.5

# 持仓个股：日内低点较昨收跌到这个幅度才算被砸（无 ATR 时的兜底）。
STOCK_DIP_PCT = -3.0

# 个股下探阈值按 ATR% 缩放的系数。缩放只负责把安静股的门槛调浅（-2%~-3%），
# 高波动股无论 ATR 多大都封顶在 -3%（STOCK_DIP_MIN_PCT）：此前按 1.2×ATR 且
# 允许到 -5%，中天科技这类 ATR 5%+ 的票 -3.4% 的坑永远"不够深"，
# 27.4→28.2 的 V 型收复整段漏报（2026-10-22 用户实盘反馈）。
STOCK_DIP_ATR_MULT = 1.2

# 缩放后的个股下探阈值上下限。下限由 -5% 收到 -3%（2026-10-22）：中天科技这类
# 高波动股按 1.2×ATR 会要求 -5% 级别的恐慌坑，-3.4% 下探 + 3% 收复的 V 型被整段
# 漏掉——下探门槛最深与兜底口径对齐，ATR 缩放只负责把安静股的门槛调浅。
STOCK_DIP_MIN_PCT = -3.0
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

# ── 反弹回落跟进（阶段2 之后） ────────────────────────────────────
# 阶段2 报的是「深跌回升」的乐观结论，回升失败时此前没有任何跟进
# （2026-10-22 中天科技 28.20→27.61 回落全程零提醒）。规则：
# 回升幅度过半被回吐（recovered < 0.5 × 本段峰值）且持续一段时间，
# 补一条「反弹回落」事实提醒；每段一次、每日限次，再创新低则重新起段。
FADE_TRIGGER_RATIO = 0.5
FADE_MIN_PEAK_PCT = 1.0   # 回升峰值低于此不谈「回落」（指数小反弹防噪声）
FADE_MIN_SAMPLES = 2
FADE_MIN_HOLD_SEC = 120.0
FADE_MAX_PER_DAY = 2

# ── 阶段一「反弹观察」 ────────────────────────────────────────────
# 观察门槛 = max(WATCH_REBOUND_MIN_PCT, WATCH_REBOUND_OF_DIP × |dip_threshold|)。
# 例：dip 门槛 -3% → 观察 0.75%；-2%（钳制上限）→ 0.5%。
# 阶段二仍是 2/3 门槛（-3% 时 2.0%），阶段一比它早约 1~3 个 15s tick。
WATCH_REBOUND_MIN_PCT = 0.5
WATCH_REBOUND_OF_DIP = 0.25

# 防单笔闪动：连续站上观察门槛 ≥2 个采样，且自首次站上门槛起 ≥30 秒。
WATCH_MIN_SAMPLES = 2
WATCH_MIN_HOLD_SEC = 30.0

# 阶段一频控：每段下探 1 次（状态机保证），每代码每天 2 次。
# 后续下探段没有观察提示、直接等阶段二确认——防跌势里连环刷屏。
WATCH_MAX_PER_DAY = 2

# 急拉警示：自低点起 90 秒内就站上观察门槛 → 措辞警示（不拦截）。
WATCH_SPIKE_SEC = 90.0

# 量能衰减警示：最近 60 秒成交节奏 < 回升全段节奏的 50% → 措辞警示（不拦截）。
WATCH_FADE_WINDOW_SEC = 60.0
WATCH_FADE_RATIO = 0.5

# 抛压警示「放量滞涨」：最近 60 秒成交节奏仍 ≥ 全段节奏的 80%（量还在放），
# 但价格几乎没涨（< 0.15 个百分点）——借反弹出货的典型痕迹。只调措辞 +
# 压住确认振动（抛压重的反弹不叫有效反弹）。
STALL_WINDOW_SEC = 60.0
STALL_MIN_PRICE_PCT = 0.15
STALL_PACE_RATIO = 0.8

# 大盘偏弱：同批次样本中上证 change_pct ≤ -1.0% → 个股提醒加可信度警示。
# 只作用于个股（指数自身的下探回升提醒不加这条，否则它在自我否定）。
MARKET_WEAK_PCT = -1.0

# 趋势标签双口径（与 intraday_tactics 一致：dict 里可能是枚举名也可能是中文 value）。
_TREND_DOWN_LABELS = frozenset({"DOWN", "大趋势向下"})
_CONFLICT_DOWN_REBOUND_LABELS = frozenset({"DOWN_MAJOR_REBOUND", "下跌趋势中的反弹"})


def _opt_float(value) -> Optional[float]:
    """状态恢复用：可缺失/可坏，坏值返回 None。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


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


def watch_threshold_pct(dip_threshold: float) -> float:
    """阶段一观察门槛：自低点回升的百分点。"""
    return max(WATCH_REBOUND_MIN_PCT, WATCH_REBOUND_OF_DIP * abs(dip_threshold))


def trend_down_risk(sample: "Sample") -> bool:
    """大趋势向下 / 下跌趋势中的反弹——只影响措辞与振动，不拦截提醒。"""
    major = str(sample.major_trend or "")
    conflict = str(sample.trend_conflict or "")
    return major in _TREND_DOWN_LABELS or conflict in _CONFLICT_DOWN_REBOUND_LABELS


@dataclass
class Sample:
    """一轮检测输入。cumulative_turnover 为当日累计成交额（元），
    volume_ratio 为同期累计倍数（只作算不出节奏时的退路）。
    day_low_change_pct 为行情快照的日内最低价对昨收涨跌%（可选）：
    采样可能漏掉闪跌低点，或因弹窗/网络造成采样空窗错过坑底，用它锚定真低点。"""
    code: str
    name: str
    price: float
    change_pct: float
    volume_ratio: Optional[float] = None
    cumulative_turnover: Optional[float] = None
    timestamp: float = 0.0
    atr_pct: Optional[float] = None
    stock: bool = False
    # 趋势上下文（可选，由调用方从 entry_structure["trend_assessment"] 透传）。
    major_trend: Optional[str] = None
    trend_conflict: Optional[str] = None
    day_low_change_pct: Optional[float] = None


@dataclass
class Alert:
    code: str
    title: str
    body: str
    vibrate: bool
    stock: bool
    stage: int = 2  # 1=反弹观察, 2=深跌回升确认, 3=反弹回落；默认 2 保持旧调用兼容


class _State:
    __slots__ = (
        "day_low_pct", "day_low_ts", "day_low_cum",
        "first_ts", "first_cum", "emitted_low_pct", "emitted_count",
        "watch_emitted", "watch_count", "watch_probe_ts", "watch_probe_ticks",
        "pace_points",
        "peak_recovered", "fade_emitted", "fade_count", "fade_probe_ts", "fade_probe_ticks",
    )

    def __init__(self, day_low_pct: float):
        self.day_low_pct = day_low_pct
        self.day_low_ts: Optional[float] = None
        self.day_low_cum: Optional[float] = None
        self.first_ts: Optional[float] = None
        self.first_cum: Optional[float] = None
        self.emitted_low_pct: Optional[float] = None
        self.emitted_count = 0
        self.watch_emitted = False
        self.watch_count = 0
        self.watch_probe_ts: Optional[float] = None
        self.watch_probe_ticks = 0
        self.pace_points: deque = deque(maxlen=8)
        # 反弹回落跟进：本段回升峰值、回落判定与防闪动 probe
        self.peak_recovered = 0.0
        self.fade_emitted = False
        self.fade_count = 0
        self.fade_probe_ts: Optional[float] = None
        self.fade_probe_ticks = 0


class DipReboundEngine:
    """下探回升提醒引擎。一段下探只提醒一次，每日限次；个股两级信号。"""

    def __init__(self) -> None:
        self._day: Optional[str] = None
        self._states: dict[str, _State] = {}

    def update(self, samples: List[Sample], day: str) -> List[Alert]:
        if self._day != day:
            self._states.clear()
            self._day = day
        # 同批次预扫上证：个股提醒的「大盘走弱」可信度警示。
        market_change_pct = None
        for sample in samples:
            if sample.code == "sh000001" and not sample.stock:
                try:
                    value = float(sample.change_pct)
                except (TypeError, ValueError):
                    continue
                if math.isfinite(value):
                    market_change_pct = value
                    break
        alerts: List[Alert] = []
        for sample in samples:
            alert = self._evaluate(sample, market_change_pct)
            if alert is not None:
                alerts.append(alert)
        return alerts

    def reset(self) -> None:
        self._day = None
        self._states.clear()

    def export_state(self) -> dict:
        """持久化抑制类状态（重启恢复防重报）；措辞类字段（量能节奏等）不导出。

        2026-10-22 15:52 重启后重报了一次当日已触发的「反弹观察」——
        内存状态随进程消失，发射记录必须落盘。
        """
        return {
            "day": self._day,
            "states": {
                code: {
                    "day_low_pct": s.day_low_pct,
                    "emitted_low_pct": s.emitted_low_pct,
                    "emitted_count": s.emitted_count,
                    "watch_emitted": s.watch_emitted,
                    "watch_count": s.watch_count,
                    "peak_recovered": s.peak_recovered,
                    "fade_emitted": s.fade_emitted,
                    "fade_count": s.fade_count,
                }
                for code, s in self._states.items()
            },
        }

    def restore_state(self, data) -> None:
        """恢复 export_state 的输出；坏数据逐条忽略，不整体报错。"""
        if not isinstance(data, dict):
            return
        day = data.get("day")
        if isinstance(day, str) and day:
            self._day = day
        states = data.get("states")
        if not isinstance(states, dict):
            return
        for code, item in states.items():
            if not isinstance(item, dict):
                continue
            try:
                state = _State(float(item["day_low_pct"]))
                state.emitted_low_pct = _opt_float(item.get("emitted_low_pct"))
                state.emitted_count = int(item.get("emitted_count") or 0)
                state.watch_emitted = bool(item.get("watch_emitted"))
                state.watch_count = int(item.get("watch_count") or 0)
                state.peak_recovered = float(item.get("peak_recovered") or 0.0)
                state.fade_emitted = bool(item.get("fade_emitted"))
                state.fade_count = int(item.get("fade_count") or 0)
            except (TypeError, ValueError, KeyError):
                continue
            self._states[str(code)] = state

    def _evaluate(self, sample: Sample, market_change_pct: Optional[float] = None) -> Optional[Alert]:
        try:
            price = float(sample.price)
            change_pct = float(sample.change_pct)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(price) or price <= 0:
            return None
        if not math.isfinite(change_pct):
            return None
        # 当日低点取「采样到的 change_pct」与「行情快照日低」的更小者：
        # 15s 采样可能漏掉闪跌低点，弹窗卡死/网络断流的空窗期更是整段错过坑底
        # （2026-10-22 中天科技 13:45 见底 27.34 时主线程正被 crash 弹窗冻结），
        # 快照的 low 字段是权威日低，用它锚定才不会把「反弹」误判成「没跌够」。
        observed_low = change_pct
        if sample.day_low_change_pct is not None:
            try:
                quote_low = float(sample.day_low_change_pct)
            except (TypeError, ValueError):
                quote_low = None
            if quote_low is not None and math.isfinite(quote_low) and quote_low < observed_low:
                observed_low = quote_low
        state = self._states.get(sample.code)
        if state is None:
            state = _State(observed_low)
            if observed_low == change_pct:
                # 首个采样即当日低点时也要锚定时间/累计量，否则急拉与节奏都算不出。
                state.day_low_ts = sample.timestamp
                state.day_low_cum = sample.cumulative_turnover
            # 低点来自快照（采样空窗前跌出的坑）：时刻未知，时间/累计量留空，
            # 节奏与急拉文案按「算不出」降级，不瞎猜。
            self._states[sample.code] = state
        if state.first_ts is None:
            state.first_ts = sample.timestamp
            state.first_cum = sample.cumulative_turnover
        if sample.cumulative_turnover is not None:
            state.pace_points.append((sample.timestamp, sample.cumulative_turnover, change_pct))
        if observed_low < state.day_low_pct:
            state.day_low_pct = observed_low
            if observed_low == change_pct:
                state.day_low_ts = sample.timestamp
                state.day_low_cum = sample.cumulative_turnover
            else:
                # 快照口径的新低（采样没踩到坑底）：时刻未知，清锚点让量能
                # 节奏退回累计量比口径，不用旧锚点算出误导性的「放量/缩量」。
                state.day_low_ts = None
                state.day_low_cum = None
            if state.watch_emitted:
                # 再创新低：观察作废、重新武装。静默处理（文案承诺「再创新低即取消」，
                # 连环新低逐条发取消通知只会刷屏），下一段回升重新观察。
                state.watch_emitted = False
                state.watch_probe_ts = None
                state.watch_probe_ticks = 0
            # 新段：回升峰值与回落判定重来（回落跟进是「每段一次」）。
            state.peak_recovered = 0.0
            state.fade_emitted = False
            state.fade_probe_ts = None
            state.fade_probe_ticks = 0

        day_low = state.day_low_pct
        dip_threshold = dip_threshold_pct(sample.code, sample.stock, sample.atr_pct)
        if day_low > dip_threshold:
            return None
        recovered = change_pct - day_low
        if recovered > state.peak_recovered:
            state.peak_recovered = recovered
        episode_done = (
            state.emitted_low_pct is not None and day_low >= state.emitted_low_pct
        )

        # 阶段二：确认（同 tick 强者优先，快 V 可直接确认、跳过观察）。
        if (
            not episode_done
            and state.emitted_count < MAX_PER_DAY
            and recovered >= rebound_threshold_pct(sample.stock, dip_threshold)
            and recovered >= (0.0 - day_low) * RECOVER_RATIO
        ):
            state.emitted_low_pct = day_low
            state.emitted_count += 1
            upgraded = state.watch_emitted
            state.watch_emitted = False
            state.watch_probe_ts = None
            state.watch_probe_ticks = 0
            leg = self._leg_pace_ratio(sample, state)
            flags = self._stage_risk_flags(sample, state, leg, market_change_pct, stage=2)
            return self._build_alert(
                sample, day_low, recovered, leg,
                stage=2, upgraded=upgraded, risk_flags=flags,
                selling_pressure=self._selling_pressure(sample, state),
            )

        # 阶段一：反弹观察（仅个股；指数维持单级语义）。
        if sample.stock and not episode_done and not state.watch_emitted:
            if state.watch_count < WATCH_MAX_PER_DAY and state.emitted_count < MAX_PER_DAY:
                watch_th = watch_threshold_pct(dip_threshold)
                if recovered < watch_th:
                    # 门槛下就清 probe——「跌→等→涨」不能瞬发。
                    state.watch_probe_ts = None
                    state.watch_probe_ticks = 0
                else:
                    state.watch_probe_ticks += 1
                    if state.watch_probe_ts is None:
                        state.watch_probe_ts = sample.timestamp
                    if (
                        state.watch_probe_ticks >= WATCH_MIN_SAMPLES
                        and sample.timestamp - state.watch_probe_ts >= WATCH_MIN_HOLD_SEC
                    ):
                        state.watch_emitted = True
                        state.watch_count += 1
                        leg = self._leg_pace_ratio(sample, state)
                        flags = self._stage_risk_flags(sample, state, leg, market_change_pct, stage=1)
                        return self._build_alert(
                            sample, day_low, recovered, leg,
                            stage=1, watch_th=watch_th, risk_flags=flags,
                        )

        # 阶段三：反弹回落跟进。阶段2 报的是「深跌回升」的乐观结论，
        # 回升失败此前没有任何跟进（2026-10-22 中天科技 28.20→27.61 回落
        # 全程零提醒）。只报事实：回升幅度过半被回吐且持续即补一条，
        # 不给买卖指令；每段一次、每日限次。
        if (
            episode_done
            and not state.fade_emitted
            and state.fade_count < FADE_MAX_PER_DAY
            and state.peak_recovered >= FADE_MIN_PEAK_PCT
        ):
            if recovered < FADE_TRIGGER_RATIO * state.peak_recovered:
                state.fade_probe_ticks += 1
                if state.fade_probe_ts is None:
                    state.fade_probe_ts = sample.timestamp
                if (
                    state.fade_probe_ticks >= FADE_MIN_SAMPLES
                    and sample.timestamp - state.fade_probe_ts >= FADE_MIN_HOLD_SEC
                ):
                    state.fade_emitted = True
                    state.fade_count += 1
                    return self._build_fade_alert(sample, state, day_low, recovered)
            else:
                # 回升重新走强就清 probe——「跌→回」不算持续回落。
                state.fade_probe_ts = None
                state.fade_probe_ticks = 0
        return None

    def _build_fade_alert(
        self, sample: Sample, state: _State, day_low: float, recovered: float,
    ) -> Alert:
        prev_close = sample.price / (1 + sample.change_pct / 100.0)
        low_price = prev_close * (1 + day_low / 100.0)
        title = f"📉 {sample.name}·反弹回落"
        body = (
            f"{sample.name}日内最低 {low_price:.2f}（{day_low:+.1f}%），"
            f"自低点回升最多 {state.peak_recovered:.1f}% 后回落，"
            f"现回升缩至 {recovered:.1f}%——回吐过半。"
            "此前「深跌回升」未稳，仅提示盘面事实；"
            "以收回支撑、企稳为准，再创新低则重新观察。"
        )
        return Alert(
            code=sample.code, title=title, body=body,
            vibrate=False, stock=sample.stock, stage=3,
        )

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
    def _recent_pace_fade(sample: Sample, state: _State) -> bool:
        """近 60 秒成交节奏是否已明显衰减（相对回升全段）——只警示不拦截。"""
        low_ts = state.day_low_ts
        low_cum = state.day_low_cum
        now_cum = sample.cumulative_turnover
        if low_ts is None or low_cum is None or now_cum is None:
            return False
        leg_minutes = (sample.timestamp - low_ts) / 60.0
        if leg_minutes < 1.0:
            return False
        cutoff = sample.timestamp - WATCH_FADE_WINDOW_SEC
        baseline: Optional[Tuple[float, float]] = None
        for ts, cum, _chg in state.pace_points:
            if ts <= cutoff:
                baseline = (ts, cum)
            else:
                break
        if baseline is None:
            return False
        pts_ts, pts_cum = baseline
        span_minutes = (sample.timestamp - pts_ts) / 60.0
        if span_minutes <= 0:
            return False
        recent_pace = max(now_cum - pts_cum, 0.0) / span_minutes
        whole_leg_pace = max(now_cum - low_cum, 0.0) / leg_minutes
        return whole_leg_pace > 0 and recent_pace < WATCH_FADE_RATIO * whole_leg_pace

    @staticmethod
    def _selling_pressure(sample: Sample, state: _State) -> bool:
        """「放量滞涨」：近 60 秒成交仍在放、价格却几乎没涨——借反弹出货的抛压痕迹。"""
        low_ts = state.day_low_ts
        low_cum = state.day_low_cum
        now_cum = sample.cumulative_turnover
        if low_ts is None or low_cum is None or now_cum is None:
            return False
        leg_minutes = (sample.timestamp - low_ts) / 60.0
        if leg_minutes < 1.0:
            return False
        cutoff = sample.timestamp - STALL_WINDOW_SEC
        baseline: Optional[Tuple[float, float, float]] = None
        for ts, cum, chg in state.pace_points:
            if ts <= cutoff:
                baseline = (ts, cum, chg)
            else:
                break
        if baseline is None:
            return False
        pts_ts, pts_cum, pts_chg = baseline
        span_minutes = (sample.timestamp - pts_ts) / 60.0
        if span_minutes <= 0:
            return False
        window_pace = max(now_cum - pts_cum, 0.0) / span_minutes
        whole_leg_pace = max(now_cum - low_cum, 0.0) / leg_minutes
        if whole_leg_pace <= 0 or window_pace < STALL_PACE_RATIO * whole_leg_pace:
            return False
        return (sample.change_pct - pts_chg) < STALL_MIN_PRICE_PCT

    def _stage_risk_flags(
        self,
        sample: Sample,
        state: _State,
        leg: Optional[float],
        market_change_pct: Optional[float],
        stage: int,
    ) -> List[str]:
        """防诱多警示旗标（纯措辞，不拦截）。阶段二只留趋势/大盘两条。"""
        flags: List[str] = []
        if stage == 1:
            if leg is not None and leg < LEG_PACE_RATIO:
                flags.append("缩量反抽，诱多风险高")
            if state.day_low_ts is not None:
                speed = sample.timestamp - state.day_low_ts
                if speed <= WATCH_SPIKE_SEC:
                    flags.append(f"低点后 {speed:.0f} 秒急拉，谨防对倒诱多")
            if self._recent_pace_fade(sample, state):
                flags.append("近 1 分钟量能已明显衰减")
        # 抛压「放量滞涨」两级都报：它否定的是反弹有效性本身，不只是时机。
        if self._selling_pressure(sample, state):
            flags.append("放量滞涨，抛压较重")
        if trend_down_risk(sample):
            flags.append("下跌趋势中的反弹，仅观察")
        if (
            sample.stock
            and market_change_pct is not None
            and market_change_pct <= MARKET_WEAK_PCT
        ):
            flags.append(f"大盘仍在走弱（上证 {market_change_pct:+.1f}%），反弹可信度下降")
        return flags

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

    def _build_alert(
        self,
        sample: Sample,
        day_low: float,
        recovered: float,
        leg: Optional[float],
        *,
        stage: int = 2,
        watch_th: Optional[float] = None,
        upgraded: bool = False,
        risk_flags: Tuple[str, ...] = (),
        selling_pressure: bool = False,
    ) -> Alert:
        if leg is not None:
            strong = leg >= LEG_PACE_RATIO
        else:
            ratio = sample.volume_ratio
            strong = ratio is not None and ratio >= VOLUME_CONFIRM_RATIO
        risk_text = "".join(
            flag if flag.endswith("。") else flag + "。" for flag in risk_flags
        )
        prev_close = sample.price / (1 + sample.change_pct / 100.0)
        low_price = prev_close * (1 + day_low / 100.0)

        if stage == 1:
            head = (
                f"{sample.name}日内最低 {low_price:.2f}（{day_low:+.1f}%），"
                f"现价 {sample.price:.2f}（{sample.change_pct:+.1f}%）"
                f"自低点回升 {recovered:.1f}%（观察门槛 {watch_th:.1f}%）"
            )
            title = f"📊 {sample.name}·反弹观察"
            body = (
                f"{head}；{self._volume_text(sample, leg, strong)}。"
                "仅观察：尚未确认，谨防诱多；再创新低即取消。"
                f"{risk_text}（观察状态：反弹观察）"
            )
            return Alert(
                code=sample.code, title=title, body=body,
                vibrate=False, stock=sample.stock, stage=1,
            )

        # 阶段二：确认语义不变；趋势向下时永不振动（防诱多唯一硬约束）。
        if sample.stock and strong:
            title = f"💰 {sample.name}·深跌回升（放量）"
            takeaway = (
                "回升段有资金承接迹象，仅作超跌反弹观察；"
                "仍以收回支撑、企稳为准，再创新低则取消。"
            )
        elif sample.stock:
            title = f"📈 {sample.name}·深跌回升"
            takeaway = "回升段量能偏弱，先按超跌反弹观察，补仓仍等企稳确认。"
        elif strong:
            title = f"💰 {sample.name}·下探回升（放量）"
            takeaway = (
                "回升段有资金承接迹象——指数反弹不替代个股判断，"
                "深套持仓仍以个股收回支撑、企稳为准。"
            )
        else:
            title = f"📈 {sample.name}·下探回升"
            takeaway = "回升段量能偏弱，先按超跌反弹观察；深套持仓若要补仓仍等个股企稳确认。"
        observation_state = "深跌回升" if strong else "超跌反弹"
        upgrade_note = "（自「反弹观察」升级确认）" if upgraded else ""
        head = (
            f"{sample.name}日内最低 {low_price:.2f}（{day_low:+.1f}%），"
            f"现价 {sample.price:.2f}（{sample.change_pct:+.1f}%）自低点回升 {recovered:.1f}%"
        )
        body = (
            f"{head}；{self._volume_text(sample, leg, strong)}。"
            f"仅观察：{takeaway}{risk_text}（观察状态：{observation_state}）{upgrade_note}"
        )
        vibrate = strong and not trend_down_risk(sample) and not selling_pressure
        return Alert(
            code=sample.code, title=title, body=body,
            vibrate=vibrate, stock=sample.stock, stage=2,
        )
