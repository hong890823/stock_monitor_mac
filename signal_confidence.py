#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""信号置信度评估系统。

通过记录历史数据，动态调整信号的确认策略。

核心指标：
1. 条件稳定性：信号条件持续了多少个tick
2. 价格动量：价格是否在信号方向上稳定移动
3. 量能一致性：量比是否持续在阈值以上/下
4. 历史成功率：该股票类似信号的历史表现
"""

from __future__ import annotations
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple
from intraday_tactics import IntradaySignalType


# ── 配置 ─────────────────────────────────────────────────────────────────────

# 每个tick间隔（秒）
TICK_INTERVAL = 15

# 信号条件历史记录长度（最多保留最近N个tick）
MAX_HISTORY_TICKS = 120  # 30分钟

# 置信度阈值（低于此值的信号不触发）
CONFIDENCE_THRESHOLD = 0.3

# 确认轮数映射（置信度 → 需要的确认轮数）
# 置信度越高，需要的确认轮数越少
CONFIDENCE_TO_ROUNDS = [
    (0.8, 1),  # 置信度 ≥ 0.8 → 1轮确认（高置信度）
    (0.6, 2),  # 置信度 ≥ 0.6 → 2轮确认
    (0.4, 3),  # 置信度 ≥ 0.4 → 3轮确认
    (0.0, 4),  # 置信度 < 0.4 → 4轮确认（低置信度）
]


# ── 数据结构 ──────────────────────────────────────────────────────────────────

@dataclass
class TickSnapshot:
    """单个tick的快照。"""
    timestamp: float
    price: float
    change_pct: float
    volume_ratio: Optional[float]
    # 各信号条件是否满足
    conditions: Dict[str, bool] = field(default_factory=dict)


@dataclass
class SignalRecord:
    """信号触发记录。"""
    signal_type: str
    trigger_time: float
    trigger_price: float
    # 后续价格变化（用于评估信号质量）
    price_after_1m: Optional[float] = None  # 1分钟后价格
    price_after_5m: Optional[float] = None  # 5分钟后价格
    price_after_15m: Optional[float] = None  # 15分钟后价格


# ── 信号置信度评估器 ──────────────────────────────────────────────────────────

class SignalConfidence:
    """基于历史数据的信号置信度评估。"""

    def __init__(self) -> None:
        # code -> 历史tick记录
        self._history: Dict[str, List[TickSnapshot]] = {}
        # code -> 信号触发记录
        self._signal_records: Dict[str, List[SignalRecord]] = {}
        # code -> 上次记录时间
        self._last_record_time: Dict[str, float] = {}

    def record_tick(
        self,
        code: str,
        price: float,
        change_pct: float,
        volume_ratio: Optional[float],
        conditions: Dict[str, bool],
    ) -> None:
        """记录一个tick的数据。"""
        now = time.time()

        # 限制记录频率（至少间隔10秒）
        last_time = self._last_record_time.get(code, 0)
        if now - last_time < 10:
            return

        if code not in self._history:
            self._history[code] = []

        snapshot = TickSnapshot(
            timestamp=now,
            price=price,
            change_pct=change_pct,
            volume_ratio=volume_ratio,
            conditions=conditions,
        )

        self._history[code].append(snapshot)

        # 限制历史长度
        if len(self._history[code]) > MAX_HISTORY_TICKS:
            self._history[code] = self._history[code][-MAX_HISTORY_TICKS:]

        self._last_record_time[code] = now

    def calculate_confidence(
        self,
        code: str,
        signal_type: IntradaySignalType,
        current_price: float,
        current_conditions: Dict[str, bool],
    ) -> float:
        """计算信号的置信度（0.0 ~ 1.0）。

        置信度越高，信号越可靠，需要的确认轮数越少。
        """
        history = self._history.get(code, [])
        if not history:
            return 0.5  # 无历史数据，返回中等置信度

        # 1. 条件稳定性评分（权重：0.4）
        stability_score = self._calculate_stability(history, signal_type)

        # 2. 价格动量评分（权重：0.3）
        momentum_score = self._calculate_momentum(history, signal_type, current_price)

        # 3. 量能一致性评分（权重：0.2）
        volume_score = self._calculate_volume_consistency(history)

        # 4. 历史信号成功率（权重：0.1）
        history_score = self._calculate_historical_success(code, signal_type)

        # 加权计算总置信度
        confidence = (
            stability_score * 0.4 +
            momentum_score * 0.3 +
            volume_score * 0.2 +
            history_score * 0.1
        )

        return min(1.0, max(0.0, confidence))

    def get_confirmation_rounds(
        self,
        code: str,
        signal_type: IntradaySignalType,
        current_price: float,
        current_conditions: Dict[str, bool],
    ) -> int:
        """根据置信度返回需要的确认轮数。"""
        # 破位/高位离场始终立即触发（风险信号）
        if signal_type in {
            IntradaySignalType.BREAK_SUPPORT,
            IntradaySignalType.EXIT_RISK,
        }:
            return 1

        confidence = self.calculate_confidence(
            code, signal_type, current_price, current_conditions
        )
        if confidence < CONFIDENCE_THRESHOLD:
            return 0

        # 根据置信度映射到确认轮数
        for threshold, rounds in CONFIDENCE_TO_ROUNDS:
            if confidence >= threshold:
                return rounds

        return 4  # 默认4轮

    def _calculate_stability(
        self,
        history: List[TickSnapshot],
        signal_type: IntradaySignalType,
    ) -> float:
        """计算信号条件的稳定性。

        条件持续满足越久，稳定性越高。
        """
        if len(history) < 2:
            return 0.5

        # 获取最近的条件状态
        recent = history[-20:]  # 最近20个tick（5分钟）
        signal_key = signal_type.value

        # 统计条件满足的比例
        met_count = sum(1 for s in recent if s.conditions.get(signal_key, False))
        stability = met_count / len(recent)

        # 如果条件持续满足，给额外加分
        if met_count == len(recent):
            stability = min(1.0, stability + 0.2)

        return stability

    def _calculate_momentum(
        self,
        history: List[TickSnapshot],
        signal_type: IntradaySignalType,
        current_price: float,
    ) -> float:
        """计算价格动量。

        价格在信号方向上稳定移动，动量评分高。
        """
        if len(history) < 4:
            return 0.5

        # 获取最近的价格变化
        recent_prices = [s.price for s in history[-12:]]  # 最近3分钟
        if len(recent_prices) < 2:
            return 0.5

        # 计算价格趋势
        price_changes = [
            recent_prices[i] - recent_prices[i-1]
            for i in range(1, len(recent_prices))
        ]

        if signal_type in [
            IntradaySignalType.BREAKOUT,
            IntradaySignalType.T_SELL,
            IntradaySignalType.T_HOLD,
            IntradaySignalType.T_HOLD_LEADER,
            IntradaySignalType.T_HOLD_CAUTION,
        ]:
            # 买入/卖出信号：价格上涨趋势
            positive_changes = sum(1 for c in price_changes if c > 0)
            momentum = positive_changes / len(price_changes)
        elif signal_type in [IntradaySignalType.BREAK_SUPPORT, IntradaySignalType.T_BUY, IntradaySignalType.PULLBACK]:
            # 卖出/买入信号：价格下跌趋势
            negative_changes = sum(1 for c in price_changes if c < 0)
            momentum = negative_changes / len(price_changes)
        else:
            momentum = 0.5

        return momentum

    def _calculate_volume_consistency(
        self,
        history: List[TickSnapshot],
    ) -> float:
        """计算量能一致性。

        量比持续在阈值以上/下，一致性高。
        """
        if len(history) < 4:
            return 0.5

        recent = history[-12:]  # 最近3分钟
        volumes = [s.volume_ratio for s in recent if s.volume_ratio is not None]

        if not volumes:
            return 0.5

        # 计算量比的标准差（越小越一致）
        mean_volume = sum(volumes) / len(volumes)
        if mean_volume <= 0:
            return 0.5

        variance = sum((v - mean_volume) ** 2 for v in volumes) / len(volumes)
        std_dev = variance ** 0.5

        # 标准差越小，一致性越高
        # 将标准差转换为0-1的评分
        consistency = max(0, 1 - std_dev / mean_volume)

        return consistency

    def _calculate_historical_success(
        self,
        code: str,
        signal_type: IntradaySignalType,
    ) -> float:
        """计算历史信号成功率。

        基于过去信号触发后的价格变化评估。
        """
        records = self._signal_records.get(code, [])
        if not records:
            return 0.5  # 无历史数据

        # 筛选同类型信号
        type_records = [
            r for r in records
            if r.signal_type == signal_type.value and r.price_after_5m is not None
        ]

        direction = signal_success_direction(signal_type)
        if direction is None or not type_records:
            return 0.5

        success_count = sum(
            1 for record in type_records
            if (
                record.price_after_5m > record.trigger_price
                if direction > 0
                else record.price_after_5m < record.trigger_price
            )
        )

        success_rate = success_count / len(type_records)
        return success_rate

    def record_signal_trigger(
        self,
        code: str,
        signal_type: IntradaySignalType,
        trigger_price: float,
    ) -> None:
        """记录信号触发，用于后续评估。"""
        if code not in self._signal_records:
            self._signal_records[code] = []

        record = SignalRecord(
            signal_type=signal_type.value,
            trigger_time=time.time(),
            trigger_price=trigger_price,
        )

        self._signal_records[code].append(record)

        # 限制记录数量
        if len(self._signal_records[code]) > 100:
            self._signal_records[code] = self._signal_records[code][-100:]

    def update_signal_outcome(
        self,
        code: str,
        signal_type: IntradaySignalType,
        current_price: float,
    ) -> None:
        """更新信号触发后的价格变化。"""
        records = self._signal_records.get(code, [])
        if not records:
            return

        now = time.time()

        # 找到最近的同类型信号记录
        for record in reversed(records):
            if record.signal_type != signal_type.value:
                continue

            elapsed = now - record.trigger_time

            # 更新1分钟后的价格
            if elapsed >= 60 and record.price_after_1m is None:
                record.price_after_1m = current_price

            # 更新5分钟后的价格
            if elapsed >= 300 and record.price_after_5m is None:
                record.price_after_5m = current_price

            # 更新15分钟后的价格
            if elapsed >= 900 and record.price_after_15m is None:
                record.price_after_15m = current_price

            break

    def get_stats(self, code: str) -> Dict:
        """获取信号统计信息。"""
        history = self._history.get(code, [])
        records = self._signal_records.get(code, [])

        stats = {
            "history_ticks": len(history),
            "signal_records": len(records),
            "by_type": {},
        }

        # 按信号类型统计
        for record in records:
            st = record.signal_type
            if st not in stats["by_type"]:
                stats["by_type"][st] = {"total": 0, "success": 0}

            stats["by_type"][st]["total"] += 1

            direction = signal_success_direction(st)
            if record.price_after_5m is not None and direction is not None:
                moved_up = record.price_after_5m > record.trigger_price
                if (direction > 0 and moved_up) or (direction < 0 and not moved_up):
                    stats["by_type"][st]["success"] += 1

        return stats


def signal_success_direction(signal_type) -> Optional[int]:
    """Return the price direction that makes a directional signal successful."""
    value = _signal_type_value(signal_type)
    if value in {
        IntradaySignalType.BREAKOUT.value,
        IntradaySignalType.RECLAIM_SUPPORT.value,
        IntradaySignalType.T_BUY.value,
        IntradaySignalType.PULLBACK.value,
    }:
        return 1
    if value in {
        IntradaySignalType.BREAK_SUPPORT.value,
        IntradaySignalType.EXIT_RISK.value,
        IntradaySignalType.EXIT_WINDOW.value,
        IntradaySignalType.T_SELL.value,
        IntradaySignalType.TREND_WEAK.value,
    }:
        return -1
    return None


def _signal_type_value(signal_type) -> str:
    return str(getattr(signal_type, "value", signal_type) or "")
