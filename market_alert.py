#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""大盘预警系统（优化版）

参考 Android 版优化建议，重点改进：
1. 通知合并：同一轮行情只发一条汇总通知
2. 重新武装：指数反弹后恢复预警能力
3. 量能基线：使用同时段历史数据，避免时间分布误判
4. 确认轮次：连续两轮确认，减少瞬时波动误报
5. 数据校验：过滤异常数据（价格为0、时间戳异常等）
6. 信号语义：反弹改为"弱势反抽观察"，不暗示买入

核心原则：
预警系统的重点不是尽可能多地发通知，而是在真正值得用户注意时发出一条清晰、可执行的通知。
"""

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Dict, List, Optional, Tuple, Set
from collections import defaultdict


class AlertLevel(Enum):
    """预警级别（按严重程度递增）"""
    INFO = "info"        # 提示（弱势反抽观察）
    WARNING = "warning"  # 转弱（市场明显走弱）
    DANGER = "danger"    # 风险（需要控制仓位）
    CRASH = "crash"      # 严重风险（系统性风险）


@dataclass
class MarketAlert:
    """预警事件"""
    level: AlertLevel
    title: str
    body: str
    icon: str
    index_name: str
    change_pct: float
    timestamp: datetime
    alert_type: str  # 'absolute' | 'speed' | 'volume' | 'recovery' | 'correlation'
    extra_info: Dict = field(default_factory=dict)


@dataclass
class IndexSnapshot:
    """指数快照"""
    code: str
    name: str
    price: float
    change_pct: float
    volume: float  # 成交额（亿）
    timestamp: datetime


@dataclass
class AlertState:
    """预警状态跟踪"""
    level_triggered: Set[str] = field(default_factory=set)  # 已触发的级别
    last_trigger_time: Optional[datetime] = None
    confirm_count: int = 0  # 连续确认计数
    pending_level: Optional[AlertLevel] = None  # 待确认的级别
    day_low: float = float('inf')
    day_high: float = float('-inf')
    armed: bool = True  # 是否处于武装状态（可触发预警）
    last_alert_pct: Optional[float] = None  # 上次提醒时的跌幅，用于继续下探时再提醒


class MarketAlertEngine:
    """大盘预警引擎（优化版）"""

    def __init__(self, config: Optional[Dict] = None):
        self.config = config or {}

        # ── 绝对跌幅阈值（分级） ──
        self.absolute_thresholds = self.config.get('absolute_thresholds', {
            AlertLevel.WARNING: -0.8,   # 转弱：-0.8%
            AlertLevel.DANGER: -1.2,     # 风险：-1.2%
            AlertLevel.CRASH: -2.5,      # 严重风险：-2.5%
        })

        # ── 速度预警阈值（5分钟窗口） ──
        self.speed_thresholds = self.config.get('speed_thresholds', {
            AlertLevel.WARNING: -0.5,
            AlertLevel.DANGER: -0.8,
            AlertLevel.CRASH: -1.2,
        })
        self.speed_window = self.config.get('speed_window', 300)  # 5分钟

        # ── 量能阈值（相对同时段基线） ──
        self.volume_thresholds = self.config.get('volume_thresholds', {
            1.5: '放量',
            2.0: '巨量',
            3.0: '天量',
        })

        # ── 反弹（弱势反抽）阈值 ──
        self.recovery_threshold = self.config.get('recovery_threshold', 0.5)
        self.recovery_min_drop = self.config.get('recovery_min_drop', -0.8)  # 此前至少跌0.8%

        # ── 多指数共振条件 ──
        self.correlation_conditions = self.config.get('correlation_conditions', {
            'min_falling_count': 2,      # 至少2个指数下跌
            'min_falling_pct': -0.8,     # 每个跌幅至少-0.8%
            'min_avg_pct': -0.7,         # 平均跌幅至少-0.7%
            'severe_single_pct': -1.2,   # 或单个指数跌幅达-1.2%
        })

        # ── 重新武装阈值 ──
        self.rearm_threshold = self.config.get('rearm_threshold', -0.6)

        # ── 继续下探重复提醒步长（已触发后每再跌这么多就再提醒一次）──
        self.realert_step = self.config.get('realert_step', -0.5)

        # ── 确认轮次 ──
        self.confirm_rounds = self.config.get('confirm_rounds', 2)  # 需要连续2轮确认

        # ── 状态跟踪 ──
        self._history: Dict[str, List[IndexSnapshot]] = {}  # code -> [snapshots]
        self._states: Dict[str, AlertState] = {}  # code -> AlertState
        self._day_volume_baseline: Dict[str, Dict[int, List[float]]] = defaultdict(
            lambda: defaultdict(list)
        )  # code -> {hour*60+minute -> [历史成交额]}
        self._correlation_triggered_today = False
        self._last_speed_check: Dict[str, datetime] = {}
        self._pending_alerts: List[MarketAlert] = []  # 待合并的通知

        # 速度检测节流
        self._speed_check_interval = 60  # 每分钟最多检测一次

    def update(self, snapshot: IndexSnapshot) -> List[MarketAlert]:
        """更新指数数据并返回待发送的预警（需要调用 flush_alerts 获取合并通知）"""
        # ── 数据校验 ──
        if not self._validate_snapshot(snapshot):
            return []

        code = snapshot.code

        # 初始化状态
        if code not in self._states:
            self._states[code] = AlertState()

        state = self._states[code]

        # 更新历史
        if code not in self._history:
            self._history[code] = []
        self._history[code].append(snapshot)

        # 保留最近1小时
        cutoff = snapshot.timestamp - timedelta(hours=1)
        self._history[code] = [s for s in self._history[code] if s.timestamp >= cutoff]

        # 更新日内高低点
        state.day_low = min(state.day_low, snapshot.price)
        state.day_high = max(state.day_high, snapshot.price)

        # 记录成交额（用于同时段基线）
        time_key = snapshot.timestamp.hour * 60 + snapshot.timestamp.minute
        self._day_volume_baseline[code][time_key].append(snapshot.volume)

        # ── 检查重新武装 ──
        self._check_rearm(snapshot, state)

        # ── 各维度检测 ──
        alerts = []

        # 1. 绝对跌幅（需要确认轮次）
        abs_alert = self._check_absolute_alert(snapshot, state)
        if abs_alert:
            alerts.append(abs_alert)

        # 2. 速度预警
        speed_alert = self._check_speed_alert(snapshot, state)
        if speed_alert:
            alerts.append(speed_alert)

        # 3. 量能确认（作为附加信息，不单独触发）
        volume_info = self._check_volume(snapshot)

        # 4. 弱势反抽观察
        recovery_alert = self._check_recovery(snapshot, state)
        if recovery_alert:
            alerts.append(recovery_alert)

        # 将量能信息附加到已有预警
        if volume_info and alerts:
            for alert in alerts:
                alert.extra_info['volume_label'] = volume_info['label']
                alert.extra_info['volume_ratio'] = volume_info['ratio']
                alert.body += f"\n📊 {volume_info['label']}下跌（{volume_info['ratio']:.1f}倍）"

        # 加入待合并队列
        self._pending_alerts.extend(alerts)

        return alerts

    def _validate_snapshot(self, snapshot: IndexSnapshot) -> bool:
        """校验数据有效性"""
        # 价格为0或负数
        if snapshot.price <= 0:
            return False

        # 时间戳异常
        if snapshot.timestamp > datetime.now() + timedelta(minutes=5):
            return False

        # 检查时间戳倒退
        code = snapshot.code
        if code in self._history and self._history[code]:
            last_ts = self._history[code][-1].timestamp
            if snapshot.timestamp < last_ts:
                return False

        # 单次价格跳变过大（>5%）且没有其他指数配合时，可能是异常数据
        if code in self._history and self._history[code]:
            last_price = self._history[code][-1].price
            if last_price > 0:
                change = abs(snapshot.price - last_price) / last_price
                if change > 0.05:  # 5%跳变
                    return False

        return True

    def _check_rearm(self, snapshot: IndexSnapshot, state: AlertState):
        """指数反弹到 rearm_threshold 以上时，清除已触发状态，恢复预警能力。

        注意：旧实现用 `if not state.armed` 作为前提，但 armed 初始为 True 且
        全代码从未置 False，导致 level_triggered 永远不会被清空——
        同级别预警全天只能报一次，后续再怎么跌都静默。这里改为只要有已触发
        状态就清空。
        """
        if snapshot.change_pct >= self.rearm_threshold:
            if state.level_triggered or not state.armed:
                state.armed = True
                state.level_triggered.clear()
                state.confirm_count = 0
                state.pending_level = None
                state.last_alert_pct = None

    def _check_absolute_alert(
        self, snapshot: IndexSnapshot, state: AlertState
    ) -> Optional[MarketAlert]:
        """检查绝对跌幅预警（带确认轮次）"""
        change_pct = snapshot.change_pct

        # 确定当前应触发的最高级别
        target_level = None
        for level in [AlertLevel.CRASH, AlertLevel.DANGER, AlertLevel.WARNING]:
            if change_pct <= self.absolute_thresholds[level]:
                target_level = level
                break

        if target_level is None:
            # 未达到任何阈值，重置确认计数
            state.confirm_count = 0
            state.pending_level = None
            return None

        # 检查是否已武装
        if not state.armed:
            return None

        # 检查是否已触发过该级别（或更高级别）
        level_order = [AlertLevel.WARNING, AlertLevel.DANGER, AlertLevel.CRASH]
        current_idx = level_order.index(target_level)

        # 检查是否已触发过该级别或更高级别
        already_triggered = any(
            level_order[i].value in state.level_triggered
            for i in range(current_idx, len(level_order))
        )
        if already_triggered:
            # 已经提醒过同级别/更高级别，但若继续大幅下探，必须再喊一次。
            # 否则会出现「13:24 敲完 CRASH，之后一路砸到 -3.9% 却全程静默」。
            if state.last_alert_pct is None:
                return None
            prev_pct = state.last_alert_pct
            if change_pct - prev_pct > self.realert_step:
                return None
            state.last_alert_pct = change_pct
            return self._make_continuation_alert(
                snapshot, target_level, change_pct, prev_pct
            )

        # 如果是升级（触发更高级别），清除低级别状态
        for i in range(current_idx):
            state.level_triggered.discard(level_order[i].value)

        # 确认轮次检查
        if target_level == AlertLevel.WARNING:
            # WARNING 需要连续两轮确认
            if state.pending_level == target_level:
                state.confirm_count += 1
            else:
                state.pending_level = target_level
                state.confirm_count = 1

            if state.confirm_count < self.confirm_rounds:
                return None
        else:
            # DANGER 和 CRASH 单轮触发
            pass

        # 触发预警
        state.level_triggered.add(target_level.value)
        state.confirm_count = 0
        state.pending_level = None

        icon = {
            AlertLevel.WARNING: '📉',
            AlertLevel.DANGER: '🚨',
            AlertLevel.CRASH: '🛑',
        }[target_level]

        title = {
            AlertLevel.WARNING: '大盘转弱提醒',
            AlertLevel.DANGER: '大盘风险预警',
            AlertLevel.CRASH: '大盘严重风险',
        }[target_level]

        # 构建正文（包含所有主要指数）
        indices_text = self._format_indices_snapshot()

        # 记录本次提醒时的跌幅，用于后续"继续下探"时再次提醒
        state.last_alert_pct = change_pct

        return MarketAlert(
            level=target_level,
            title=f"{icon} {title}",
            body=f"{indices_text}\n市场出现持续走弱迹象，请关注仓位和止损。",
            icon=icon,
            index_name='大盘',
            change_pct=change_pct,
            timestamp=snapshot.timestamp,
            alert_type='absolute',
            extra_info={
                'trigger_index': snapshot.name,
                'threshold': self.absolute_thresholds[target_level],
            },
        )

    def _make_continuation_alert(
        self, snapshot: IndexSnapshot, level: AlertLevel,
        change_pct: float, prev_pct: float,
    ) -> MarketAlert:
        """已触发过该级别后继续下探时的再次提醒。

        首次提醒负责"报状态"，这里负责"报恶化"，让用户知道情况比上次更糟，
        而不是在暴跌加深的过程中全程听不到任何声音。
        """
        icon = {
            AlertLevel.WARNING: '📉',
            AlertLevel.DANGER: '🚨',
            AlertLevel.CRASH: '🛑',
        }[level]

        title = {
            AlertLevel.WARNING: '大盘继续走弱',
            AlertLevel.DANGER: '大盘风险加剧',
            AlertLevel.CRASH: '大盘仍在下探',
        }[level]

        indices_text = self._format_indices_snapshot()
        worst = self._worst_snapshot()
        worst_text = f"最弱指数：{worst.name} {worst.change_pct:+.2f}%" if worst else ""

        return MarketAlert(
            level=level,
            title=f"{icon} {title}",
            body=(
                f"{indices_text}\n"
                + (f"{worst_text}\n" if worst_text else "")
                + f"{snapshot.name}较上次提醒（{prev_pct:+.2f}%）再跌 "
                f"{change_pct - prev_pct:+.2f}%，当前 {change_pct:+.2f}%，仍在创新低。\n"
                f"请立即核对持仓止损位，不要在下跌途中抄底加仓。"
            ),
            icon=icon,
            index_name=worst.name if worst else '大盘',
            change_pct=worst.change_pct if worst else change_pct,
            timestamp=snapshot.timestamp,
            alert_type='continue',
            extra_info={
                'trigger_index': snapshot.name,
                'is_continuation': True,
            },
        )

    def _worst_snapshot(self) -> Optional[IndexSnapshot]:
        """当前跌幅最深的指数快照。"""
        worst = None
        for history in self._history.values():
            if history:
                s = history[-1]
                if worst is None or s.change_pct < worst.change_pct:
                    worst = s
        return worst

    def _check_speed_alert(
        self, snapshot: IndexSnapshot, state: AlertState
    ) -> Optional[MarketAlert]:
        """检查暴跌速度预警（5分钟窗口）"""
        code = snapshot.code

        # 节流：每分钟最多检测一次
        now = snapshot.timestamp
        if code in self._last_speed_check:
            if (now - self._last_speed_check[code]).seconds < self._speed_check_interval:
                return None
        self._last_speed_check[code] = now

        # 开盘前5分钟不检测
        if now.hour == 9 and now.minute < 35:
            return None

        # 午盘开盘后重新建立基线（13:00-13:05）
        if now.hour == 13 and now.minute < 5:
            return None

        # 查找5分钟前的数据
        history = self._history.get(code, [])
        if not history:
            return None

        target_time = now - timedelta(seconds=self.speed_window)
        old_snapshot = None
        for s in history:
            if s.timestamp <= target_time:
                old_snapshot = s

        if old_snapshot is None:
            return None

        # 检查时间戳间隔是否正常
        time_diff = (now - old_snapshot.timestamp).total_seconds()
        if time_diff < self.speed_window * 0.8:  # 间隔不足80%，数据不完整
            return None

        # 计算跌幅
        if old_snapshot.price <= 0:
            return None

        price_change = (snapshot.price - old_snapshot.price) / old_snapshot.price * 100

        # 检查是否触发速度预警
        target_level = None
        for level in [AlertLevel.CRASH, AlertLevel.DANGER, AlertLevel.WARNING]:
            if price_change <= self.speed_thresholds[level]:
                target_level = level
                break

        if target_level is None:
            return None

        # 检查是否已触发
        if not state.armed:
            return None

        if target_level.value in state.level_triggered:
            return None

        # 触发
        state.level_triggered.add(f"speed_{target_level.value}")

        icon = '⚡' if target_level != AlertLevel.CRASH else '💥'

        return MarketAlert(
            level=target_level,
            title=f"{icon} 大盘急跌预警",
            body=f"过去5分钟内下跌 {price_change:.2f}%，速度 {price_change/5:.2f}%/分钟",
            icon=icon,
            index_name=snapshot.name,
            change_pct=snapshot.change_pct,
            timestamp=snapshot.timestamp,
            alert_type='speed',
            extra_info={
                'speed_pct_per_min': price_change / 5,
                'window_change': price_change,
            },
        )

    def _check_volume(self, snapshot: IndexSnapshot) -> Optional[Dict]:
        """检查量能（使用同时段基线），返回附加信息"""
        code = snapshot.code
        volume = snapshot.volume

        if volume <= 0:
            return None

        # 获取同时段历史基线
        time_key = snapshot.timestamp.hour * 60 + snapshot.timestamp.minute
        historical = self._day_volume_baseline[code].get(time_key, [])

        # 需要至少10个历史数据点（这里简化为用当日数据）
        if len(historical) < 5:
            return None

        avg_volume = sum(historical) / len(historical)
        if avg_volume <= 0:
            return None

        volume_ratio = volume / avg_volume

        # 返回量能标签
        for threshold, label in sorted(self.volume_thresholds.items(), reverse=True):
            if volume_ratio >= threshold:
                return {
                    'label': label,
                    'ratio': volume_ratio,
                    'current': volume,
                    'baseline': avg_volume,
                }

        return None

    def _check_recovery(
        self, snapshot: IndexSnapshot, state: AlertState
    ) -> Optional[MarketAlert]:
        """检查弱势反抽观察（注意：不是买入信号！）"""
        code = snapshot.code

        # 此前必须跌幅足够大
        if snapshot.change_pct > self.recovery_min_drop:
            return None

        # 从日内低点反弹
        if state.day_low <= 0 or state.day_low == float('inf'):
            return None

        recovery_pct = (snapshot.price - state.day_low) / state.day_low * 100

        if recovery_pct < self.recovery_threshold:
            return None

        # 检查是否已触发
        recovery_key = f"recovery_{snapshot.timestamp.date()}"
        if recovery_key in state.level_triggered:
            return None

        state.level_triggered.add(recovery_key)

        return MarketAlert(
            level=AlertLevel.INFO,
            title="💡 弱势反抽观察",
            body=(
                f"从今日低点 {state.day_low:.2f} 反弹 {recovery_pct:.2f}%，"
                f"当前价 {snapshot.price:.2f}\n"
                f"注意：这可能只是下跌中继，不构成买入信号。"
            ),
            icon='💡',
            index_name=snapshot.name,
            change_pct=snapshot.change_pct,
            timestamp=snapshot.timestamp,
            alert_type='recovery',
            extra_info={
                'day_low': state.day_low,
                'recovery_pct': recovery_pct,
            },
        )

    def check_correlation(self, snapshots: List[IndexSnapshot]) -> Optional[MarketAlert]:
        """检查多指数共振（合并通知）"""
        if self._correlation_triggered_today:
            return None

        if len(snapshots) < 2:
            return None

        # 条件1：至少2个指数跌幅达-0.8%，且平均跌幅达-0.7%
        falling_major = [s for s in snapshots if s.change_pct <= -0.8]
        avg_change = sum(s.change_pct for s in snapshots) / len(snapshots)

        condition1 = (
            len(falling_major) >= 2 and
            avg_change <= -0.7
        )

        # 条件2：三个指数同时下跌，且至少一个跌幅达-1.2%
        all_falling = all(s.change_pct < 0 for s in snapshots)
        has_severe = any(s.change_pct <= -1.2 for s in snapshots)

        condition2 = all_falling and has_severe and len(snapshots) >= 3

        if condition1 or condition2:
            self._correlation_triggered_today = True

            # 构建指数列表
            indices_text = self._format_indices_from_snapshots(snapshots)

            level = AlertLevel.DANGER if avg_change <= -1.5 else AlertLevel.WARNING

            return MarketAlert(
                level=level,
                title="📉 多指数共振下跌",
                body=f"{indices_text}\n多个主要指数同步走弱，平均跌幅 {avg_change:+.2f}%。",
                icon='📉',
                index_name='多指数',
                change_pct=avg_change,
                timestamp=datetime.now(),
                alert_type='correlation',
                extra_info={
                    'falling_count': len(falling_major),
                    'avg_change': avg_change,
                },
            )

        return None

    def _format_indices_snapshot(self) -> str:
        """格式化所有指数快照"""
        lines = []
        for code, history in self._history.items():
            if history:
                s = history[-1]
                lines.append(f"{s.name} {s.change_pct:+.2f}%")
        return '，'.join(lines) if lines else '暂无数据'

    def _format_indices_from_snapshots(self, snapshots: List[IndexSnapshot]) -> str:
        """从快照列表格式化指数信息"""
        lines = [f"{s.name} {s.change_pct:+.2f}%" for s in snapshots]
        return '，'.join(lines) if lines else '暂无数据'

    def flush_alerts(self) -> Optional[MarketAlert]:
        """获取合并后的预警通知（调用一次后清空队列）"""
        if not self._pending_alerts:
            return None

        # 按级别排序，取最高级别
        level_order = [AlertLevel.CRASH, AlertLevel.DANGER, AlertLevel.WARNING, AlertLevel.INFO]
        self._pending_alerts.sort(
            key=lambda a: level_order.index(a.level)
        )
        highest = self._pending_alerts[0]

        # 合并所有原因
        reasons = []
        for alert in self._pending_alerts:
            if alert.alert_type not in reasons:
                reasons.append(alert.alert_type)

        # 重新构建指数列表（使用最新数据）
        indices_text = self._format_indices_snapshot()

        # 标题上的跌幅必须是全场最惨的那个指数。
        # 否则会出现「创业板 -3.76% 的行情里，通知标题写着 -1.21%」——
        # 用户扫一眼以为没事，报警等于没报。
        worst = self._worst_snapshot()
        headline_pct = worst.change_pct if worst else highest.change_pct
        headline_name = worst.name if worst else highest.index_name

        # 级别必须和标题跌幅对齐。标题写着「转弱」配 -3.56% 一样是误导。
        level = highest.level
        for lv in (AlertLevel.CRASH, AlertLevel.DANGER, AlertLevel.WARNING):
            if headline_pct <= self.absolute_thresholds[lv]:
                if level_order.index(lv) < level_order.index(level):
                    level = lv
                break

        if level != highest.level:
            icon = {AlertLevel.WARNING: '📉', AlertLevel.DANGER: '🚨',
                    AlertLevel.CRASH: '🛑'}[level]
            title = {AlertLevel.WARNING: '大盘转弱提醒',
                     AlertLevel.DANGER: '大盘风险预警',
                     AlertLevel.CRASH: '大盘严重风险'}[level]
            title = f"{icon} {title}"
        else:
            title = highest.title
            icon = highest.icon

        # 构建合并通知
        merged = MarketAlert(
            level=level,
            title=title,
            body=f"{indices_text}\n市场出现持续走弱迹象，请关注仓位和止损。",
            icon=icon,
            index_name=headline_name,
            change_pct=headline_pct,
            timestamp=datetime.now(),
            alert_type='merged',
            extra_info={
                'all_alerts': len(self._pending_alerts),
                'reasons': reasons,
            },
        )

        # 清空队列
        self._pending_alerts.clear()

        return merged

    def get_market_status(self, snapshots: List[IndexSnapshot]) -> Dict:
        """获取市场状态摘要"""
        if not snapshots:
            return {'status': '无数据', 'level': 'info'}

        avg_change = sum(s.change_pct for s in snapshots) / len(snapshots)

        if avg_change <= -2.5:
            status = '🔴 市场严重风险'
            level = 'crash'
        elif avg_change <= -1.2:
            status = '🚨 市场风险'
            level = 'danger'
        elif avg_change <= -0.8:
            status = '📉 市场转弱'
            level = 'warning'
        elif avg_change >= 1.5:
            status = '🟢 市场强势'
            level = 'recover'
        elif avg_change >= 0.5:
            status = '🟢 市场偏强'
            level = 'info'
        else:
            status = '⚪ 市场平稳'
            level = 'info'

        return {
            'status': status,
            'level': level,
            'avg_change': avg_change,
            'indices': [
                {'name': s.name, 'change_pct': s.change_pct, 'price': s.price}
                for s in snapshots
            ],
        }

    def reset_daily(self):
        """每日开盘前重置所有状态"""
        self._states.clear()
        self._history.clear()
        self._day_volume_baseline.clear()
        self._correlation_triggered_today = False
        self._last_speed_check.clear()
        self._pending_alerts.clear()


# ── 辅助函数 ──

def format_alert_notification(alert: MarketAlert) -> Tuple[str, str, str]:
    """格式化预警通知（返回 title, subtitle, body）"""
    title = alert.title
    subtitle = f"{alert.index_name}  {alert.change_pct:+.2f}%"
    body = alert.body

    return title, subtitle, body


def get_alert_summary(alerts: List[MarketAlert]) -> str:
    """获取预警摘要文本"""
    if not alerts:
        return "暂无预警"

    lines = []
    for alert in alerts:
        lines.append(f"{alert.icon} {alert.title}")
        lines.append(f"   {alert.body}")

    return '\n'.join(lines)
