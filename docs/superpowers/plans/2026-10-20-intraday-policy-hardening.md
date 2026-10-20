# 盘中交易策略安全加固 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修正T+1、K线时效、信号状态机和多入口话术中的剩余风险，使所有盘中操作提醒共用一套可验证的确定性边界。

**Architecture:** 以交易批次账本和行情交易日期作为交易资格唯一来源；在信号进入已触发状态前完成15/30分钟证据判断；最后把统一动作守卫接入所有仍会输出买卖/做T文字的盘中入口。

**Tech Stack:** Python 3、新浪盘中K线、JSON交易账本、`unittest`。

**Spec:** `docs/superpowers/specs/2026-10-20-intraday-t1-dual-timeframe-design.md`

## Global Constraints

- 行情中的交易日期是T+1判断日期，不能使用本机自然日期代替。
- 未完成或过期K线不得产生确定性买入、卖出或做T动作。
- 买入需要连续两个新鲜的已完成15分钟K线确认且30分钟不反向。
- 风险卖出只等待一个新鲜的已完成15分钟K线，不等待30分钟。
- 普通卖出和做T只使用当日可卖批次，数量不得超过账本可卖数量。
- 所有买卖/做T文案必须经过同一个动作守卫；未通过时只能输出观察、风险事实或次日处置。
- 此目录不是Git仓库；不创建提交，以测试输出作为交付证据。

## 本轮换模型后的计划修订

- 策略主轴不变：买点保守，风险卖点及时；15分钟负责执行确认，30分钟负责买入和普通卖出/做T的方向过滤。
- Task 1已完成实现，但计划勾选状态此前未更新；Task 2已实现行情日期主路径，仍需修复集成测试夹具并清除剩余本机日期入口。
- Task 3不再把“K线日期新鲜”当作完整证据。即使K线属于今天，只要不是当前交易时段最新的已完成槽位，也必须降级为等待。
- Task 4把“站稳”和“明显上影拒绝”改为可测试的确定阈值，避免执行人员自行解释。
- Task 5禁止依赖文案正则决定能否交易。所有入口先形成结构化动作意图，再由统一守卫核验日期、K线证据和T+1资格；旧文案只作为需要清洗的输入。
- Task 6区分真实交易和人工账务调整。成本修正不得伪造成交批次；数量增减必须记录生效日期并遵守当日可卖限制。
- Task 7新增集成夹具回归和动作话术清单，当前`tests/test_intraday_trade_integration.py`因未传行情日期产生的4项失败必须清零后才能验收。

---

### Task 1: 修复“等待确认后永远不再触发”的状态机缺口

**Files:**
- Modify: `intraday_tactics.py`
- Modify: `stock_menubar.py`
- Test: `tests/test_intraday_deferred_confirmation.py`

**Interfaces:**
- Consumes: `IntradayTactics.check_signal(...)`、`StockMenuBar._apply_intraday_trade_policy(...)`。
- Produces: `IntradayTactics.defer_signal(code, signal_type) -> None`，用于策略降级为等待后释放跃迁状态但保留条件上下文。

当前缺口：`check_signal`先把原始信号写入`triggered`，策略随后发现15/30分钟未确认并降级；下一轮K线确认时`check_signal`会因“已触发”直接静默，真正可执行提醒可能永远不出现。

- [x] **Step 1: 编写失败测试**

```python
def test_waited_buy_fires_after_timeframe_confirmation():
    first = tactics.check_signal(**candidate_kwargs)
    tactics.defer_signal("sz000737", first.signal_type)
    second = tactics.check_signal(**candidate_kwargs)
    self.assertIsNotNone(second)

def test_condition_disappearance_still_rearms_normally():
    tactics.defer_signal("sz000737", IntradaySignalType.T_BUY)
    self.assertIsNone(tactics.check_signal(**not_candidate_kwargs))
    self.assertIsNotNone(tactics.check_signal(**candidate_kwargs))
```

- [x] **Step 2: 运行测试确认当前状态机失败**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_intraday_deferred_confirmation -v`

- [x] **Step 3: 在`_apply_intraday_trade_policy`返回`TRADE_WAIT`时调用`defer_signal`，并用测试证明两轮后可执行**

- [x] **Step 4: 运行盘中状态机、策略和通知测试**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_intraday_deferred_confirmation tests.test_intraday_trade_integration test_intraday_tactics tests.test_intraday_notification -v`

### Task 2: T+1使用行情交易日期并统一账本来源

**Files:**
- Modify: `trade_ledger.py`
- Modify: `stock_menubar.py`
- Test: `tests/test_trade_date_authority.py`

**Interfaces:**
- Produces: `TradeLedger.position_summary(code, as_of_trade_date) -> dict`继续保留，但调用方必须传行情日期。
- Produces: `StockMenuBar._current_trade_date(code, live) -> str | None`，优先取该股票行情日期；缺失时返回`None`。
- Produces: `StockMenuBar._apply_intraday_trade_policy(..., trade_date: str | None = None)`；有效日期必须来自行情，任何分支都不得回退到`datetime.now()`。

当前缺口：`position_summary`现在使用`datetime.now().date()`；节假日、跨日缓存和行情日期异常时可能误判可卖。项目文档已明确“行情日期是唯一交易日判据”。

- [x] **Step 1: 编写失败测试**

```python
def test_sellable_is_compared_to_quote_date_not_wall_clock():
    ledger.record_trade("buy", "sz000737", "北方铜业", "2026-10-20", 100, 12.13)
    self.assertEqual(ledger.sellable_quantity("sz000737", "2026-10-20"), 0)
    self.assertEqual(ledger.sellable_quantity("sz000737", "2026-10-21"), 100)

def test_missing_quote_date_blocks_all_day_trading_actions():
    decision = policy_for_quote_date(None)
    self.assertEqual(decision.action, DecisionAction.WAIT)
    self.assertIn("行情日期", decision.message)
```

- [x] **Step 2: 运行测试确认调用方仍使用本机日期或缺日期直接行动**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_trade_date_authority -v`

- [x] **Step 3: 从`live`提取行情日期；缺日期时把买入、普通卖出和做T降级为等待，风险卖出只发风险事实和次日计划**

- [x] **Step 4: 运行账本、集成和策略测试**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_trade_ledger tests.test_trade_date_authority tests.test_intraday_trade_integration tests.test_intraday_trade_policy -v`

- [x] **Step 5: 修正既有集成测试夹具并清除剩余本机日期入口**

`tests/test_intraday_trade_integration.py`中每个正常交易场景显式传入`trade_date="2026-10-20"`或构造含`date`字段的`stock_data`；缺日期行为只由`tests/test_trade_date_authority.py`覆盖。删除`_check_intraday_signals`用第一只持仓行情日期代表全部股票的临时逻辑，过滤事件和每个动作都使用当前股票自己的行情日期。再次运行Step 4，期望4项集成失败全部消失。

### Task 3: 校验新浪K线时间语义与新鲜度

**Files:**
- Modify: `intraday_bars.py`
- Test: `tests/test_intraday_bar_freshness.py`

**Interfaces:**
- Produces: `parse_intraday_rows(rows, scale, now, timestamp_semantics="end") -> list[dict]`；规范化结果中的`timestamp`表示K线结束时间。
- Produces: `latest_completed_slot(now, scale, quote_date) -> str | None`，按A股`09:30-11:30`、`13:00-15:00`连续竞价段计算最新已完成槽位。
- Produces: `build_timeframe_evidence(..., now, quote_date) -> TimeframeEvidence`；只有最后一根15/30分钟K线分别等于最新已完成槽位时，`complete`才可为`True`。

当前缺口：代码把`day`当K线开始时间再加周期，但新浪接口的`day`可能是该K线结束时间；现有测试自行假定了语义。此外旧K线也能被标成`complete=True`。测试不访问网络，但实现前必须保存一份最小原始响应样本并记录`day`序列；如果无法确认语义，默认采用更保守的`end`解释，并让歧义样本进入安全失败。

- [x] **Step 1: 保存最小脱敏原始响应样本并确认`day`语义**

样本只保留连续三条15分钟和三条30分钟记录的`day/open/high/low/close/volume`。相邻记录时间差必须分别等于15和30分钟；结合返回长度与请求时间判断`day`是开始还是结束。把样本保存为`tests/fixtures/sina_intraday_timestamps.json`，测试不访问网络。

- [x] **Step 2: 编写失败测试**

```python
def evidence_with_latest_bars(now, last_15m, last_30m):
    bars15 = [
        {"timestamp": "2026-10-20 11:15:00", "open": 12.00, "high": 12.15, "low": 11.98, "close": 12.10, "volume": 100},
        {"timestamp": last_15m, "open": 12.10, "high": 12.18, "low": 12.08, "close": 12.13, "volume": 100},
    ]
    bars30 = [
        {"timestamp": "2026-10-20 11:00:00", "open": 11.95, "high": 12.12, "low": 11.92, "close": 12.08, "volume": 200},
        {"timestamp": last_30m, "open": 12.08, "high": 12.18, "low": 12.06, "close": 12.13, "volume": 200},
    ]
    return build_timeframe_evidence(
        bars15, bars30, price=12.13, support=12.03, resistance=13.0,
        now=now, quote_date="2026-10-20",
    )

def test_stale_bars_are_not_complete():
    evidence = build_timeframe_evidence(
        bars15_from_last_week, bars30_from_last_week,
        price=12.1, support=12.0, resistance=13.0,
        now=now, quote_date="2026-10-20",
    )
    self.assertFalse(evidence.complete)

def test_current_forming_bar_is_excluded_under_sina_end_timestamp():
    bars = parse_intraday_rows(rows, scale=15, now=now, timestamp_semantics="end")
    self.assertEqual(bars[-1]["timestamp"], "2026-10-20 10:15:00")

def test_lunch_gap_keeps_1130_bar_current_until_next_slot_completes():
    evidence_at_1305 = evidence_with_latest_bars(
        datetime(2026, 10, 20, 13, 5), "2026-10-20 11:30:00", "2026-10-20 11:30:00"
    )
    evidence_at_1316 = evidence_with_latest_bars(
        datetime(2026, 10, 20, 13, 16), "2026-10-20 11:30:00", "2026-10-20 11:30:00"
    )
    self.assertTrue(evidence_at_1305.complete)
    self.assertFalse(evidence_at_1316.complete)
```

- [x] **Step 3: 运行测试确认旧K线仍被误判完整、未完成K线被误纳入**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_intraday_bar_freshness -v`

- [x] **Step 4: 显式处理Sina时间语义和交易时段槽位**

`timestamp_semantics="start"`时把`timestamp + scale分钟`规范化为结束时间；`timestamp_semantics="end"`直接保留。行情日期缺失、未来K线、重复K线、15/30分钟任一周期落后于`latest_completed_slot`时均返回不完整证据，策略只能等待，不能把“今天早些时候的K线”当成新鲜证据。

- [x] **Step 5: 运行盘中K线和策略测试**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_intraday_bars tests.test_intraday_bar_freshness tests.test_intraday_trade_policy -v`

### Task 4: 收紧买点与普通卖点证据

**Files:**
- Modify: `intraday_bars.py`
- Modify: `intraday_trade_policy.py`
- Test: `tests/test_intraday_evidence_quality.py`

**Interfaces:**
- Produces: `TimeframeEvidence`新增`m15_distance_pct`、`m30_distance_pct`、`last_bar_rejected`和`clearance_confirmed`。
- Consumes: `evaluate_trade_action(intent, evidence, eligibility, invalidation_price)`接口保持不变。

当前缺口：两根15分钟收盘价“刚好高于支撑”加30分钟收盘不下降，仍可能只是一次脆弱反抽，与“买点求稳”的目标不完全一致。

确定阈值：买入站稳要求两个已完成15分钟收盘价均比目标位高至少`0.3%`，30分钟最新收盘不低于前一根且不低于目标位。若最新15分钟上影线占全K线振幅至少`50%`，且收盘低于K线中点，则标记`last_bar_rejected=True`。风险卖出继续要求一个新鲜15分钟K线，收盘和现价均低于支撑`0.5%`，不受30分钟过滤。

- [x] **Step 1: 编写失败测试**

```python
def test_barely_above_support_is_not_actionable_buy():
    decision = evaluate_trade_action("buy", barely_reclaimed_evidence, eligibility, 12.0)
    self.assertEqual(decision.action, DecisionAction.WAIT)

def test_upper_wick_rejection_is_not_actionable_buy():
    decision = evaluate_trade_action("buy", rejected_wick_evidence, eligibility, 12.0)
    self.assertEqual(decision.action, DecisionAction.WAIT)

def test_actionable_buy_keeps_03_percent_clearance():
    decision = evaluate_trade_action("buy", confirmed_evidence, eligibility, 12.0)
    self.assertEqual(decision.action, DecisionAction.BUY_SMALL)

def test_risk_exit_still_uses_single_15m_bar():
    decision = evaluate_trade_action("risk_exit", risk_evidence, eligibility, 12.0)
    self.assertEqual(decision.action, DecisionAction.RISK_EXIT)
```

- [x] **Step 2: 运行测试确认脆弱站回仍可买、风险卖出被过度要求**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_intraday_evidence_quality -v`

- [x] **Step 3: 实现上述确定阈值；风险卖出继续单根15分钟即时确认**

- [x] **Step 4: 运行策略、K线和集成测试**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_intraday_evidence_quality tests.test_intraday_trade_policy tests.test_intraday_bars tests.test_intraday_trade_integration -v`

### Task 5: 所有盘中入口共用动作守卫

**Files:**
- Create: `trade_action_guard.py`
- Modify: `intraday_tactics.py`
- Modify: `portfolio_analysis.py`
- Modify: `dip_rebound.py`
- Modify: `portfolio_daily_report.py`
- Modify: `stock_menubar.py`
- Audit: `ai_analysis.py`、`deep_loss_policy.py`、`market_alert.py`、`observation_ui.py`、`stock_analysis.py`
- Test: `tests/test_all_intraday_action_surfaces.py`

**Interfaces:**
- Produces: `guard_actionable_copy(kind, text, eligibility, evidence, trade_date, invalidation_price=0.0) -> GuardedCopy`。
- Produces: `GuardedCopy(level, text, max_quantity=0, invalidation_price=0.0, reason="")`，其中`level`为`actionable`、`risk_fact`、`observation`或`next_day`。

当前缺口：`dip_rebound.py`、`portfolio_analysis.py`、日报告和AI提示仍可能绕过新策略，直接写“补仓/做T/减仓/接回”。仅修`_check_intraday_signals`不能满足“所有提醒都不草率行动”。

守卫只接受结构化`kind`和证据。`actionable`必须同时满足：行情日期有效、K线证据完整且新鲜、T+1资格允许、数量大于0；买入还必须有失效价。守卫不得仅靠删除关键词冒充安全，原动作必须改写为“观察/停止执行/次一交易日复核”并给出原因。

- [x] **Step 1: 编写失败测试，枚举每类输出入口**

```python
def test_dip_rebound_cannot_issue_direct_buy_or_t_action():
    guarded = guard_actionable_copy("buy", "可补仓做T", eligibility_unconfirmed, None)
    self.assertEqual(guarded.level, "observation")
    self.assertNotIn("可补仓", guarded.text)

def test_actionable_copy_requires_guarded_evidence():
    guarded = guard_actionable_copy("t_sell", "做T减仓", eligibility, None)
    self.assertEqual(guarded.level, "observation")

def test_valid_dual_timeframe_buy_passes_guard():
    guarded = guard_actionable_copy("buy", "小仓买入", eligibility, evidence, "2026-10-20", 12.0)
    self.assertEqual(guarded.level, "actionable")
    self.assertGreater(guarded.max_quantity, 0)
```

- [x] **Step 2: 运行测试确认多个入口仍产生直接动作**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_all_intraday_action_surfaces -v`

- [x] **Step 3: 先实现纯函数动作守卫，再逐个接入盘中信号、下探回升和持仓分析**

生产代码从`intraday_trade_policy.DecisionAction`和`PositionEligibility`构造动作，禁止先写死“买入/卖出/做T”再靠正则修补。日报告、AI回答和观察UI只能引用守卫后的`GuardedCopy`。审计文件若只包含回测、教育性说明或历史交易字段而不输出当日动作，则记录后不修改。

- [x] **Step 4: 搜索源代码，确保盘中输出不再绕过守卫直接给确定性动作**

Run: `rg -n '可考虑.*买|小仓补|做T|减仓|清仓|接回' intraday_tactics.py portfolio_analysis.py dip_rebound.py portfolio_daily_report.py stock_menubar.py ai_analysis.py deep_loss_policy.py market_alert.py observation_ui.py stock_analysis.py`

命中项逐条标记为`结构化动作输出`、`旧文案待清洗`、`历史/回测非盘中输出`或`文档字符串`。只有前两类进入代码修改范围。

- [x] **Step 5: 运行入口覆盖与回归测试**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_all_intraday_action_surfaces tests.test_intraday_trade_integration tests.test_intraday_notification tests.test_portfolio_analysis tests.test_dip_rebound -v`

### Task 6: 统一持仓修改入口与交易账本

**Files:**
- Modify: `trade_ledger.py`
- Modify: `stock_menubar.py`
- Test: `tests/test_portfolio_mutation_sync.py`

**Interfaces:**
- Produces: `LedgerAdjustment`和`TradeLedger.record_adjustment(code, name, effective_date, quantity_delta, cost, reason) -> LedgerAdjustment`。调整记录独立于真实`Trade`，不伪造成交方向。
- Produces: `StockMenuBar._apply_portfolio_quantity_change(code, new_quantity, cost, effective_date, reason)`。

当前缺口：“修改数量/成本”“批量修改”“删除监控股票”等入口直接改`portfolio.json`，不会生成交易批次，可能把今日新仓重新变成“历史可卖”。

`quantity_delta > 0`形成带`effective_date`的新批次，当日批次继续锁定；`quantity_delta < 0`只能消耗该生效日前的可卖批次，数量不足立即抛出`ValueError`。`cost`只作为人工参考成本保存，不改写历史成交价、不生成批次。新增“仅修改参考成本”路径，避免为成本修正制造虚假交易。删除持仓等价于在生效日卖出全部数量，因此同样受当日可卖限制；若只是停止监控而不卖出，必须使用单独的“移出监控但保留账本”入口。

- [x] **Step 1: 编写失败测试**

```python
def test_manual_quantity_increase_today_is_t_plus_1():
    app._apply_portfolio_quantity_change("sz000737", 600, 12.2, "2026-10-20", "manual_correction")
    summary = ledger.position_summary("sz000737", "2026-10-20")
    self.assertEqual(summary["today_locked_quantity"], 100)

def test_manual_sell_cannot_exceed_sellable():
    with self.assertRaisesRegex(ValueError, "当日可卖"):
        app._apply_portfolio_quantity_change("sz000737", 0, 12.2, "2026-10-20", "manual_sell")

def test_cost_only_correction_does_not_create_trade_or_lock_quantity():
    before_trade_count = len(ledger.trades)
    adjustment = ledger.record_adjustment("sz000737", "北方铜业", "2026-10-20", 0, 12.2, "cost_correction")
    self.assertEqual(adjustment.quantity_delta, 0)
    self.assertEqual(len(ledger.trades), before_trade_count)
```

- [x] **Step 2: 运行测试确认数量修改绕过T+1**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_portfolio_mutation_sync -v`

- [x] **Step 3: 所有数量变化记录为带生效日期的调整项；修改弹窗增加生效日期和“仅修成本”选项，删除/清仓受当日可卖约束**

- [x] **Step 4: 运行账本、菜单和T+1测试**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_trade_ledger tests.test_trade_ledger_menu tests.test_portfolio_mutation_sync tests.test_trade_date_authority -v`

### Task 7: 回归、缺口清单与文档

- [x] **Step 1: 运行本功能全部测试并记录数量**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest tests.test_intraday_deferred_confirmation tests.test_trade_date_authority tests.test_intraday_bar_freshness tests.test_intraday_evidence_quality tests.test_all_intraday_action_surfaces tests.test_portfolio_mutation_sync tests.test_trade_ledger tests.test_trade_ledger_menu tests.test_intraday_bars tests.test_intraday_trade_policy tests.test_intraday_trade_integration tests.test_intraday_notification test_intraday_tactics -v`

- [x] **Step 2: 运行非异常全量回归**

Run: `PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v`

已知非本功能问题：`tests.test_holding_sectors.FirstRoundLabelUpdateTests.test_quote_cache_is_filled_for_later_menu_rebuilds`会令测试进程退出134。若仍复现，单独记录，不在本计划内修复。

本功能集成夹具已修复，`tests/test_intraday_trade_integration.py`不再有行情日期缺失导致的失败。最新复现显示`FirstRoundLabelUpdateTests`的两个测试均会令进程退出134；同模块其余28项通过。该异常位于板块龙头菜单测试，与本计划交易策略改动无关。

- [x] **Step 3: 更新`A股监控使用说明.md`和策略文档，写明行情日期、K线新鲜度、账本唯一来源和所有入口统一动作守卫**

- [x] **Step 4: 生成最终验收清单，逐项对应本计划Global Constraints并报告任何未满足项**

## 最终验证记录

- 本功能测试：`120`项通过。
- 非异常全量回归：`759`项通过、`4`项跳过；补跑`test_holding_sectors`中不触发异常的`28`项也通过，共`787`项有效通过。
- 已知外部异常：全量`unittest discover`仍以退出码`134`终止；单独运行`FirstRoundLabelUpdateTests`的两个测试均复现，按计划不修复。
- 语法与导入：改动的Python文件均通过`compile(..., "exec")`，`trade_action_guard`、`portfolio_analysis`、`portfolio_daily_report`、`stock_menubar`导入冒烟通过。
- 动作文案审计：见`docs/reviews/2026-10-20-intraday-action-copy-audit.md`。

## Global Constraints 验收清单

- [x] T+1日期只取行情`date`；缺失时交易动作安全失败。
- [x] 形成中、过期或错误槽位K线不产生确定性动作。
- [x] 买入要求两个新鲜15分钟确认且30分钟不反向，并满足`0.3%`站稳距离与上影拒绝过滤。
- [x] 风险卖出只等待一个新鲜15分钟确认，`0.5%`破位阈值，不等待30分钟。
- [x] 普通卖出和做T受双周期、当日可卖批次和T+1数量上限约束。
- [x] 所有用户可见盘中动作入口均经统一动作守卫；未通过时仅观察、风险事实或次日处置。
- [x] 人工账本调整、成本修正和“移出监控（保留账本）”不绕过T+1。
- [ ] 非本功能的`FirstRoundLabelUpdateTests`仍存在退出码134；已隔离记录，不影响本功能`787`项有效回归。

