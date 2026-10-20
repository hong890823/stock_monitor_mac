# Strong Market T-Filter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Suppress ordinary intraday T-sell prompts when broad-market turnover, index, sector, and stock price action are jointly strong, while preserving risk alerts and recording deferred T opportunities in the daily portfolio report.

**Architecture:** Keep the environment decision pure and optional in `intraday_tactics.py`, so existing callers and tests retain their current behavior when context is absent. The menu-bar coordinator supplies the live turnover/index/sector context, records confirmed deferrals, and passes a compact event summary into the existing portfolio report pipeline.

**Tech Stack:** Python, `unittest`, existing menu-bar and daily-report modules.

## Global Constraints

- Missing market or sector context must not suppress an existing T-sell signal.
- Risk signals such as support breaks remain higher priority than the environment filter.
- Use the existing `TurnoverTracker` prediction in 亿元; do not add a second turnover estimator.
- Keep thresholds centralized and easy to tune.

### Task 1: Environment-aware intraday signal

**Files:**
- Modify: `intraday_tactics.py`
- Test: `test_intraday_tactics.py`

- [x] Add failing tests for strong-environment deferral, missing-context fallback, and risk priority.
- [x] Add an explicit `T_HOLD` signal type and centralized environment thresholds.
- [x] Pass optional market context through `evaluate` and `check_signal`.
- [x] Return a confirmed “强势延续观察” signal instead of `T_SELL` only when all environment inputs are strong.

### Task 2: Menu-bar context and event capture

**Files:**
- Modify: `stock_menubar.py`
- Test: `tests/test_t_sell_filter.py`

- [x] Add pure helpers/tests for deriving sector strength and event summaries.
- [x] Pass current predicted turnover, index change, and mapped sector change into the intraday engine.
- [x] Capture confirmed deferrals once per stock/day with trigger price and forecast turnover.

### Task 3: Daily report rendering

**Files:**
- Modify: `portfolio_daily_report.py`, `stock_menubar.py`
- Test: `tests/test_portfolio_daily_report.py`

- [x] Add the event summary to the existing market evidence section.
- [x] Include current-price outcome for deferred T events in the final report.
- [x] Keep the report silent when no deferral events exist.

### Task 4: Documentation and verification

**Files:**
- Modify: `A股监控使用说明.md`, `CHANGELOG.md`

- [x] Document thresholds, exceptions, and the report meaning.
- [x] Run focused tests, then the full available test suite.
