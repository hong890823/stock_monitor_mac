# 盘中资金流优化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将概念板块纳入盘中资金流刷新，并把净占比、大单证据和资金背离作为不改变原有买卖信号的辅助信息。

**Architecture:** 继续使用 `capital_flow.py` 的标准化快照和 `stock_menubar.py` 的后台刷新线程。板块目标按映射名称分别请求行业与概念数据；提醒层只追加证据或观察标签，资金流缺失、过期或未连续确认时保持原策略。

**Tech Stack:** Python 3.9、requests、AKShare、unittest。

**Spec:** `docs/superpowers/specs/2026-10-12-intraday-capital-flow-design.md`

## Global Constraints

- 平台估算主力资金不称为真实机构成交。
- 不在每 15 秒行情刷新中同步请求资金流。
- 至少连续两个有效采样方向一致才升级资金确认。
- 资金流缺失或过期时降级为现有价格、支撑压力和量比策略。
- 不新增独立买入或卖出信号类型。

---

### Task 1: 目标分类和概念板块刷新

**Files:**
- Modify: `stock_menubar.py`
- Test: `tests/test_capital_flow_integration.py`

**Interfaces:**
- Consumes: `_capital_flow_targets()` 返回的持仓代码和映射板块名。
- Produces: 行业、概念分组后的后台刷新请求，并继续只缓存持仓相关板块。

- [x] **Step 1: Write the failing test**
  - 给测试 app 增加一个概念板块名称，断言刷新线程分别调用 `fetch_sector_flow("industry")` 和 `fetch_sector_flow("concept")`。
  - 断言行业和概念返回中只有目标名称进入缓存。
- [x] **Step 2: Run the focused test and verify it fails**
  - Run: `python3 -m unittest tests.test_capital_flow_integration.CapitalFlowIntegrationTests.test_refreshes_industry_and_concept_targets`
  - Expected: 当前只调用行业 provider，测试失败。
- [x] **Step 3: Implement minimal classification**
  - 增加可配置的概念名称集合或分类 helper。
  - `_refresh_capital_flow` 按类型请求 provider，并合并过滤后的快照。
  - provider 失败时保留已有降级行为，不阻塞行情线程。
- [x] **Step 4: Run focused tests**
  - Run: `python3 -m unittest tests.test_capital_flow_integration`
  - Expected: PASS。

### Task 2: 资金证据文案

**Files:**
- Modify: `stock_menubar.py`
- Test: `tests/test_capital_flow_integration.py`

**Interfaces:**
- Consumes: 当前有效的个股和板块 `CapitalFlowSnapshot`。
- Produces: 带净额、净占比、可选大单/超大单信息和数据时间的辅助文案。

- [x] **Step 1: Write the failing test**
  - 构造含 `net_ratio_pct`、`large_net_yi`、`super_large_net_yi` 的个股与板块快照。
  - 断言突破确认文案包含净额、净占比和“平台口径”标签。
  - 断言缺失比例或大单字段时仍生成基础净额文案。
- [x] **Step 2: Run the focused test and verify it fails**
  - Run: `python3 -m unittest tests.test_capital_flow_integration.CapitalFlowIntegrationTests.test_flow_confirmation_includes_ratio_and_optional_large_orders`
  - Expected: 当前文案只有净流入/净流出方向，测试失败。
- [x] **Step 3: Implement minimal evidence formatting**
  - 增加只负责格式化快照证据的 helper。
  - 不把大单或超大单字段用于独立触发条件。
- [x] **Step 4: Run focused tests**
  - Run: `python3 -m unittest tests.test_capital_flow_integration`
  - Expected: PASS。

### Task 3: 资金背离观察标签

**Files:**
- Modify: `stock_menubar.py`
- Test: `tests/test_capital_flow_integration.py`

**Interfaces:**
- Consumes: 已产生的既有盘中信号、有效资金流快照及连续方向确认状态。
- Produces: 仅供观察的背离说明，不改变信号类型和默认通知等级。

- [x] **Step 1: Write the failing test**
  - 断言放量突破但个股和板块连续净流出时生成“上涨缺乏资金确认”观察文案，且不返回 `danger`。
  - 断言资金流方向与价格信号一致时不增加背离标签。
  - 断言资金流缺失或未连续确认时不生成背离标签。
- [x] **Step 2: Run the focused test and verify it fails**
  - Run: `python3 -m unittest tests.test_capital_flow_integration.CapitalFlowIntegrationTests.test_breakout_divergence_is_observation_only`
  - Expected: 当前没有背离文案，测试失败。
- [x] **Step 3: Implement minimal observation logic**
  - 扩展 `_capital_flow_signal_context` 的文案分支。
  - 只对已有“放量突破”或“跌破支撑”信号追加观察说明；不创建新信号。
- [x] **Step 4: Run focused and regression tests**
  - Run: `python3 -m unittest tests.test_capital_flow_integration tests.test_capital_flow tests.test_intraday_tactics`
  - Expected: PASS。

### Task 4: 全量验证

**Files:**
- No additional production files.

- [x] **Step 1: Run compile checks**
  - Run: `python3 -m py_compile capital_flow.py stock_menubar.py`
- [ ] **Step 2: Run the relevant suite**
  - Run: `python3 -m unittest discover -s tests -p 'test_*.py'`
  - Result: 424 tests run, 1 existing failure in `tests/test_menu_format.py:98`; all capital-flow and intraday tests pass。
- [x] **Step 3: Inspect the diff**
  - Confirm only the planned files changed and no raw or stale flow data can upgrade an alert.
