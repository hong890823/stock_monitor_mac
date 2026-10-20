# 持仓多周期加仓评估 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在持仓报告中综合周K、月K、年K、MA250 和基本面，只推荐一只值得加仓的现有持仓或明确不加仓，并给出受风险和仓位约束的参考股数。

**Architecture:** 新增纯计算模块完成周期重采样、评分、否决、排名和仓位计算；独立服务负责长周期行情与基本面缓存；持仓调度只做 IO 和失败隔离，报告模块只展示结构化结果。

**Tech Stack:** Python 3.9、pandas、现有 `unittest`、`requests`、`fundamentals.py`、`stock_analysis.py`、`portfolio_daily_report.py`。

**Spec:** `docs/superpowers/specs/2026-10-15-portfolio-addition-ranking-design.md`

## Global Constraints

- 只评估 `quantity > 0` 的现有持仓，不选择观察池或全市场新股票。
- 最多推荐一只；证据不足时必须输出“当前不建议加仓”。
- 不自动下单、不修改持仓、不发送新通知，不改变风险提醒和观察池行为。
- 年K、月K和周K来自至少 1250 根日线的本地重采样；未完成周期不参与确认评分。
- 基本面快照超过 30 天不得支持新建议。
- 建议股数受 0.5% 风险预算、可用资金、单股最大仓位和 100 股整手约束。
- 所有金额、价格、比例和股数必须是有限数；NaN、Infinity、零或负关键值不得进入推荐。
- 本工作区不是 Git checkout，且用户未要求提交；执行过程中不要创建 Git 提交。
- 测试使用项目现有 `unittest`，不要引入 pytest。

## File Structure

- `portfolio_addition.py`：新建。只做纯计算，包含数据模型、重采样、评分、门槛、排名和仓位计算。
- `portfolio_addition_store.py`：新建。只做结构化基本面快照的 JSON 原子持久化和有效期判断。
- `portfolio_addition_service.py`：新建。读取长日线、调用基本面接口、更新缓存并生成评估输入；不负责 UI 或报告文案。
- `portfolio_daily_report.py`：修改。新增「三、加仓机会」章节并顺延原报告编号。
- `stock_menubar.py`：修改。后台调用评估服务，失败时保留原持仓报告。
- `tests/test_portfolio_addition.py`：新建。覆盖纯计算。
- `tests/test_portfolio_addition_store.py`：新建。覆盖缓存与过期。
- `tests/test_portfolio_addition_report.py`：新建。覆盖报告格式化。
- `tests/test_portfolio_addition_integration.py`：新建。覆盖服务和调度失败隔离。
- `tests/test_portfolio_daily_report.py`：修改。锁定新章节和编号。
- `docs/strategies/持仓多周期加仓评估.md`：新建。记录用户可见口径和限制。

---

### Task 1: 周期数据模型与重采样

**Files:**
- Create: `portfolio_addition.py`
- Test: `tests/test_portfolio_addition.py`

**Interfaces:**
- Consumes: pandas DataFrame，至少包含 `date/open/high/low/close/volume`。
- Produces: `Bar`、`FundamentalSnapshot`、`HoldingAssessmentInput`、`AdditionRecommendation`、`resample_ohlcv(history, frequency, now)`、`calculate_ma250(history)`。

- [x] **Step 1: 写失败测试**

```python
from datetime import datetime
import pandas as pd
from portfolio_addition import calculate_ma250, resample_ohlcv


def _daily_frame():
    rows = []
    for day in range(30):
        date = pd.Timestamp("2026-01-05") + pd.Timedelta(days=day)
        rows.append({
            "date": date.strftime("%Y-%m-%d"),
            "open": 10 + day * 0.1,
            "high": 11 + day * 0.1,
            "low": 9 + day * 0.1,
            "close": 10.5 + day * 0.1,
            "volume": 1000 + day,
        })
    return pd.DataFrame(rows)


def test_weekly_resampling_marks_current_week_incomplete():
    result = resample_ohlcv(_daily_frame(), "W", datetime(2026, 1, 20, 15))
    assert result.frequency == "W"
    assert result.completed[-1].date < "2026-01-20"
    assert result.current is not None
    assert result.current.completed is False


def test_ma250_requires_enough_valid_closes():
    assert calculate_ma250(_daily_frame()) is None
```

- [x] **Step 2: 运行并确认失败**

Run: `PYTHONPYCACHEPREFIX=/tmp/codex-pycache python3 -m unittest tests.test_portfolio_addition -v`
Expected: FAIL，原因是 `portfolio_addition` 尚未定义。

- [x] **Step 3: 实现数据模型和最小重采样**

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Mapping, Optional, Sequence, Tuple

import math
import pandas as pd


@dataclass(frozen=True)
class Bar:
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    completed: bool = True


@dataclass(frozen=True)
class FundamentalSnapshot:
    analyzed_at: str
    score: int
    health_level: str
    revenue_yoy: Optional[float]
    profit_yoy: Optional[float]
    deduct_ratio: Optional[float]
    gross_margin: Optional[float]
    net_margin: Optional[float]
    ocf_to_profit: Optional[float]
    roe: Optional[float]
    current_pe: Optional[float]
    current_pb: Optional[float]
    pe_percentile: Optional[float]
    pb_percentile: Optional[float]
    fair_low: Optional[float]
    fair_high: Optional[float]
    risks: Tuple[str, ...] = ()
    notes: Tuple[str, ...] = ()
    quarter_trends: Tuple[Tuple[float, float], ...] = ()


@dataclass(frozen=True)
class HoldingAssessmentInput:
    code: str
    name: str
    quantity: float
    cost: float
    price: float
    total_capital: float
    available_capital: float
    max_position_pct: float
    daily_history: pd.DataFrame
    technical: Mapping[str, Any]
    fundamental: Optional[FundamentalSnapshot]


@dataclass(frozen=True)
class AdditionRecommendation:
    code: str
    name: str
    selected: bool
    composite_score: float
    technical_score: float
    fundamental_score: float
    yearly_score: float
    monthly_score: float
    weekly_score: float
    shares: int
    amount: float
    stop_price: float
    position_pct_before: float
    position_pct_after: float
    reasons: Tuple[str, ...]
    missing: Tuple[str, ...]


@dataclass(frozen=True)
class ResampledHistory:
    frequency: Literal["W", "M", "Y"]
    completed: Tuple[Bar, ...]
    current: Optional[Bar]


def _valid_history(history: pd.DataFrame) -> pd.DataFrame:
    frame = history.copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
    for column in ("open", "high", "low", "close", "volume"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["date", "open", "high", "low", "close", "volume"])
    frame = frame[frame["volume"] >= 0]
    return frame.sort_values("date").reset_index(drop=True)


def _period_key(timestamp: pd.Timestamp, frequency: str):
    if frequency == "W":
        days_to_friday = (4 - timestamp.weekday()) % 7
        return (timestamp + pd.Timedelta(days=days_to_friday)).date()
    if frequency == "M":
        next_month = (timestamp.replace(day=1) + pd.offsets.MonthEnd(1)).date()
        return next_month
    if frequency == "Y":
        return timestamp.replace(month=12, day=31).date()
    raise ValueError("frequency must be W, M or Y")


def resample_ohlcv(history: pd.DataFrame, frequency: Literal["W", "M", "Y"],
                   now: datetime) -> ResampledHistory:
    frame = _valid_history(history)
    if frame.empty:
        raise ValueError("history has no valid OHLCV rows")
    frame["period_key"] = frame["date"].map(lambda value: _period_key(value, frequency))
    groups = []
    for key, group in frame.groupby("period_key", sort=True):
        groups.append(Bar(
            date=str(group["date"].iloc[-1].date()),
            open=float(group["open"].iloc[0]),
            high=float(group["high"].max()),
            low=float(group["low"].min()),
            close=float(group["close"].iloc[-1]),
            volume=float(group["volume"].sum()),
            completed=key < now.date(),
        ))
    completed = tuple(bar for bar in groups if bar.completed)
    current = next((bar for bar in reversed(groups) if not bar.completed), None)
    return ResampledHistory(frequency=frequency, completed=completed, current=current)


def calculate_ma250(history: pd.DataFrame) -> Optional[float]:
    frame = _valid_history(history)
    if len(frame) < 250:
        return None
    return float(frame["close"].tail(250).mean())
```

- [x] **Step 4: 运行并确认通过**

Run: `PYTHONPYCACHEPREFIX=/tmp/codex-pycache python3 -m unittest tests.test_portfolio_addition -v`
Expected: PASS。

---

### Task 2: 多周期技术评分

**Files:**
- Modify: `portfolio_addition.py`
- Test: `tests/test_portfolio_addition.py`

**Interfaces:**
- Consumes: `ResampledHistory`、`HoldingAssessmentInput.technical`、`calculate_ma250`。
- Produces: `score_period(bars, price, ma250)`、`score_technical(technical, yearly_score, monthly_score, weekly_score)`，两者返回 `float` 0–100。

- [x] **Step 1: 写失败测试**

```python
def _uptrend_bars(count=80):
    rows = []
    for index in range(count):
        date = pd.Timestamp("2020-01-01") + pd.Timedelta(days=index * (7 if count == 80 else 1))
        close = 10 + index * 0.2
        rows.append(Bar(str(date.date()), close - 0.2, close + 0.4, close - 0.5,
                        close, 1000 + index * 10))
    return tuple(rows)


def test_strong_period_scores_high():
    bars = _uptrend_bars()
    assert score_period(bars, bars[-1].close, bars[-1].close - 5) >= 80


def test_technical_score_uses_approved_period_weights():
    source = pd.DataFrame([{
        "date": f"2026-01-{day + 1:02d}", "open": 10, "high": 11, "low": 9,
        "close": 10 + day * 0.1, "volume": 1000,
    } for day in range(30)])
    technical = {"score": 5, "entry_can_buy": True, "trend_weak": False}
    assert score_technical(technical, 100.0, 95.0, 100.0) == 100.0
```

- [x] **Step 2: 运行并确认失败**

Run: `PYTHONPYCACHEPREFIX=/tmp/codex-pycache python3 -m unittest tests.test_portfolio_addition -v`
Expected: FAIL，原因是 `score_period`/`score_technical` 未定义。

- [x] **Step 3: 实现评分**

评分使用固定 0–100 子分：

- 趋势 40 分：`price > MA20` 15 分；MA20 较前值上升 10 分；`price > MA50` 5 分；`MA20 > MA50` 5 分；`price > MA250` 5 分。
- 位置 25 分：20 期价格位置在 35%–75% 得 25 分，20%–85% 得 18 分，其他得 8 分。
- 动量 20 分：3 期上涨 8 分、6 期上涨 7 分、最新周期收阳 5 分。
- 量能 15 分：最新完整周期量/前 20 期均量在 0.8–2.0 且周期上涨得 8 分；OBV 累计方向为正得 4 分；量增价升得 3 分。

`score_period` 对所有有效子分求和。`score_technical` 计算年K 35%、月K 30%、周K 20%、日线 15%：

```python
from statistics import mean


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, float(value)))


def score_period(bars, price, ma250) -> float:
    if len(bars) < 3:
        raise ValueError("period history requires at least 3 completed bars")
    closes = [bar.close for bar in bars]
    highs = [bar.high for bar in bars]
    lows = [bar.low for bar in bars]
    volumes = [bar.volume for bar in bars]
    ma20 = mean(closes[-20:])
    ma20_prev = mean(closes[max(0, len(closes) - 21):-1])
    ma50 = mean(closes[-50:])
    high20 = max(highs[-20:])
    low20 = min(lows[-20:])
    location = (price - low20) / (high20 - low20) if high20 > low20 else 0.5

    trend = 0.0
    trend += 15 if price > ma20 else 0
    trend += 10 if ma20 > ma20_prev else 0
    trend += 5 if price > ma50 else 0
    trend += 5 if ma20 > ma50 else 0
    if ma250 is not None:
        trend += 5 if price > ma250 else 0

    if 0.35 <= location <= 0.75:
        position = 25.0
    elif 0.20 <= location <= 0.85:
        position = 18.0
    else:
        position = 8.0

    momentum = 0.0
    momentum += 8 if closes[-1] > closes[max(0, len(closes) - 4)] else 0
    momentum += 7 if closes[-1] > closes[max(0, len(closes) - 7)] else 0
    momentum += 5 if bars[-1].close > bars[-1].open else 0

    average_volume = mean(volumes[max(0, len(volumes) - 21):-1])
    volume_ratio = volumes[-1] / average_volume if average_volume > 0 else 0
    positive_periods = sum(
        1 for index in range(1, len(closes)) if closes[index] > closes[index - 1]
    )
    obv = sum(
        math.copysign(volumes[index], closes[index] - closes[index - 1])
        for index in range(1, len(closes))
    )
    volume_score = 0.0
    volume_score += 8 if 0.8 <= volume_ratio <= 2.0 and closes[-1] > closes[-2] else 0
    volume_score += 4 if obv > 0 else 0
    volume_score += 3 if positive_periods > len(closes) / 2 and volume_ratio >= 1 else 0

    return clamp(trend + position + momentum + volume_score)


def score_technical(technical, yearly_score, monthly_score, weekly_score) -> float:
    base = 50.0 + float(technical.get("score") or 0) * 8.0
    if technical.get("entry_can_buy"):
        base += 15.0
    if technical.get("trend_weak"):
        base -= 30.0
    daily = clamp(base)
    return clamp(
        float(yearly_score) * 0.35
        + float(monthly_score) * 0.30
        + float(weekly_score) * 0.20
        + daily * 0.15
    )
```

- [x] **Step 4: 运行并确认通过**

Run: `PYTHONPYCACHEPREFIX=/tmp/codex-pycache python3 -m unittest tests.test_portfolio_addition -v`
Expected: PASS。

---

### Task 3: 基本面评分与否决

**Files:**
- Modify: `portfolio_addition.py`
- Test: `tests/test_portfolio_addition.py`

**Interfaces:**
- Consumes: `FundamentalSnapshot`。
- Produces: `score_fundamental(snapshot, current_price)` 返回 `float` 0–100；`fundamental_blockers(snapshot, current_price)` 返回 `tuple[str, ...]`。

- [x] **Step 1: 写失败测试**

```python
def test_alert_fundamental_blocks_addition():
    snapshot = FundamentalSnapshot(
        analyzed_at="2026-10-15", score=90, health_level="ALERT",
        revenue_yoy=10, profit_yoy=12, deduct_ratio=0.9, gross_margin=30,
        net_margin=15, ocf_to_profit=1.0, roe=12, current_pe=15, current_pb=2,
        pe_percentile=30, pb_percentile=30, fair_low=8, fair_high=12,
        risks=("立案调查",),
    )
    assert fundamental_blockers(snapshot, 10.0)


def test_sound_fundamental_scores_high():
    snapshot = FundamentalSnapshot(
        analyzed_at="2026-10-15", score=90, health_level="SOUND",
        revenue_yoy=15, profit_yoy=20, deduct_ratio=0.95, gross_margin=35,
        net_margin=18, ocf_to_profit=1.1, roe=15, current_pe=15, current_pb=2,
        pe_percentile=25, pb_percentile=25, fair_low=8, fair_high=12,
    )
    assert score_fundamental(snapshot, 10.0) >= 85
    assert fundamental_blockers(snapshot, 10.0) == ()
```

- [x] **Step 2: 运行并确认失败**

Run: `PYTHONPYCACHEPREFIX=/tmp/codex-pycache python3 -m unittest tests.test_portfolio_addition -v`
Expected: FAIL，原因是函数未定义。

- [x] **Step 3: 实现基本面评分**

- 财务质量 35 分：`35 * clamp(snapshot.score) / 100`。
- 现金流、利润率、ROE 25 分：三项 `clamp(value, 0, 100)` 后等权，其中 OCF/利润映射 `min(100, max(0, value * 75))`，净利率映射 `min(100, max(0, value * 5))`，ROE 映射 `min(100, max(0, value * 6))`。
- 估值安全度 25 分：价格在 `fair_low..fair_high` 得 25 分；低于下沿得 25 分；高于上沿 20% 以内按 `25 * (1 - 超出比例/0.2)`；超出 20% 得 0 分。PE/PB 分位均值再乘 `0.5 + (100 - mean_percentile) / 200`。
- 治理 15 分：无风险得 15 分，`WATCH` 文本每项减 5 分，`ALERT` 文本每项减 15 分，最低 0 分。

`fundamental_blockers` 必须返回以下原因：`ALERT`；连续双降；综合分低于 55；风险文本包含立案、重大违规、重大减值、持续减持；价格高于 `fair_high * 1.20` 且最新营收/净利未同时为正；快照为空或 `analyzed_at` 不是有效日期。

- [x] **Step 4: 运行并确认通过**

Run: `PYTHONPYCACHEPREFIX=/tmp/codex-pycache python3 -m unittest tests.test_portfolio_addition -v`
Expected: PASS。

---

### Task 4: 门槛、排名和买入数量

**Files:**
- Modify: `portfolio_addition.py`
- Test: `tests/test_portfolio_addition.py`

**Interfaces:**
- Consumes: 技术分、基本面分、可信止损位、持仓和资金字段。
- Produces: `trusted_stop_price(input, technical)`、`suggest_add_quantity(...)`、`rank_addition_candidates(candidates)`。

- [x] **Step 1: 写失败测试**

```python
def test_quantity_is_limited_by_risk_budget_and_rounded_down():
    shares = suggest_add_quantity(
        price=10.0, stop_price=9.5, total_capital=100000,
        available_capital=50000, current_value=10000,
        max_position_pct=20,
    )
    assert shares == 1000


def test_all_candidates_below_threshold_returns_none():
    candidates = (
        AdditionRecommendation(
            code="sh600001", name="甲", selected=False,
            composite_score=69, technical_score=70, fundamental_score=68,
            yearly_score=75, monthly_score=72, weekly_score=70,
            shares=0, amount=0, stop_price=9, position_pct_before=10,
            position_pct_after=10, reasons=("综合分不足",), missing=(),
        ),
    )
    assert rank_addition_candidates(candidates) is None
```

- [x] **Step 2: 运行并确认失败**

Run: `PYTHONPYCACHEPREFIX=/tmp/codex-pycache python3 -m unittest tests.test_portfolio_addition -v`
Expected: FAIL。

- [x] **Step 3: 实现门槛、止损和仓位**

```python
LOT = 100


def trusted_stop_price(technical, price):
    values = [
        float(technical.get("entry_stop_price") or 0),
        float(technical.get("weekly_stop_price") or 0),
    ]
    values = [value for value in values if math.isfinite(value) and 0 < value < price]
    return max(values) if values else 0.0


def suggest_add_quantity(*, price, stop_price, total_capital, available_capital,
                         current_value, max_position_pct):
    values = (price, stop_price, total_capital, max_position_pct)
    if not all(math.isfinite(float(value)) for value in values):
        return 0
    if price <= 0 or stop_price <= 0 or stop_price >= price:
        return 0
    if total_capital <= 0 or max_position_pct <= 0:
        return 0
    distance = max((price - stop_price) / price, 0.01)
    if distance > 0.10:
        return 0
    risk_shares = (total_capital * 0.005) / (price * distance)
    max_value = total_capital * max_position_pct / 100.0
    cap_shares = max(0.0, max_value - current_value) / price
    shares = min(risk_shares, cap_shares)
    if math.isfinite(float(available_capital)) and available_capital > 0:
        shares = min(shares, available_capital / price)
    return int(shares // LOT) * LOT
```

`rank_addition_candidates` 只接受 `selected=True`、综合分 ≥70、技术分 ≥65、数量 >0 的候选，按综合分、基本面分、年K分、月K分、周K分、仓位升序排序并返回第一个；其余返回 `None`。

- [x] **Step 4: 运行并确认通过**

Run: `PYTHONPYCACHEPREFIX=/tmp/codex-pycache python3 -m unittest tests.test_portfolio_addition -v`
Expected: PASS。

---

### Task 5: 单股评估编排

**Files:**
- Modify: `portfolio_addition.py`
- Test: `tests/test_portfolio_addition.py`

**Interfaces:**
- Consumes: `HoldingAssessmentInput`、`resample_ohlcv`、评分、基本面和仓位函数。
- Produces: `assess_holding(input, now)` 返回 `AdditionRecommendation`；`select_addition_candidate(inputs, now)` 返回 `Optional[AdditionRecommendation]`。

- [x] **Step 1: 写失败测试**

```python
def test_no_fundamental_snapshot_is_not_recommended():
    source = pd.DataFrame([{
        "date": (datetime(2025, 1, 1) + pd.Timedelta(days=index)).strftime("%Y-%m-%d"),
        "open": 10, "high": 11, "low": 9,
        "close": 10 + index * 0.05, "volume": 1000,
    } for index in range(1250)])
    item = HoldingAssessmentInput(
        "sh600001", "甲", 1000, 10, 15, 100000, 50000, 20,
        source, {"score": 6, "entry_can_buy": True, "trend_weak": False},
        None,
    )
    assert select_addition_candidate([item], datetime(2026, 10, 15, 15)) is None
```

- [x] **Step 2: 运行并确认失败**

Run: `PYTHONPYCACHEPREFIX=/tmp/codex-pycache python3 -m unittest tests.test_portfolio_addition -v`
Expected: FAIL。

- [x] **Step 3: 实现编排**

`assess_holding` 按以下顺序执行并返回所有证据：

1. 重采样 W/M/Y，校验完整年K ≥3、月K ≥12、周K ≥60。
2. 计算 MA250、三周期分、日线分和综合技术分。
3. 校验基本面 `analyzed_at` 不超过 30 天并计算基本面分/否决原因。
4. 计算可信止损位和参考股数。
5. 只有综合分 ≥70、技术分 ≥65、无基本面否决、年/月趋势非下行、日线非转弱、60 日位置 <85%、止损距离 ≤10%、股数 ≥100 时设置 `selected=True`。

`select_addition_candidate` 对所有输入调用 `assess_holding`，转换为 `AdditionRecommendation` 后调用 `rank_addition_candidates`。任何单股异常写入该股 `missing`，不阻断其他股票。

- [x] **Step 4: 运行并确认通过**

Run: `PYTHONPYCACHEPREFIX=/tmp/codex-pycache python3 -m unittest tests.test_portfolio_addition -v`
Expected: PASS。

---

### Task 6: 基本面缓存与数据服务

**Files:**
- Create: `portfolio_addition_store.py`
- Create: `portfolio_addition_service.py`
- Test: `tests/test_portfolio_addition_store.py`
- Test: `tests/test_portfolio_addition_integration.py`

**Interfaces:**
- Consumes: `fundamentals.fetch_and_analyze`、`StockMenuBar.get_stock_analysis_history`。
- Produces: `PortfolioAdditionStore.load(code)`、`save(code, snapshot)`、`is_fresh(snapshot, now)`；`PortfolioAdditionService.prepare_inputs(positions, technical_by_code, now)`。

- [x] **Step 1: 写失败测试**

```python
import tempfile
import unittest
from pathlib import Path


class PortfolioAdditionStoreTests(unittest.TestCase):
    def test_store_round_trip_and_expiry(self):
        with tempfile.TemporaryDirectory() as directory:
            store = PortfolioAdditionStore(Path(directory) / "addition-cache.json")
            snapshot = FundamentalSnapshot(
                "2026-10-15", 80, "SOUND", 10, 12, .9, 30, 15, 1, 12,
                15, 2, 25, 25, 8, 12,
            )
            store.save("sh600001", snapshot)
            self.assertEqual(store.load("sh600001"), snapshot)
            self.assertTrue(store.is_fresh(snapshot, datetime(2026, 10, 20, 15)))
            self.assertFalse(store.is_fresh(snapshot, datetime(2026, 11, 16, 15)))
```

- [x] **Step 2: 运行并确认失败**

Run: `PYTHONPYCACHEPREFIX=/tmp/codex-pycache python3 -m unittest tests.test_portfolio_addition_store -v`
Expected: FAIL，原因是类未定义。

- [x] **Step 3: 实现原子 JSON 缓存**

- `save` 写 `{version: 1, records: {code: asdict(snapshot)}}` 到同目录临时文件，`flush`、`fsync` 后 `os.replace`。
- `load` 校验 `version == 1`、代码、日期、数值字段类型；无效记录返回 `None` 并在 `last_error` 留原因。
- `is_fresh` 使用 `analyzed_at` 与当前时间差 ≤30 天；未来时间、无效日期返回 `False`。

`PortfolioAdditionService.prepare_inputs` 对每个实际持仓：

1. 调用 `history_fetcher(code, data_length=1250)`。
2. 读取缓存；缺失或过期时调用 `fundamentals.fetch_and_analyze(code, name)`，用 `fundamental_health.grade_result` 得到 `health_level` 并抽取 `FundamentalSnapshot`，成功后保存。
3. 网络失败时保留旧快照并把原因写入 `missing`。
4. 并发上限为 2，单股失败不传播给其他股票。

- [x] **Step 4: 运行并确认通过**

Run: `PYTHONPYCACHEPREFIX=/tmp/codex-pycache python3 -m unittest tests.test_portfolio_addition_store tests.test_portfolio_addition_integration -v`
Expected: PASS。

---

### Task 7: 持仓报告章节

**Files:**
- Modify: `portfolio_daily_report.py`
- Test: `tests/test_portfolio_addition_report.py`
- Modify: `tests/test_portfolio_daily_report.py`

**Interfaces:**
- Consumes: `AdditionRecommendation` 或无推荐状态。
- Produces: `format_addition_section(recommendation, evaluations, meta)` 返回 `list[str]`；`format_daily_report(..., addition=...)` 接受可选参数并生成章节。

- [x] **Step 1: 写失败测试**

```python
def test_report_shows_one_addition_candidate():
    recommendation = AdditionRecommendation(
        code="sh600001", name="甲", selected=True,
        composite_score=82, technical_score=78, fundamental_score=88,
        yearly_score=92, monthly_score=86, weekly_score=80,
        shares=300, amount=3000, stop_price=9.5, position_pct_before=12.0,
        position_pct_after=14.2,
        reasons=("年K上行", "基本面未见恶化"), missing=(),
    )
    text = format_daily_report({}, [], {}, {"generated_at": "15:02"},
                               addition=recommendation)
    assert "三、加仓机会" in text
    assert "甲（sh600001）" in text
    assert "300股" in text
    assert "四、优先处理" in text
    assert "五、逐股明细" in text


def test_report_does_not_force_candidate():
    text = format_daily_report({}, [], {}, {"generated_at": "15:02"},
                               addition=None)
    assert "当前不建议加仓" in text
```

- [x] **Step 2: 运行并确认失败**

Run: `PYTHONPYCACHEPREFIX=/tmp/codex-pycache python3 -m unittest tests.test_portfolio_addition_report -v`
Expected: FAIL。

- [x] **Step 3: 实现报告格式化**

推荐章节固定展示：股票/代码、综合/技术/基本面分、股数/金额、现价/止损、加仓前/后仓位、支持证据、数据日期和失效条件。无推荐时显示“当前不建议加仓”，列出前三个否决原因和缺失数据。异常时显示“加仓评估暂不可用，不影响原风险结论”。

`format_daily_report` 保持旧调用兼容，`addition=None` 时仍输出该章节；原第三、第四节标题分别改为“四、优先处理”和“五、逐股明细”。

- [x] **Step 4: 运行并确认通过**

Run: `PYTHONPYCACHEPREFIX=/tmp/codex-pycache python3 -m unittest tests.test_portfolio_addition_report tests.test_portfolio_daily_report -v`
Expected: PASS。

---

### Task 8: 调度集成与失败隔离

**Files:**
- Modify: `stock_menubar.py`
- Test: `tests/test_portfolio_addition_integration.py`

**Interfaces:**
- Consumes: `PortfolioAdditionService`、`select_addition_candidate`、`format_daily_report`。
- Produces: `_run_portfolio_analysis` 生成 `self._last_addition_assessment` 并传给日报。

- [x] **Step 1: 写失败测试**

```python
def test_addition_failure_keeps_existing_portfolio_report():
    app = FakeStockMenuBar()
    app.addition_service.prepare_inputs = Mock(side_effect=RuntimeError("network down"))
    app.run_portfolio_analysis()
    assert app.risk_results
    assert "加仓评估暂不可用" in app.daily_report
    assert app.notifications == []
```

- [x] **Step 2: 运行并确认失败**

Run: `PYTHONPYCACHEPREFIX=/tmp/codex-pycache python3 -m unittest tests.test_portfolio_addition_integration -v`
Expected: FAIL。

- [x] **Step 3: 实现集成**

- 在 `_run_portfolio_analysis` 中，原逐股风险结果生成后单独进入 `try/except`。
- 构造 `HoldingAssessmentInput` 时只取 `quantity > 0` 的持仓。
- `PortfolioAdditionService.prepare_inputs` 失败时设置 `_last_addition_assessment = {"error": str(error)}`，继续 `_update_daily_report`。
- 成功时调用 `select_addition_candidate`，保存结构化评估结果并传给 `format_daily_report`。
- 不调用 `_notify`、`portfolio.save` 或任何交易接口。

- [x] **Step 4: 运行并确认通过**

Run: `PYTHONPYCACHEPREFIX=/tmp/codex-pycache python3 -m unittest tests.test_portfolio_addition_integration -v`
Expected: PASS。

---

### Task 9: 文档、回归与完整验收

**Files:**
- Create: `docs/strategies/持仓多周期加仓评估.md`
- Modify: `A股监控使用说明.md`

**Interfaces:**
- Consumes: 已完成的计算、缓存、报告和调度行为。
- Produces: 用户可读的策略口径、参数、限制和报告入口。

- [x] **Step 1: 写文档**

文档必须包含：评估对象、1250 根日线与 W/M/Y 重采样、MA250、评分权重、基本面否决、0.5% 风险预算、最大仓位、100 股取整、数据过期、无建议条件和“不自动交易”声明。使用说明的持仓报告条目链接该文档。

- [x] **Step 2: 运行完整回归**

Run:

```bash
PYTHONPYCACHEPREFIX=/tmp/codex-pycache python3 -m unittest discover -s tests
PYTHONPYCACHEPREFIX=/tmp/codex-pycache python3 -m unittest test_intraday_tactics test_notification
PYTHONPYCACHEPREFIX=/tmp/codex-pycache python3 -m py_compile portfolio_addition.py portfolio_addition_store.py portfolio_addition_service.py portfolio_daily_report.py stock_menubar.py
```

Expected: 测试 0 failures/0 errors；允许项目原有 GUI 测试跳过；编译命令退出码 0。

- [x] **Step 3: 对照规格验收**

确认：最多一只推荐；证据不足不加仓；股数不超过风险预算/资金/仓位；基本面过期不推荐；报告编号更新；风险提醒、观察池和持仓数据不变。

