# 盘中资金流辅助确认 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 接入可验证的盘中板块/个股资金流数据，并作为现有盘中提醒的辅助确认层。

**Architecture:** 新增纯数据模块负责标准化、缓存和新鲜度判断；`stock_menubar.py` 只在后台刷新数据并把快照传给既有提醒策略。资金流缺失时策略保持当前行为，资金流连续确认时只提升提醒等级或增加证据文案。

**Tech Stack:** Python 3.9、`requests`、AKShare、`unittest`、现有 `IntradayTactics` 与 `SignalConfidence`。

**Spec:** `docs/superpowers/specs/2026-10-12-intraday-capital-flow-design.md`

## Global Constraints

- 不将平台估算主力资金称为真实机构成交。
- 不在每 15 秒行情刷新中直接请求资金流接口。
- 数据日期、更新时间、来源、范围、交易时段和过期状态必须可追溯。
- 资金流缺失时必须降级为现有策略，不影响原有提醒。

---

### Task 1: 资金流数据模块

**Files:**
- Create: `capital_flow.py`
- Test: `tests/test_capital_flow.py`

**Interfaces:**
- Produces `CapitalFlowSnapshot`, `normalize_flow_frame`, `flow_confirmation`, `FlowCache`。
- Provides provider functions that can be mocked in tests and return normalized snapshots.

- [x] Write failing tests for unit normalization, date validation, cache freshness, consecutive confirmation, and stale fallback.
- [x] Run `python3 -m unittest tests.test_capital_flow` and confirm expected failures.
- [x] Implement the normalized snapshot and cache with no UI dependencies.
- [x] Add AKShare-backed provider wrappers with bounded timeouts and source labels.
- [x] Run the focused tests until green.

### Task 2: 后台刷新接入

**Files:**
- Modify: `stock_menubar.py`
- Test: `tests/test_capital_flow_integration.py`

**Interfaces:**
- Consumes `CapitalFlowSnapshot` and `FlowCache` from Task 1.
- Produces `self._capital_flow_cache` and a non-blocking refresh path for held stocks and related sectors.

- [x] Write failing tests for refresh interval, missing data, and no blocking of existing quote refresh.
- [x] Add cache initialization and background refresh scheduling.
- [x] Keep the existing 15-second quote loop independent from the flow request.
- [x] Run focused integration tests.

### Task 3: 盘中提醒确认层

**Files:**
- Modify: `stock_menubar.py`, `intraday_tactics.py`
- Test: `tests/test_intraday_capital_flow.py`

**Interfaces:**
- Consumes price/volume conditions plus normalized stock and sector flow snapshots.
- Produces confirmation metadata and severity adjustments without creating a standalone buy/sell signal.

- [x] Write failing tests for support-break confirmation, breakout confirmation, divergence-only observation, and missing-flow fallback.
- [x] Implement consecutive-sample confirmation and conservative severity adjustment.
- [x] Keep existing signal cooldown and confidence logic intact.
- [x] Run all intraday and capital-flow tests.

### Task 4: AI/报告证据

**Files:**
- Modify: `ai_analysis.py`, `market_timing.py`, `portfolio_daily_report.py`
- Test: `tests/test_ai_analysis.py`, `tests/test_market_timing.py`, `tests/test_portfolio_daily_report.py`

**Interfaces:**
- Consumes normalized snapshots only when current and non-stale.
- Produces concise, explicitly labeled flow evidence for AI and holdings reports.

- [x] Add failing tests for source/date/scope labels and missing-flow wording.
- [x] Add flow evidence to the existing context without changing core conclusion rules.
- [x] Run the full test suite and compile checks.
