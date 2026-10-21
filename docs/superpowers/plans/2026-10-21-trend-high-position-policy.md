# 统一趋势与高位结构门控改造 Implementation Plan

> **For agentic workers:** REQUIRED SUBSKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用同一套可测试口径确认短期趋势、大趋势和高位结构，并据此统一控制个股分析、观察池、买入、接回、做 T、加仓和风险提醒。

**Architecture:** 新增两个纯计算策略模块：`trend_assessment.py` 负责多周期趋势与交易门控，`high_position_policy.py` 负责高位派发、整理、二波候选和二波确认；现有数据获取、状态机、报告和通知只负责组装数据与展示结果。趋势判断优先使用项目现有的 `entry_structure` M20/破位口径，并复用 `portfolio_addition` 的周线、月线未完成周期规则。

**Tech Stack:** Python 3.9、pandas、现有 `unittest`、现有新浪日线/15 分钟/30 分钟行情接口，不引入新依赖。

**Spec:** `/Users/stephen/Desktop/temp.txt`。原稿中的 Kotlin、Android、`HoldingTTrend`、`SwingTrend`、`TradeNotification`、`MonitorService` 等名称，按本计划的 Python 文件与函数映射执行，不照搬 Android 模块名。

## Global Constraints

- 趋势在前，入场结构、量能、15/30 分钟确认在后；单根 K 线、MACD 金叉/死叉、单次背离或单次放量不得独立触发普通买入。
- 大趋势 `DOWN` 时禁止普通买入、加仓、接回和普通做 T；只保留减仓、退出、破位、基本面红灯等风险处置。
- 大趋势 `BOTTOMING` 时只允许底部反转观察和轻仓试错，试错金额不超过“初始仓位金额的 25%”，不得包装为正常买入、加仓、接回或做 T。
- 正常买入只有在大趋势 `UP`、短期趋势 `UP/EXTENDED`、个股入场结构成立、高位结构允许、市场/板块允许、15/30 分钟执行条件同时通过时才能生成。
- 大趋势 `DOWN` 与短期 `UP/REBOUND` 冲突时必须显示“下跌趋势中的反弹”，不得显示为正常上涨趋势机会。
- 高位结构证据冲突或分钟数据缺失时输出 `HIGH_PENDING` / `MISSING` 并只观察，不预测“必然二波”或“随时下杀”。
- 风险信号优先级最高，不受普通机会开关、同类冷却和每日机会预算限制；普通机会仍受现有跃迁、确认轮数和冷却控制。
- 筹码峰只作为成交量加权估算证据，不能单独证明主力控盘、筹码锁定或洗盘完成。
- 日线、周线、月线只使用已结束周期；当日实时价格只能作为盘中确认，不得改写大趋势。
- 关键价格、涨跌幅、成交量、ATR、资金和比例必须是有限正数或明确 `None`；NaN、Infinity、负成交量和坏 OHLCV 一律降级为数据缺失。
- 测试使用项目现有 `unittest`，不引入 pytest。
- 用户未要求提交；执行过程中不要创建 Git 提交。每个任务以测试结果作为交付证据，如需提交必须再次获得用户明确授权。

## Project Mapping

原计划概念与当前 Python 项目对应如下：

| 原计划 | 本项目落位 |
|---|---|
| `TrendAssessmentPolicy` | 新建 `trend_assessment.py` |
| `HighPositionPolicy` | 新建 `high_position_policy.py` |
| `HoldingTTrend` / `SwingTrend` | 由 `entry_structure.EntryStructure`、`stock_analysis.analyze_history` 和 `portfolio_addition` 周期数据映射，不新增遗留命名 |
| `IntradayTactics` | 修改 `intraday_tactics.py`、`intraday_trade_policy.py` |
| `ObservationPool` | 修改 `observation_pool.py`、`observation_alerts.py` |
| `StockAnalysis` | 修改 `stock_analysis.py`、`technical_summary.py` |
| `PortfolioSwing` | 修改 `portfolio_addition.py`、`portfolio_addition_service.py` |
| `TradeNotification` | 修改 `portfolio_analysis.py`、`intraday_tactics.py`、`stock_menubar.py` |
| `MonitorService` | 修改 `stock_menubar.py` 的统一调度链路 |

## File Structure

- `trend_assessment.py`：创建。短期趋势、大趋势、综合趋势、冲突解释和交易门控的纯计算与序列化。
- `high_position_policy.py`：创建。高位派发风险、高位整理、二波候选、二波确认、证据冲突的纯计算与序列化。
- `entry_structure.py`：小幅修改。只提取/复用 M20、有效破位、ATR 和近 5 日趋势事实；保留现有入场结构行为。
- `stock_analysis.py`：修改。组装趋势与高位评估，把结果加入技术分析返回字典。
- `technical_summary.py`：修改。把统一趋势和高位结构显示在“技术分析摘要”中，不再用局部均线判断冒充完整趋势结论。
- `portfolio_analysis.py`：修改。持仓动作先经过统一交易门控，结果透传趋势、高位状态和限制理由。
- `intraday_trade_policy.py`：修改。买入/加仓/接回/做 T 接收趋势门控；风险处置不受普通门控限制。
- `intraday_tactics.py`：修改。机会信号受趋势与高位门控；风险信号保持最高优先级。
- `observation_pool.py`：修改。完整日线复核同时计算趋势与高位结构，按门控决定正常买点、底部观察或淘汰。
- `observation_alerts.py`：修改。观察池买点提醒带上趋势、高位结构和门控理由。
- `portfolio_addition.py`、`portfolio_addition_service.py`：修改。新增资金和加仓金额受大趋势、筑底试错上限和高位结构限制。
- `stock_menubar.py`：修改。统一组装数据、调用策略并输出结构化结果；不把网络获取放进纯策略模块。
- `tests/test_trend_assessment.py`：创建。覆盖短期、大趋势、综合冲突和数据缺失。
- `tests/test_high_position_policy.py`：创建。覆盖高位五态、证据冲突和二波确认。
- `tests/test_trade_gate_policy.py`：创建。覆盖正常买入、做 T、接回、筑底试错和风险优先级。
- `tests/test_intraday_trend_gate_integration.py`：创建。覆盖盘中机会降级、风险放行和安全文案。
- `tests/test_observation_pool.py`、`tests/test_portfolio_addition.py`、`tests/test_portfolio_daily_report.py`：修改并补充回归。
- `docs/strategies/统一趋势与高位结构决策.md`：创建。记录用户可见规则、阈值、阶段语义、数据边界和限制。
- `docs/README.md`、`A股监控使用说明.md`、`CHANGELOG.md`、`CHANGES_2026-10-21.md`：完成时更新。

---

### Task 1: 多周期趋势事实与统一趋势模型

**Files:**
- Create: `trend_assessment.py`
- Test: `tests/test_trend_assessment.py`
- Modify: `entry_structure.py`（仅在确有必要时提取共享常量/辅助函数）

**Interfaces:**
- Consumes: `portfolio_addition.resample_ohlcv`、`entry_structure.compute_entry_input`、`entry_structure.evaluate`，以及 OHLCV DataFrame。
- Produces: `ShortTrend(Enum)`、`MajorTrend(Enum)`、`TrendConflict(Enum)`、`TrendEvidence`、`TrendAssessment`、`assess_trend(history: pd.DataFrame, price: Optional[float], now: datetime, benchmark_ok: Optional[bool] = None) -> TrendAssessment`、`trend_assessment_to_dict(value: TrendAssessment) -> dict`。

- [ ] **Step 1: 写短期趋势失败测试**

```python
def test_short_up_requires_alignment_ma20_rising_and_no_break():
    result = assess_trend(_series_with_ma_alignment("up"), price=12.0, now=_close_time())
    self.assertEqual(result.short_trend, ShortTrend.UP)
    self.assertFalse(result.evidence.trend_weak)

def test_short_down_uses_existing_effective_ma20_break_rule():
    result = assess_trend(_series_breaking_ma20_two_days(), price=10.0, now=_close_time())
    self.assertEqual(result.short_trend, ShortTrend.DOWN)
    self.assertTrue(result.evidence.trend_weak)

def test_extended_requires_five_day_gain_and_up_days():
    result = assess_trend(_series_with_sharp_five_day_run(), price=15.0, now=_close_time())
    self.assertEqual(result.short_trend, ShortTrend.EXTENDED)
```

- [ ] **Step 2: 写大趋势和冲突失败测试**

```python
def test_major_up_uses_completed_week_month_and_ma250():
    result = assess_trend(_long_uptrend_history(), price=30.0, now=_close_time())
    self.assertEqual(result.major_trend, MajorTrend.UP)

def test_major_bottoming_requires_five_days_without_new_low():
    result = assess_trend(_bottoming_history(), price=11.0, now=_close_time())
    self.assertEqual(result.major_trend, MajorTrend.BOTTOMING)

def test_down_major_plus_rebound_short_is_not_normal_uptrend():
    result = assess_trend(_major_down_short_rebound(), price=11.0, now=_close_time())
    self.assertEqual(result.conflict, TrendConflict.DOWN_MAJOR_REBOUND)
    self.assertIn("下跌趋势中的反弹", result.summary)
```

- [ ] **Step 3: 运行测试确认模块/接口不存在而失败**

Run: `PYTHONPYCACHEPREFIX=/tmp/codex-pycache python3 -m unittest tests.test_trend_assessment -v`

Expected: FAIL，原因是 `trend_assessment` 尚未定义或状态枚举缺失。

- [ ] **Step 4: 实现数据清洗和已完成周期**

- 接受 pandas DataFrame，要求至少存在 `date/open/high/low/close/volume`；非法行丢弃，非法关键值返回 `MISSING` 而不是抛给 UI。
- 当日 15:00 前的当日日线只作为实时价格上下文，不进入 MA5/MA10/MA20、近 5 日或 MA250 统计。
- 周线、月线直接调用 `portfolio_addition.resample_ohlcv`，只使用 `.completed`；不在本模块重复实现周期边界。
- 大趋势最低数据边界沿用现有加仓口径：至少 250 根有效日线、60 根完整周线、12 根完整月线；不足时输出对应 `missing_data`。

- [ ] **Step 5: 实现短期状态**

- `UP`：收盘价不低于 MA5，`MA5 > MA10 > MA20`，且当前 MA20 高于 5 日前 MA20 的 `1.001` 倍。
- `EXTENDED`：近 5 日涨幅至少为 `max(6%, 3 × ATR14%)`，且最近 5 次收盘比较至少 3 次上涨；ATR 百分比使用当前 ATR14/现价。
- `DOWN`：沿用 `entry_structure` 的有效破位口径——价格低于 MA20 的 98%，或连续两日收于对应 MA20 的 99.5% 下方。
- `REBOUND`：收盘高于前一日、MA5 较前值上行，但尚未形成完整多头排列，且未有效破位。
- `RANGE`：以上都不满足。
- `MISSING`：不足 25 根有效日线或关键均线/ATR 无法计算。
- 状态优先级固定为 `MISSING > EXTENDED > DOWN > UP > REBOUND > RANGE`，并把被覆盖条件写入 `reasons`，避免同一输入因分支顺序变化而改变结果。

- [ ] **Step 6: 实现大趋势状态**

- 定义周线/月线方向：完整周期收盘高于该周期 MA20，且该周期 MA20 高于上一根完整周期 MA20 为 `UP`；收盘低于 MA20 且 MA20 下移为 `DOWN`；其他为 `RANGE`。
- `MajorTrend.UP`：周线 `UP`，月线 `UP/RANGE`，现价不低于 MA250。
- `MajorTrend.DOWN`：周线与月线均为 `DOWN`，或现价低于 MA250 且周线、月线均不为 `UP`。
- `MajorTrend.BOTTOMING`：此前存在 `DOWN` 证据或现价曾低于 MA250；最近 5 根已结束日线相对其之前的 20 日低点不再创新低；当前 MA20 走平或上行；价格站上 MA20；周线已不再是 `DOWN`。
- `MajorTrend.REPAIR`：存在周线/月线偏弱或价格低于 MA250，但筑底证据不完整。
- 不把当前未完成周线、月线或盘中价格计入确认。

- [ ] **Step 7: 实现综合状态和冲突解释**

- 保留 `short_trend`、`major_trend`、`conflict`、`reasons`、`missing_data`、`data_as_of`。
- 大趋势 `DOWN` + 短期 `UP/EXTENDED/REBOUND` → `DOWN_MAJOR_REBOUND`，摘要固定包含“下跌趋势中的反弹”。
- 大趋势 `BOTTOMING` + 短期转强 → `BOTTOM_REVERSAL_WATCH`，摘要固定包含“仅底部反转观察/轻仓试错”。
- 大趋势 `UP` + 短期 `UP/EXTENDED` → `ALIGNED_UP`。
- 其他组合输出 `NO_CONFIRMED_ALIGNMENT`，不使用确定性预测词。

- [ ] **Step 8: 运行趋势测试**

Run: `PYTHONPYCACHEPREFIX=/tmp/codex-pycache python3 -m unittest tests.test_trend_assessment tests.test_entry_structure tests.test_portfolio_addition -v`

Expected: PASS；现有入场结构和周期重采样回归不发生变化。

### Task 2: 交易门控纯策略

**Files:**
- Create: `tests/test_trade_gate_policy.py`
- Modify: `trend_assessment.py`

**Interfaces:**
- Consumes: `TrendAssessment`、入场结构状态、市场环境、高位结构状态、15/30 分钟证据。
- Produces: `TradePermissions`、`build_trade_permissions(trend, entry_can_buy, high_state, market_ok, sector_ok, timeframe, intent, total_capital, initial_entry_pct) -> TradePermissions`。

- [ ] **Step 1: 写普通买入门控失败测试**

```python
def test_normal_buy_requires_aligned_up_and_complete_execution_evidence():
    permissions = build_trade_permissions(
        trend=_aligned_up(), entry_can_buy=True, high_state=HighPositionState.NOT_HIGH,
        market_ok=True, sector_ok=True, timeframe=_confirmed_timeframes(),
        intent="buy", total_capital=100000.0, initial_entry_pct=5.0,
    )
    self.assertTrue(permissions.allow_normal_buy)

def test_down_major_blocks_buy_add_reentry_and_t():
    permissions = build_trade_permissions(
        trend=_major_down(), entry_can_buy=True, high_state=HighPositionState.NOT_HIGH,
        market_ok=True, sector_ok=True, timeframe=_confirmed_timeframes(),
        intent="buy", total_capital=100000.0, initial_entry_pct=5.0,
    )
    self.assertTrue(permissions.risk_only)
    self.assertFalse(permissions.allow_normal_buy)
    self.assertFalse(permissions.allow_reentry)
    self.assertFalse(permissions.allow_t)
```

- [ ] **Step 2: 写筑底、高位和风险优先级失败测试**

```python
def test_bottoming_allows_probe_capped_at_quarter_initial_position():
    permissions = build_trade_permissions(
        trend=_bottoming(), entry_can_buy=True,
        high_state=HighPositionState.NOT_HIGH, market_ok=True, sector_ok=True,
        timeframe=_confirmed_timeframes(), intent="probe_buy",
        total_capital=100000.0, initial_entry_pct=5.0,
    )
    self.assertTrue(permissions.allow_probe)
    self.assertLessEqual(permissions.probe_cap_ratio, 0.25)
    self.assertFalse(permissions.allow_normal_buy)

def test_risk_action_bypasses_all_normal_opportunity_blocks():
    permissions = build_trade_permissions(
        trend=_major_down(), entry_can_buy=False,
        high_state=HighPositionState.DISTRIBUTION_RISK, market_ok=False, sector_ok=False,
        timeframe=_missing_timeframes(), intent="risk_exit",
        total_capital=100000.0, initial_entry_pct=5.0,
    )
    self.assertTrue(permissions.allow_risk_action)

def test_high_distribution_blocks_reentry_and_t():
    permissions = build_trade_permissions(
        trend=_aligned_up(), entry_can_buy=True,
        high_state=HighPositionState.DISTRIBUTION_RISK, market_ok=True, sector_ok=True,
        timeframe=_confirmed_timeframes(), intent="do_t",
        total_capital=100000.0, initial_entry_pct=5.0,
    )
    self.assertFalse(permissions.allow_reentry)
    self.assertFalse(permissions.allow_t)
```

- [ ] **Step 3: 运行测试确认接口缺失而失败**

Run: `PYTHONPYCACHEPREFIX=/tmp/codex-pycache python3 -m unittest tests.test_trade_gate_policy -v`

- [ ] **Step 4: 实现门控矩阵**

- 正常买入：`major_trend=UP`、`short_trend in {UP, EXTENDED}`、`entry_can_buy=True`、高位允许、市场/板块允许、15/30 分钟证据完整。
- 加仓：在正常买入基础上另需现有持仓允许加仓；大趋势 `DOWN` 为 0。
- 接回：只允许大趋势 `UP`、短期 `UP/EXTENDED`、回踩未破位且高位非派发风险/二波候选。
- 普通做 T：只允许大趋势 `UP`、短期 `UP/EXTENDED`、高位非派发风险/整理/二波候选，并通过现有 15/30 分钟策略。
- 筑底试错：只有 `BOTTOMING` 且连续 5 日不创新低时允许；输出 `probe_cap_ratio=0.25`，金额上限按 `total_capital × initial_entry_pct × 25%` 计算。
- 风险处置：无论其他门控如何均允许，但继续遵守 A 股 T+1 可卖数量限制。
- 所有 `MISSING` 数据只能降级为观察，不得默认放行普通机会。

- [ ] **Step 5: 运行门控测试**

Run: `PYTHONPYCACHEPREFIX=/tmp/codex-pycache python3 -m unittest tests.test_trade_gate_policy -v`

Expected: PASS。

### Task 3: 高位结构五态策略

**Files:**
- Create: `high_position_policy.py`
- Test: `tests/test_high_position_policy.py`

**Interfaces:**
- Consumes: 近 60 日已完成 OHLCV、当前价格、ATR14、量比、`PriceContext`、支撑/压力、已完成 15/30 分钟证据、市场环境、板块状态、可选筹码估算。
- Produces: `HighPositionState(Enum)`、`HighPositionEvidence`、`HighPositionAssessment`、`assess_high_position(value: HighPositionInput) -> HighPositionAssessment`、`high_position_to_dict(value: HighPositionAssessment) -> dict`。

- [ ] **Step 1: 写五态和优先级失败测试**

```python
def test_high_volume_stall_and_dropping_pivots_are_distribution_risk():
    result = assess_high_position(_distribution_fixture())
    self.assertEqual(result.state, HighPositionState.DISTRIBUTION_RISK)

def test_narrow_range_shrink_pullback_and_rising_lows_are_consolidation():
    result = assess_high_position(_high_consolidation_fixture())
    self.assertEqual(result.state, HighPositionState.HIGH_CONSOLIDATION)

def test_candidate_requires_second_wave_conditions_without_full_breakout():
    result = assess_high_position(_second_wave_candidate_fixture())
    self.assertEqual(result.state, HighPositionState.SECOND_WAVE_CANDIDATE)

def test_second_wave_requires_05pct_breakout_volume_and_timeframes():
    result = assess_high_position(_confirmed_second_wave_fixture())
    self.assertEqual(result.state, HighPositionState.SECOND_WAVE_CONFIRMED)

def test_conflicting_distribution_and_support_evidence_stays_pending():
    result = assess_high_position(_conflicting_fixture())
    self.assertEqual(result.state, HighPositionState.HIGH_PENDING)
```

- [ ] **Step 2: 写非高位和数据缺失失败测试**

```python
def test_mid_or_low_price_is_not_high_position_state():
    result = assess_high_position(_mid_range_fixture())
    self.assertEqual(result.state, HighPositionState.NOT_HIGH)

def test_missing_minutes_cannot_confirm_second_wave():
    result = assess_high_position(_second_wave_fixture_without_minutes())
    self.assertIn(result.state, {HighPositionState.SECOND_WAVE_CANDIDATE, HighPositionState.HIGH_PENDING})
    self.assertFalse(result.evidence.second_wave_confirmed)
```

- [ ] **Step 3: 运行测试确认模块不存在而失败**

Run: `PYTHONPYCACHEPREFIX=/tmp/codex-pycache python3 -m unittest tests.test_high_position_policy -v`

- [ ] **Step 4: 实现证据计算**

- 高位沿用 `price_position.PriceLocation.HIGH`：近 60 日位置至少 2/3，且窗口累计涨幅至少 20%。
- 高位区间宽度必须同时满足相对阈值才视为“窄幅”：区间价差不超过 8%，且不超过 `4 × ATR14`。
- 低点抬高定义为最近 3 根完整日线低点的最小值高于前 3 根低点的最小值，且最近一根低点不低于前一根低点。
- 放量滞涨定义为量比至少 1.3，当日收跌或收盘位于当根 K 线下部 40%；连续上影、高点/低点下移、跌时放量/反弹缩量作为加重证据。
- 整理蓄势要求高位、窄幅、回踩量比不高于 0.9、低点抬高、价格守住 MA10/MA20/前期突破位、没有连续放量下跌，且市场/板块未明显恶化。
- `SECOND_WAVE_CANDIDATE`：已满足整理蓄势，短期趋势重新向上、回踩未破区间下沿、反弹量能恢复、市场/板块健康，但尚未满足完整突破。
- `SECOND_WAVE_CONFIRMED`：价格突破区间高点至少 0.5%，量比至少 1.3，15 分钟至少 2 次完成确认、30 分钟方向不向下，回踩突破位不破，且大趋势和高位门控允许。
- `DISTRIBUTION_RISK` 的确定证据优先于整理；若派发证据与低点抬高/支撑守住等证据冲突，输出 `HIGH_PENDING`，不得强行二波或下杀。
- 筹码估算只能加减证据强度，不能单独把状态改成派发风险、整理或二波确认。

- [ ] **Step 5: 实现理由和失效参考**

- 返回 `reasons`、`blockers`、`missing_data`、`breakout_price`、`invalidation_price`、`confidence`。
- 整理和二波候选以区间下沿、MA20、前期突破位中的最近有效支撑为失效参考。
- 二波确认以突破位为首要失效参考；无有效 ATR 或支撑时为 `None` 并说明缺失，不编造价格。

- [ ] **Step 6: 运行高位策略测试**

Run: `PYTHONPYCACHEPREFIX=/tmp/codex-pycache python3 -m unittest tests.test_high_position_policy -v`

Expected: PASS。

### Task 4: 接入个股技术分析与统一报告

**Files:**
- Modify: `stock_analysis.py`
- Modify: `technical_summary.py`
- Modify: `portfolio_analysis.py`
- Test: `tests/test_stock_menubar_import.py`、`tests/test_portfolio_analysis.py`

**Interfaces:**
- Consumes: `assess_trend`、`assess_high_position`、`build_trade_permissions`。
- Produces: `analyze_history(history, code, name, live_data=None, benchmark_history=None)` 返回新增 `trend_assessment`、`high_position`、`trade_permissions`；`TechnicalSummary` 显示趋势/高位/门控；持仓结果透传相同结构。

- [ ] **Step 1: 写技术分析集成失败测试**

```python
def test_analyze_history_returns_trend_high_position_and_permissions():
    result = analyze_history(_long_history(), "sh600000", "示例", live_data={"price": 30.0})
    self.assertEqual(result["trend_assessment"]["major_trend"], "UP")
    self.assertIn(result["high_position"]["state"], {"NOT_HIGH", "HIGH_CONSOLIDATION"})
    self.assertIn("allow_normal_buy", result["trade_permissions"])

def test_summary_puts_trend_before_action():
    text = format_summary(TechnicalSummary.from_result(result))
    self.assertIn("短期趋势", text)
    self.assertIn("大趋势", text)
    self.assertIn("高位结构", text)
```

- [ ] **Step 2: 写持仓门控失败测试**

```python
def test_major_down_position_report_only_offers_risk_disposition():
    result = evaluate_position(
        position=_down_trend_position(), technical=_down_trend_technical(),
        live_data=_down_trend_live_data(), portfolio=_portfolio_context(),
        fundamental_health=_healthy_fundamentals(), predicted_market_turnover=18000.0,
        market_change_pct=-1.0, sector_change_pct=-1.2,
    )
    self.assertFalse(result["trade_permissions"]["allow_normal_buy"])
    self.assertFalse(result["trade_permissions"]["allow_t"])
    self.assertIn("风险", result["action"])
```

- [ ] **Step 3: 运行测试确认新增字段缺失而失败**

Run: `PYTHONPYCACHEPREFIX=/tmp/codex-pycache python3 -m unittest tests.test_stock_menubar_import tests.test_portfolio_analysis -v`

- [ ] **Step 4: 接入数据组装**

- `stock_analysis.analyze_history` 在既有 `entry_structure` 之后调用 `assess_trend` 和 `assess_high_position`，不改变旧字段语义。
- `entry_structure.entry_can_buy` 只表示个股结构成立，不再直接等于允许买入；最终买入以 `trade_permissions.allow_normal_buy` 为准。
- `technical_summary` 新增短期趋势、大趋势、趋势冲突、高位结构和限制条件；保留原评分、量价、动量、位置、入场结构字段。
- `portfolio_analysis.evaluate_position` 先计算风险状态，再用 `build_trade_permissions` 限制动作；风险分支不能被普通门控覆盖。
- 所有报告字段使用 `asdict`/简单字典序列化，禁止把 Enum 对象直接写入 JSON。

- [ ] **Step 5: 运行报告回归**

Run: `PYTHONPYCACHEPREFIX=/tmp/codex-pycache python3 -m unittest tests.test_stock_menubar_import tests.test_portfolio_analysis tests.test_portfolio_daily_report -v`

Expected: PASS。

### Task 5: 盘中买卖、接回与做 T 门控

**Files:**
- Modify: `intraday_trade_policy.py`
- Modify: `intraday_tactics.py`
- Modify: `stock_menubar.py`
- Test: `tests/test_trade_gate_policy.py`、`tests/test_intraday_trend_gate_integration.py`

**Interfaces:**
- Consumes: `TrendAssessment`、`HighPositionAssessment`、`TradePermissions`、`TimeframeEvidence`、现有 `PositionEligibility`。
- Produces: `evaluate_trade_action(intent, evidence, eligibility, invalidation_price, permissions=None, trend=None, high_position=None) -> TradeDecision`。

- [ ] **Step 1: 写盘中机会降级失败测试**

```python
def test_major_down_suppresses_normal_buy_and_reentry_signals():
    signals = _evaluate_intraday(
        code="sh600000", name="示例", price=10.0, change_pct=1.0, volume_ratio=1.2,
        trend=_major_down(), permissions=_risk_only(),
    )
    self.assertNotIn(signal.signal_type for signal in signals, {IntradaySignalType.BUY, IntradaySignalType.REENTRY})

def test_down_major_rebound_copy_says_rebound_not_uptrend():
    signal = _evaluate_intraday(
        code="sh600000", name="示例", price=10.0, change_pct=1.0, volume_ratio=1.2,
        trend=_down_major_rebound(), permissions=_risk_only(),
    )
    self.assertIn("下跌趋势中的反弹", signal.body)
    self.assertNotIn("上涨趋势确认", signal.body)
```

- [ ] **Step 2: 写风险优先级和高位做 T 失败测试**

```python
def test_risk_exit_is_not_suppressed_by_trend_gate():
    decision = evaluate_trade_action(
        "risk_exit", _risk_confirmed_timeframes(), _sellable_position(),
        invalidation_price=9.5, permissions=_risk_only(),
    )
    self.assertEqual(decision.action, DecisionAction.RISK_EXIT)

def test_high_consolidation_blocks_reentry_and_t_before_second_wave_confirmation():
    permissions = build_trade_permissions(
        trend=_aligned_up(), entry_can_buy=True,
        high_state=HighPositionState.HIGH_CONSOLIDATION, market_ok=True, sector_ok=True,
        timeframe=_confirmed_timeframes(), intent="do_t",
        total_capital=100000.0, initial_entry_pct=5.0,
    )
    self.assertFalse(permissions.allow_reentry)
    self.assertFalse(permissions.allow_t)
```

- [ ] **Step 3: 运行测试确认门控参数未接入而失败**

Run: `PYTHONPYCACHEPREFIX=/tmp/codex-pycache python3 -m unittest tests.test_trade_gate_policy tests.test_intraday_trend_gate_integration -v`

- [ ] **Step 4: 接入现有信号优先级**

- 先处理跌破支撑、确认破位、高位转弱、基本面红灯、深套退出；这些分支不读普通机会预算。
- 普通买入、加仓、接回、冲高做 T、回踩接回在进入现有跃迁/冷却前调用 `build_trade_permissions`。
- 未通过门控的可执行机会降级为 `TRADE_WAIT` 或 `None`，文案写明“仅观察”和首要限制原因；不得仍出现“可买/可接回/可做 T”。
- `BOTTOMING` 只生成“底部反转观察/轻仓试错”，试错数量由资金计算器按 25% 初始仓位金额上限向下取整到 100 股。
- `SECOND_WAVE_CANDIDATE` 不生成突破买入、接回或做 T；只有 `SECOND_WAVE_CONFIRMED` 才允许二波突破提醒。
- 15/30 分钟数据不完整、未完成或方向冲突时，任何普通机会均保持等待。

- [ ] **Step 5: 运行盘中相关回归**

Run: `PYTHONPYCACHEPREFIX=/tmp/codex-pycache python3 -m unittest test_intraday_tactics tests.test_intraday_notification tests.test_intraday_trade_integration tests.test_intraday_deferred_confirmation tests.test_signal_confidence -v`

Expected: PASS。

### Task 6: 观察池、新增资金与通知接入

**Files:**
- Modify: `observation_pool.py`
- Modify: `observation_alerts.py`
- Modify: `portfolio_addition.py`
- Modify: `portfolio_addition_service.py`
- Modify: `portfolio_daily_report.py`
- Test: `tests/test_observation_pool.py`、`tests/test_observation_integration.py`、`tests/test_portfolio_addition.py`、`tests/test_portfolio_addition_integration.py`、`tests/test_portfolio_daily_report.py`

**Interfaces:**
- Consumes: 任务 1–5 的 `TrendAssessment`、`HighPositionAssessment`、`TradePermissions`。
- Produces: 观察记录新增 `trend_assessment/high_position/trade_permissions`；加仓推荐新增趋势限制和最终可买金额；报告先展示趋势与高位状态，再展示动作。

- [ ] **Step 1: 写观察池门控失败测试**

```python
def test_major_down_candidate_is_not_ready():
    pool.review(
        hits=_candidate_hits(), at=_close_time(), histories=_major_down_histories(),
        benchmark=_benchmark(), names={"sh600000": "示例"},
    )
    self.assertEqual(pool.records["sh600000"]["status"], "observing")
    self.assertFalse(pool.records["sh600000"]["can_enter"])
    self.assertIn("下跌趋势", pool.records["sh600000"]["reason"])

def test_bottoming_candidate_is_bottom_watch_not_normal_buy():
    pool.review(
        hits=_candidate_hits(), at=_close_time(), histories=_bottoming_histories(),
        benchmark=_benchmark(), names={"sh600000": "示例"},
    )
    self.assertEqual(pool.records["sh600000"]["trend_mode"], "BOTTOM_REVERSAL_WATCH")
    self.assertFalse(pool.records["sh600000"]["can_enter"])
```

- [ ] **Step 2: 写新增资金门控失败测试**

```python
def test_major_down_recommendation_has_zero_new_capital():
    result = assess_holding(_down_trend_input(), now=_close_time())
    self.assertFalse(result.selected)
    self.assertEqual(result.shares, 0)
    self.assertIn("大趋势向下", result.reasons)

def test_bottoming_probe_is_capped_at_quarter_initial_position():
    result = assess_holding(_bottoming_input(), now=_close_time())
    self.assertLessEqual(result.amount, _initial_position_amount(result) * 0.25)
```

- [ ] **Step 3: 运行测试确认字段/门控缺失而失败**

Run: `PYTHONPYCACHEPREFIX=/tmp/codex-pycache python3 -m unittest tests.test_observation_pool tests.test_portfolio_addition -v`

- [ ] **Step 4: 接入观察池**

- `ObservationPool._assess` 在 `entry.evaluate` 后调用统一趋势和高位策略。
- 只有 `allow_normal_buy` 才能把状态置为 `ready`；`BOTTOM_REVERSAL_WATCH` 只能为观察/轻仓试错；大趋势 `DOWN` 不能晋级正常买点。
- 记录保存完整结构化理由和缺失数据；连续两日趋势转弱淘汰逻辑保持兼容，不因新增状态破坏旧 JSON。
- `observation_alerts` 的买点提醒正文先写“短期趋势/大趋势/高位结构”，再写入场结构、资金流和失效价。

- [ ] **Step 5: 接入新增资金和报告**

- `portfolio_addition.assess_holding` 把大趋势向下作为硬否决，推荐股数和金额必须为 0。
- `BOTTOMING` 只输出试错金额，且不超过 `total_capital × initial_entry_pct × 25%`，不得进入正常加仓排名。
- `SECOND_WAVE_CANDIDATE` 或 `DISTRIBUTION_RISK` 时正常新增资金为 0；`SECOND_WAVE_CONFIRMED` 仍需趋势、市场和 15/30 分钟门控全部通过。
- `portfolio_daily_report`、持仓摘要和盘中通知按“趋势 → 高位结构 → 限制 → 操作 → 失效参考”排序。
- 删除或改写“必然上涨、随时下杀、一定二波、确认洗盘完成”等确定性文案。

- [ ] **Step 6: 运行观察池和资金回归**

Run: `PYTHONPYCACHEPREFIX=/tmp/codex-pycache python3 -m unittest tests.test_observation_pool tests.test_observation_integration tests.test_observation_alerts tests.test_portfolio_addition tests.test_portfolio_addition_integration tests.test_portfolio_daily_report -v`

Expected: PASS。

### Task 7: 全链路回归、数据降级与文档

**Files:**
- Modify: `docs/strategies/统一趋势与高位结构决策.md`
- Modify: `docs/README.md`
- Modify: `A股监控使用说明.md`
- Modify: `CHANGELOG.md`
- Create: `CHANGES_2026-10-21.md`

- [ ] **Step 1: 写边界回归测试**

- 日线不足 25 根：短期趋势 `MISSING`，禁止普通机会。
- 日线不足 250 根或周/月完整周期不足：大趋势 `MISSING`，只观察。
- 分钟 K 线缺失、未完成、时间戳过期：二波不能确认，普通买入/做 T 不能执行。
- 板块或大盘数据缺失：市场过滤降级为观察，不得默认视为健康。
- 筹码数据缺失或估算冲突：高位状态最多为待定，不产生确定性结论。
- NaN、Infinity、负成交量、坏 OHLCV：不抛出 UI 异常，输出结构化缺失原因。
- 风险信号在大趋势向下、高位派发和数据缺失时仍可触发；A 股 T+1 可卖限制继续生效。

- [ ] **Step 2: 运行全量测试**

Run: `PYTHONPYCACHEPREFIX=/tmp/codex-pycache python3 -m unittest discover -v`

Expected: 0 failures、0 errors。若原测试固定允许跳过，记录原有 skip 数并在最终结果中明确列出，不把新增 skip 当成功。

- [ ] **Step 3: 运行重点导入与回归**

Run: `PYTHONPYCACHEPREFIX=/tmp/codex-pycache python3 -m unittest tests.test_stock_menubar_import tests.test_menu_format tests.test_all_intraday_action_surfaces tests.test_target_cost_strategy -v`

Expected: PASS。

- [ ] **Step 4: 更新策略文档**

- `docs/strategies/统一趋势与高位结构决策.md` 必须写清状态定义、全部阈值、优先级、交易门控矩阵、筑底 25% 上限、高位筹码限制和数据降级行为。
- `docs/README.md` 的策略表登记新文档、两个纯策略模块和对应测试。
- `A股监控使用说明.md` 说明趋势/高位状态如何影响买入、接回、做 T、加仓和风险提醒。
- `CHANGELOG.md` 与 `CHANGES_2026-10-21.md` 记录原因、规则变化、验证结果和已知限制。

- [ ] **Step 5: 文案一致性检查**

- 使用 `rg -n "必然上涨|随时下杀|一定二波|确认洗盘完成|正常买入|接回|做 T"` 检查报告和通知文案。
- 大趋势向下、筑底、二波候选场景不得残留普通机会表述。
- 风险提示仍优先、明确、可执行；观察提示不得伪装成交易指令。

- [ ] **Step 6: 再次运行全量测试并交付**

Run: `PYTHONPYCACHEPREFIX=/tmp/codex-pycache python3 -m unittest discover -v`

交付时列出：新增文件、修改文件、测试命令、测试统计、关键兼容行为和已知限制。未经用户明确要求，不创建 Git 提交。

## Recommended Execution Order

1. Task 1 和 Task 2 是核心口径，必须先完成并通过测试。
2. Task 3 可与 Task 1/2 的部分测试并行设计，但集成只能在门控接口稳定后开始。
3. Task 4 先接入静态个股报告，验证数据结构后再做盘中门控。
4. Task 5 完成盘中机会和风险优先级，再接观察池与资金。
5. Task 6 验证跨模块动作约束，最后执行 Task 7 全量回归与文档。

## Definition of Done

- 任一股票都能返回结构化 `short_trend`、`major_trend`、`high_position`、`trade_permissions`、`reasons` 和 `missing_data`。
- 大趋势向下不再产生普通买入、接回、加仓或普通做 T；风险提醒不被抑制。
- 筑底只产生观察/试错，且试错金额不超过初始仓位金额的 25%。
- 二波只有在价格、量能、15/30 分钟、趋势和高位门控全部确认后才能提醒。
- 个股分析、持仓报告、观察池、盘中通知和新增资金使用同一套状态与理由。
- 全量 `unittest discover` 无 failure/error，文档与实际阈值一致。
