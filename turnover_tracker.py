#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""两市成交额的「同一时刻」对比。

原来用「今天半日累计」直接比「昨天全天总额」，盘中会恒定显示大幅缩水，没有意义。
改成同口径比较：
- 优先从东方财富分时曲线接口拉取昨日同期（当天即可用）
- 同时每15分钟把当日累计成交额落盘，只保留最近30个交易日（本地兜底）
- 全天预估按「昨日同期占昨日全天的比例」推算

来源：Android 版 TurnoverTracker.kt + EastMoneyClient.kt
"""

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_DAYS = 30          # 只保留最近 30 个交易日
SLOT_INTERVAL = 15     # 采样间隔（分钟）
OPEN_GUARD_MINUTE = 10 # 09:40 前不预估（开盘前数据波动大）
MIN_SHARE = 0.02       # 昨日同期至少占昨日全天 2% 才做预估

# 东方财富指数 secid：上证指数 + 深证成指
TURNOVER_SECIDS = ["1.000001", "0.399001"]

# ---------------------------------------------------------------------------
# TurnoverCurve — 单日分时累计成交额曲线
# ---------------------------------------------------------------------------

@dataclass
class TurnoverCurve:
    """单个交易日的分时累计成交额曲线。

    key 为当天分钟数（09:30 = 570），value 为截至该分钟的累计成交额（元）。
    """
    date: str
    cumulative: Dict[int, float] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not self.cumulative

    def at(self, minute_of_day: int) -> Optional[float]:
        """取不超过 minute_of_day 的最后一个采样；早于第一个采样点返回 None。"""
        candidates = [k for k in self.cumulative if k <= minute_of_day]
        if not candidates:
            return None
        return self.cumulative[max(candidates)]

    @property
    def full_day(self) -> Optional[float]:
        """收盘累计（全天成交额），取最后一个采样。"""
        if not self.cumulative:
            return None
        return self.cumulative[max(self.cumulative.keys())]


# ---------------------------------------------------------------------------
# 东方财富分时成交额接口
# ---------------------------------------------------------------------------

def _http_get_text(url: str, referer: str, timeout: int = 12, retries: int = 2) -> str:
    """带 Referer 的 GET 请求，返回文本，自动重试（模仿 Android 版策略）。"""
    import time
    import urllib.request
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": referer,
        "Accept": "*/*",
    }
    last_error = None
    for attempt in range(retries):
        conn = None
        try:
            req = urllib.request.Request(url, headers=headers)
            conn = urllib.request.urlopen(req, timeout=timeout)
            data = conn.read()
            return data.decode("utf-8", errors="replace")
        except Exception as e:
            last_error = e
            if attempt < retries - 1:
                time.sleep(2.0)  # 重试延迟 2 秒
        finally:
            if conn:
                conn.close()
    raise last_error


def fetch_index_turnover_curves(secid: str, days: int = 2) -> List[TurnoverCurve]:
    """拉取指数分时累计成交额曲线。

    东方财富 trends2 接口一次返回最近 ndays 个交易日的 1 分钟级数据。
    返回按日期升序的 TurnoverCurve 列表。
    """
    url = (
        f"https://push2his.eastmoney.com/api/qt/stock/trends2/get?secid={secid}"
        f"&fields1=f1,f2,f3,f4,f5,f6,f7,f8"
        f"&fields2=f51,f52,f53,f54,f55,f56,f57,f58"
        f"&iscr=0&ndays={days}"
    )
    import json as _json
    text = _http_get_text(url, "https://quote.eastmoney.com/")
    data = _json.loads(text)
    trends = (data.get("data") or {}).get("trends") or []
    if not trends:
        return []

    # trends 每行形如 "2026-09-28 09:30,price,...,amount,..."
    # f57（index=6）是每分钟成交额，先按日期分桶再累加
    per_date: Dict[str, Dict[int, float]] = {}
    for line in trends:
        parts = line.split(",")
        if len(parts) < 7:
            continue
        stamp = parts[0]  # "2026-09-28 09:30"
        date_str = stamp.split(" ")[0]
        time_str = stamp.split(" ")[1] if " " in stamp else ""
        try:
            hour, minute = map(int, time_str.split(":"))
        except (ValueError, IndexError):
            continue
        try:
            amount = float(parts[6])
        except (ValueError, IndexError):
            continue
        minute_of_day = hour * 60 + minute
        bucket = per_date.setdefault(date_str, {})
        bucket[minute_of_day] = bucket.get(minute_of_day, 0.0) + amount

    curves = []
    for date_str in sorted(per_date.keys()):
        per_minute = per_date[date_str]
        cumulative: Dict[int, float] = {}
        running = 0.0
        for minute_of_day in sorted(per_minute.keys()):
            running += per_minute[minute_of_day]
            cumulative[minute_of_day] = running
        curves.append(TurnoverCurve(date=date_str, cumulative=cumulative))
    return curves


def merge_curves(curves: List[TurnoverCurve], date: str) -> Optional[TurnoverCurve]:
    """按分钟把多条曲线合并成「两市合计」曲线。"""
    merged: Dict[int, float] = {}
    for curve in curves:
        if curve.date != date:
            continue
        for minute_of_day, amount in curve.cumulative.items():
            merged[minute_of_day] = merged.get(minute_of_day, 0.0) + amount
    if not merged:
        return None
    return TurnoverCurve(date=date, cumulative=merged)


# 模块级缓存：每天只请求一次 API
_curve_cache: Dict[str, Optional[TurnoverCurve]] = {}
_curve_cache_file = os.path.join(os.path.dirname(__file__), "_curve_cache.json")

def _load_curve_cache():
    """从磁盘加载曲线缓存"""
    global _curve_cache
    if os.path.exists(_curve_cache_file):
        try:
            with open(_curve_cache_file, "r", encoding="utf-8") as f:
                raw = json.load(f)
            for date_str, data in raw.items():
                if data:
                    cumulative = {int(k): float(v) for k, v in data.get("cumulative", {}).items()}
                    _curve_cache[date_str] = TurnoverCurve(
                        date=data["date"],
                        cumulative=cumulative
                    )
                else:
                    _curve_cache[date_str] = None
        except Exception:
            pass

def _save_curve_cache():
    """保存曲线缓存到磁盘"""
    try:
        raw = {}
        for date_str, curve in _curve_cache.items():
            if curve:
                raw[date_str] = {
                    "date": curve.date,
                    "cumulative": {str(k): v for k, v in curve.cumulative.items()}
                }
            else:
                raw[date_str] = None
        os.makedirs(os.path.dirname(_curve_cache_file) or ".", exist_ok=True)
        with open(_curve_cache_file, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

# 启动时加载缓存
_load_curve_cache()

def fetch_previous_turnover_curve(today_date: str) -> Optional[TurnoverCurve]:
    """拉取昨日两市合计分时成交额曲线（当天即可用，带磁盘缓存）。

    与 Android 策略一致：
    - 只缓存成功结果，失败不缓存
    - 失败后下次调用会立即重试（不等待）
    - 每天只请求一次 API（成功后）
    """
    # 命中缓存直接返回（只缓存成功结果）
    if today_date in _curve_cache and _curve_cache[today_date] is not None:
        return _curve_cache[today_date]

    import time
    result = None
    try:
        all_curves: List[TurnoverCurve] = []
        for i, secid in enumerate(TURNOVER_SECIDS):
            if i > 0:
                time.sleep(2.0)  # 请求间隔 2 秒，避免限流
            all_curves.extend(fetch_index_turnover_curves(secid, days=2))
        # 找到早于今天、且数据最全的那个日期
        dates = sorted({c.date for c in all_curves if c.date < today_date})
        if dates:
            result = merge_curves(all_curves, dates[-1])
    except Exception:
        result = None

    # 只缓存成功结果（与 Android 一致）
    # 失败时不缓存，下次调用会重试
    if result is not None:
        _curve_cache[today_date] = result
        _save_curve_cache()

    return result


# ---------------------------------------------------------------------------
# Source enum
# ---------------------------------------------------------------------------

class Source(Enum):
    """昨日同期数据的来源。"""
    CURVE = "curve"    # 来自外部行情曲线接口
    LOCAL = "local"    # 来自本地历史采样
    NONE  = "none"     # 暂无数据

# ---------------------------------------------------------------------------
# Snapshot dataclass
# ---------------------------------------------------------------------------

@dataclass
class Snapshot:
    """某一时刻的成交额快照。"""
    slot: str                          # "10:00"
    slot_minute: int                   # 0..240
    today: float                       # 当日累计成交额（亿）
    previous_same_time: Optional[float]  # 昨日同期（亿）
    previous_full_day: Optional[float]   # 昨日全天（亿）
    predicted_full_day: Optional[float]  # 全天预估（亿）
    source: Source                     # 数据来源

# ---------------------------------------------------------------------------
# TurnoverTracker
# ---------------------------------------------------------------------------

class TurnoverTracker:
    """跟踪两市成交额，支持同口径历史对比。"""

    # 每日交易时段的起止分钟数（相对 09:30）
    # 上午 09:30-11:30 → 0-120
    # 下午 13:00-15:00 → 135-240
    _MORNING_START = 0
    _MORNING_END   = 120
    _BREAK_START   = 120   # 11:30 午休开始
    _BREAK_END     = 135   # 13:00 午休结束
    _AFTERNOON_END = 240   # 15:00 收盘
    _CLOSE_SAMPLE_KEY = -1

    def __init__(self, data_file: str):
        """
        Parameters
        ----------
        data_file : str
            JSON 持久化文件路径，用于保存每日采样数据。
        """
        self._data_file: str = data_file
        self._data: Dict[str, Dict[int, float]] = {}
        self._load()

    # ------------------------------------------------------------------
    # 静态工具方法
    # ------------------------------------------------------------------

    @staticmethod
    def slot_minute(dt: datetime) -> Optional[int]:
        """返回给定时间对应的 15 分钟槽位编号（0-240），不在交易时段返回 None。

        编号规则：
        - 09:30 → 0,  09:45 → 15,  ..., 11:30 → 120
        - 13:00 → 135, 13:15 → 150, ..., 15:00 → 240
        """
        hour = dt.hour
        minute = dt.minute

        # 上午时段 09:30 - 11:30
        if hour == 9 and minute >= 30:
            return (minute - 30)
        if hour == 10:
            return 30 + minute
        if hour == 11 and minute <= 30:
            return 60 + 30 + minute  # 11:00→90, 11:15→105, 11:30→120

        # 下午时段 13:00 - 15:00
        if hour == 13:
            return 135 + minute
        if hour == 14:
            return 135 + 60 + minute  # 14:00→195, 14:15→210, ...
        if hour == 15 and minute == 0:
            return 240

        return None

    @staticmethod
    def slot_text(slot_minute: int) -> str:
        """将 slot_minute 转换为 \"HH:MM\" 时间字符串。

        0 → "09:30", 15 → "09:45", ..., 120 → "11:30",
        135 → "13:00", ..., 240 → "15:00"
        """
        if slot_minute <= 120:
            # 上午段：09:30 + slot_minute 分钟
            total_minutes = 9 * 60 + 30 + slot_minute
        else:
            # 下午段：13:00 + (slot_minute - 135) 分钟
            total_minutes = 13 * 60 + (slot_minute - 135)
            # 最后一个槽位 = 收盘时刻 15:00
            if total_minutes >= 14 * 60 + 45:
                total_minutes = 15 * 60

        hour = total_minutes // 60
        minute = total_minutes % 60
        return f"{hour:02d}:{minute:02d}"

    @staticmethod
    def slot_minute_of_day(slot_minute: int) -> int:
        """将槽位编号（0-240）转换为当天分钟数（570-900）。"""
        if slot_minute <= 120:
            return 9 * 60 + 30 + slot_minute
        else:
            return 13 * 60 + (slot_minute - 135)

    def recent_full_day_turnovers(
        self, before_date: Optional[str] = None, limit: int = 5
    ) -> List[float]:
        """返回已记录日期的收盘成交额（亿元）；缺收盘采样用 NaN 保留断点。"""
        dates = sorted(self._data)
        if before_date:
            dates = [date for date in dates if date < before_date]
        values = []
        for date in dates[-max(1, limit):]:
            samples = self._data.get(date) or {}
            values.append(float(samples.get(self._CLOSE_SAMPLE_KEY, float('nan'))))
        return values

    def recent_full_day_turnover_history(
        self, before_date: Optional[str] = None, limit: int = 5
    ) -> List[Tuple[str, float]]:
        """返回此前完整收盘日的日期和成交额；缺收盘采样的日期直接跳过。"""
        dates = sorted(self._data)
        if before_date:
            dates = [date for date in dates if date < before_date]
        values: List[Tuple[str, float]] = []
        for date in dates[-max(1, limit):]:
            samples = self._data.get(date) or {}
            value = samples.get(self._CLOSE_SAMPLE_KEY)
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if number > 0:
                values.append((date, number))
        return values

    # ------------------------------------------------------------------
    # 核心采样
    # ------------------------------------------------------------------

    def sample(
        self,
        today_turnover: float,
        dt: datetime,
        previous_curve: Optional[TurnoverCurve] = None,
        force_record: bool = False,
    ) -> Optional[Snapshot]:
        """记录当前时刻的成交额并返回快照。

        与 Android 版保持一致：
        - 每次调用都用最新的 today_turnover 计算预估（实时性）
        - 只在 15 分钟槽位记录历史数据（避免频繁写入）
        - 预估始终使用实时数据，而非槽位采样值

        Parameters
        ----------
        today_turnover : float
            当日累计成交额（亿），应为最新实时值。
        dt : datetime
            当前时间。
        previous_curve : TurnoverCurve, optional
            昨日分时累计成交额曲线（来自东方财富接口），优先于本地采样。
        force_record : bool
            强制记录到历史（用于槽位边界），默认 False。

        Returns
        -------
        Snapshot or None
            不在交易时段时返回 None。
        """
        sm = self.slot_minute(dt)
        if sm is None:
            return None

        today_date = dt.strftime("%Y-%m-%d")
        slot_str = self.slot_text(sm)

        # 使用精确的分钟数（与 Android 一致），而非槽位边界
        # 这样即使在 11:31 采样，也能查到 11:31 的昨日同期值
        precise_minute_of_day = dt.hour * 60 + dt.minute

        # 记录当日数据（只在槽位边界或强制记录时写入）
        # 与 Android 一致：每 15 分钟采样一次，但预估始终用最新值
        if force_record or sm % 15 == 0:
            if today_date not in self._data:
                self._data[today_date] = {}
            self._data[today_date][sm] = today_turnover
            if dt.hour == 15 and dt.minute == 0:
                self._data[today_date][self._CLOSE_SAMPLE_KEY] = today_turnover
            self._save()

        # 查找昨日同期 —— 优先外部曲线，其次本地采样
        prev_same_time: Optional[float] = None
        prev_full_day: Optional[float] = None
        source = Source.NONE

        if previous_curve is not None and not previous_curve.is_empty:
            # 外部曲线：使用精确的分钟数查询（与 Android 一致）
            prev_same_time = previous_curve.at(precise_minute_of_day)
            if prev_same_time is not None and prev_same_time > 0:
                # 曲线单位是元，转为亿
                prev_same_time = prev_same_time / 1e8
                source = Source.CURVE
            prev_full_day_val = previous_curve.full_day
            if prev_full_day_val is not None and prev_full_day_val > 0:
                prev_full_day = prev_full_day_val / 1e8

        if source == Source.NONE:
            # 退回本地采样（使用槽位，因为本地采样是按槽位记录的）
            prev_date = self._previous_date(today_date)
            if prev_date is not None:
                prev_day_data = self._data[prev_date]
                if sm in prev_day_data:
                    prev_same_time = prev_day_data[sm]
                    source = Source.LOCAL
                if prev_day_data:
                    prev_full_day = max(prev_day_data.values())

        # 全天预估（早盘保护 + 最小占比阈值）
        predicted: Optional[float] = None
        if sm < OPEN_GUARD_MINUTE:
            # 09:40 前不预估，开盘数据波动大
            predicted = None
        elif (prev_same_time is not None and prev_same_time > 0
              and prev_full_day is not None
              and prev_same_time >= prev_full_day * MIN_SHARE):
            predicted = today_turnover / prev_same_time * prev_full_day

        # 持久化
        self._save()

        return Snapshot(
            slot=slot_str,
            slot_minute=sm,
            today=today_turnover,
            previous_same_time=prev_same_time,
            previous_full_day=prev_full_day,
            predicted_full_day=predicted,
            source=source,
        )

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _previous_date(self, today_date: str) -> Optional[str]:
        """返回 self._data 中比 today_date 早的最近一个日期。"""
        candidates = [d for d in self._data if d < today_date]
        if not candidates:
            return None
        return max(candidates)

    def _load(self):
        """从 JSON 文件加载历史数据，并裁剪到 MAX_DAYS。"""
        if not os.path.exists(self._data_file):
            self._data = {}
            return
        try:
            with open(self._data_file, "r", encoding="utf-8") as f:
                raw = json.load(f)
            # JSON key 的 slot_minute 是字符串，转回 int
            self._data = {}
            for date_str, slots in raw.items():
                self._data[date_str] = {int(k): float(v) for k, v in slots.items()}
            self._trim()
        except (json.JSONDecodeError, OSError):
            self._data = {}

    def _save(self):
        """将 self._data 写入 JSON 文件。"""
        self._trim()
        try:
            os.makedirs(os.path.dirname(self._data_file) or ".", exist_ok=True)
            with open(self._data_file, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def _trim(self):
        """只保留最近 MAX_DAYS 天的数据。"""
        if len(self._data) <= MAX_DAYS:
            return
        sorted_dates = sorted(self._data.keys())
        for old_date in sorted_dates[: len(sorted_dates) - MAX_DAYS]:
            del self._data[old_date]

# ---------------------------------------------------------------------------
# 显示辅助
# ---------------------------------------------------------------------------

def format_turnover_lines(snapshot: Snapshot) -> List[str]:
    """将 Snapshot 转换为可显示的文本行。

    Returns
    -------
    list of str
        例如：
        ["两市总成交 8356.2亿",
         "较昨日同期（10:00） +523.1亿 (+6.7%)",
         "全天约 16800.0亿"]
    """
    lines: List[str] = []
    lines.append(f"两市总成交 {snapshot.today:.1f}亿")

    if snapshot.previous_same_time is not None:
        delta = snapshot.today - snapshot.previous_same_time
        if snapshot.previous_same_time > 0:
            pct = delta / snapshot.previous_same_time * 100
        else:
            pct = 0.0
        sign = "+" if delta >= 0 else ""
        compare_label = "昨日全天" if snapshot.slot_minute >= 240 else f"昨日同期（{snapshot.slot}）"
        lines.append(
            f"较{compare_label} {sign}{delta:.1f}亿 ({sign}{pct:.1f}%)"
        )
    else:
        lines.append("昨日同期数据暂缺，已开始记录，明天起可对比")

    if snapshot.predicted_full_day is not None and snapshot.slot_minute < 240:
        lines.append(f"全天约 {snapshot.predicted_full_day:.1f}亿")

    return lines
