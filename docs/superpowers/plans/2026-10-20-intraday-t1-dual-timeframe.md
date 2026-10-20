# 盘中 T+1 与双周期确定性提醒 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让盘中买卖、减仓和做T提醒遵守A股T+1，并通过15分钟执行、30分钟过滤给出确定性动作。

**Architecture:** 新增独立交易批次账本、已完成盘中K线证据和纯函数策略，再接入现有`IntradayTactics`通知管线。旧持仓迁移为历史可卖批次，新增交易通过菜单记录。

**Tech Stack:** Python 3、标准库、现有Sina HTTP接口、`unittest`。

**Spec:** `docs/superpowers/specs/2026-10-20-intraday-t1-dual-timeframe-design.md`

## Global Constraints

- 当日买入不可当日卖出。
- 买入要求两个已完成15分钟K线确认且30分钟不反向。
- 风险卖出只要一个已完成15分钟K线确认，不受30分钟过滤限制。
- 普通止盈和做T要求15/30分钟共同确认，并限制在当日可卖数量内。
- 数据不完整或K线未完成时只允许等待观察。
- 此目录不是Git仓库；不创建提交，完成时以测试结果作为交付证据。

---

### Task 1: A股交易批次与T+1账本

**Files:**
- Create: `trade_ledger.py`
- Test: `tests/test_trade_ledger.py`

**Interfaces:**
- Consumes: 现有持仓`{"code", "name", "quantity", "cost"}`。
- Produces: `TradeLedger.record_trade(...) -> Trade`, `TradeLedger.sellable_quantity(code, as_of_date) -> int`, `TradeLedger.position_summary(code, as_of_date) -> dict`, `TradeLedger.sync_legacy_positions(positions) -> None`。

- [ ] **Step 1: 编写失败测试**

```python
def test_same_day_buy_is_not_sellable():
    ledger = TradeLedger()
    ledger.record_trade("buy", "sz000737", "北方铜业", "2026-10-20", 100, 12.13)
    self.assertEqual(ledger.sellable_quantity("sz000737", "2026-10-20"), 0)
    self.assertEqual(ledger.sellable_quantity("sz000737", "2026-10-21"), 100)

def test_sell_cannot_exceed_same_day_sellable():
    ledger = TradeLedger()
    ledger.record_trade("buy", "sz000737", "北方铜业", "2026-10-19", 500, 12.5)
    ledger.record_trade("buy", "sz000737", "北方铜业", "2026-10-20", 100, 12.13)
    ledger.record_trade("sell", "sz000737", "北方铜业", "2026-10-20", 500, 12.2)
    with self.assertRaisesRegex(ValueError, "当日可卖"):
        ledger.record_trade("sell", "sz000737", "北方铜业", "2026-10-20", 100, 12.2)
```

- [ ] **Step 2: 运行测试确认因模块不存在而失败**

Run: `python3 -m unittest tests.test_trade_ledger -v`

- [ ] **Step 3: 实现不可变交易记录、FIFO旧批次扣减、JSON持久化和旧仓迁移**

- [ ] **Step 4: 运行新测试和相邻账本测试**

Run: `python3 -m unittest tests.test_trade_ledger -v`

### Task 2: 15/30分钟已完成K线证据

**Files:**
- Create: `intraday_bars.py`
- Test: `tests/test_intraday_bars.py`

**Interfaces:**
- Produces: `fetch_intraday_bars(code, scale, datalen, now, fetcher=requests.get) -> list[dict]`, `build_timeframe_evidence(bars15, bars30, price, support, resistance, now) -> TimeframeEvidence`。

- [ ] **Step 1: 编写失败测试**

```python
def test_current_incomplete_bar_is_excluded():
    now = datetime(2026, 10, 20, 10, 7)
    bars = parse_intraday_rows([
        {"day": "2026-10-20 09:45:00", "open": "12", "high": "12.2", "low": "11.9", "close": "12.1", "volume": "100"},
        {"day": "2026-10-20 10:00:00", "open": "12.1", "high": "12.3", "low": "12", "close": "12.2", "volume": "100"},
    ], scale=15, now=now)
    self.assertEqual(len(bars), 1)

def test_buy_needs_two_completed_15m_confirmations():
    evidence = build_timeframe_evidence(bars15, bars30, price=12.1, support=12.0,
                                         resistance=13.0, now=now)
    self.assertEqual(evidence.m15_confirmations, 2)
    self.assertTrue(evidence.m30_aligned)
    self.assertTrue(evidence.complete)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python3 -m unittest tests.test_intraday_bars -v`

- [ ] **Step 3: 实现Sina参数构造、响应校验、未完成K线排除和纯函数证据计算**

- [ ] **Step 4: 运行测试确认通过**

Run: `python3 -m unittest tests.test_intraday_bars -v`

### Task 3: 确定性买卖与T+1策略

**Files:**
- Create: `intraday_trade_policy.py`
- Test: `tests/test_intraday_trade_policy.py`

**Interfaces:**
- Consumes: `TimeframeEvidence`和`PositionEligibility`。
- Produces: `evaluate_trade_action(intent, evidence, eligibility, invalidation_price) -> TradeDecision`。

- [ ] **Step 1: 编写失败测试**

```python
def test_buy_requires_two_15m_and_30m_alignment():
    decision = evaluate_trade_action("buy", evidence, eligibility, 12.0)
    self.assertEqual(decision.action, DecisionAction.BUY_SMALL)

def test_risk_exit_ignores_30m_filter_but_respects_t1():
    decision = evaluate_trade_action("risk_exit", evidence, eligibility, 12.0)
    self.assertEqual(decision.action, DecisionAction.NEXT_DAY_REVIEW)
    self.assertIn("T+1", decision.message)

def test_t_sell_is_capped_by_sellable_quantity():
    decision = evaluate_trade_action("t_sell", evidence, eligibility, 12.0)
    self.assertLessEqual(decision.max_sell_quantity, eligibility.sellable_quantity)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python3 -m unittest tests.test_intraday_trade_policy -v`

- [ ] **Step 3: 实现买入、风险卖出、普通卖出和做T的非对称阈值与确定性文案**

- [ ] **Step 4: 运行测试确认通过**

Run: `python3 -m unittest tests.test_intraday_trade_policy -v`

### Task 4: 接入盘中信号与安全文案

**Files:**
- Modify: `stock_menubar.py`
- Modify: `intraday_tactics.py`
- Test: `tests/test_intraday_trade_integration.py`

**Interfaces:**
- Consumes: `TradeLedger.position_summary`、`fetch_intraday_bars`、`build_timeframe_evidence`、`evaluate_trade_action`。
- Produces: `_apply_intraday_trade_policy(signal, code, name, price, support, resistance, now) -> IntradaySignal | None`。

- [ ] **Step 1: 编写失败测试**

```python
def test_unconfirmed_buy_becomes_observation_only():
    result = app._apply_intraday_trade_policy(buy_signal, "sz000737", "北方铜业", 12.1, 12.0, 13.0, now)
    self.assertEqual(result.signal_type, IntradaySignalType.TRADE_WAIT)
    self.assertIn("仅观察", result.notification_body)

def test_today_bought_only_risk_signal_cannot_sell_today():
    result = app._apply_intraday_trade_policy(risk_signal, "sz000737", "北方铜业", 11.8, 12.0, 13.0, now)
    self.assertIn("下一交易日", result.notification_body)
    self.assertNotIn("今日卖出", result.notification_body)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python3 -m unittest tests.test_intraday_trade_integration -v`

- [ ] **Step 3: 在信号发出前调用策略，未通过时降级或抑制，并移除可能暗示当日卖出新仓的文案**

- [ ] **Step 4: 运行盘中策略测试**

Run: `python3 -m unittest tests.test_intraday_trade_integration tests.test_intraday_notification test_intraday_tactics -v`

### Task 5: 交易记录菜单与持仓同步

**Files:**
- Modify: `stock_menubar.py`
- Test: `tests/test_trade_ledger_menu.py`

**Interfaces:**
- Produces: `show_trade_ledger(_)`，输入格式`买入/卖出,代码或名称,YYYY-MM-DD,数量,价格`。

- [ ] **Step 1: 编写失败测试**

```python
def test_trade_text_records_buy_and_updates_sellable():
    trades = parse_trade_text("买入,北方铜业,2026-10-20,100,12.13", resolver)
    self.assertEqual(trades[0].side, "buy")
    self.assertEqual(trades[0].quantity, 100)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python3 -m unittest tests.test_trade_ledger_menu -v`

- [ ] **Step 3: 增加菜单入口、文本解析、账本保存和持仓数量/加权成本同步**

- [ ] **Step 4: 运行账本与菜单测试**

Run: `python3 -m unittest tests.test_trade_ledger tests.test_trade_ledger_menu -v`

### Task 6: 全量回归与文档

- [ ] **Step 1: 运行全部测试**

Run: `python3 -m unittest discover -v`

- [ ] **Step 2: 修复与本功能相关的失败，不修改无关测试**

- [ ] **Step 3: 更新`A股监控使用说明.md`，写明交易记录格式、T+1和15/30分钟提醒边界**

- [ ] **Step 4: 再次运行全部测试并记录结果**

