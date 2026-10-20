# Evidence Review and T-Filter Tracking Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 将 TradingAgents-CN 中“证据分层、正反证据、失效条件、事后追踪”的轻量思想贴合到桌面端和 Android 端，增强冲高做 T 过滤的可解释性与可复盘性，不改变确定性买卖规则。

**Architecture:** 保留现有 `MarketEnvironmentQuality` 作为唯一市场环境判定源，新增轻量证据卡/决策复核纯逻辑层。报告只展示真实可用的支持证据、反向证据、缺失信息和失效条件；盘中信号仍由现有确定性规则产生。被暂缓做 T 的事件继续落盘，并记录观察窗口内的事实结果，不把结果直接包装成策略有效性结论。

**Tech Stack:** Python 3、`unittest`；Android Kotlin、JUnit、现有 Gradle 构建。

**Spec:** 本轮对 TradingAgents-CN 的借鉴结论：采用证据契约、正反观点、失效条件和事后结果追踪；不引入多智能体、AI 实时决策、FastAPI/MongoDB/Redis 或第二套成交额预测算法。

## Global Constraints

- 盘中提醒继续使用确定性规则，AI 不参与实时买卖信号。
- 跌破支撑等风险信号优先级不变。
- 缺失或过期数据不得被猜测成利好、利空或“放量分歧”。
- 成交额连续性沿用当前已确认口径：今日预估成交额达到 2 万亿且此前完整收盘日量能、宽度、指数位置共同确认；不恢复 1.5 万亿低阈值。
- 报告文案按实际证据动态拼接，不使用固定看多/看空结论。
- 不提交 commit；当前桌面目录没有可用 Git 元数据。

### Task 1: Add shared evidence cards and decision review on desktop

**Files:**
- Create: `market_evidence.py`
- Create: `tests/test_market_evidence.py`
- Modify: `portfolio_daily_report.py`
- Test: `tests/test_portfolio_daily_report.py`

**Interfaces:**
- `EvidenceCard(label, value, source=None, quote_date=None, as_of=None, scope=None, fresh=True, available=True)` stores one observable item.
- `format_evidence_cards(cards)` returns concise dynamic lines and marks unavailable/stale data without inventing direction.
- `build_decision_review(market, overview)` returns `supporting`, `counter`, `missing`, and `invalidators` lists.

- [ ] Write tests for complete, unavailable, and stale evidence cards.
- [ ] Run `python -m unittest tests.test_market_evidence -v` and verify the new tests fail before implementation.
- [ ] Implement the dataclass and formatting helpers with no network or UI dependencies.
- [ ] Add decision-review tests for strong breadth, index hesitation, missing data, and invalidation conditions.
- [ ] Implement dynamic decision review and expose it from `build_overview`.
- [ ] Run the focused desktop tests and verify they pass.

### Task 2: Integrate desktop report evidence without changing signal priority

**Files:**
- Modify: `portfolio_daily_report.py`
- Modify: `stock_menubar.py`
- Modify: `tests/test_portfolio_daily_report.py`

**Interfaces:**
- `build_overview` adds `decision_review` and `evidence_cards` fields.
- `format_daily_report` renders only non-empty review sections before technical actions.
- Existing `t_filter_events` remain backward-compatible with old persisted dictionaries.

- [ ] Add report tests proving support, counter, missing, and invalidation sections are dynamic and absent when empty.
- [ ] Run the focused tests and verify the tests fail before integration.
- [ ] Pass market environment fields, timing metadata, and capital-flow text into the evidence layer.
- [ ] Render the review sections while preserving existing action ordering and risk language.
- [ ] Run all desktop tests.

### Task 3: Track factual outcomes for deferred T events on desktop

**Files:**
- Modify: `portfolio_daily_report.py`
- Modify: `stock_menubar.py`
- Modify: `tests/test_portfolio_daily_report.py`

**Interfaces:**
- `update_t_filter_event_outcome(event, current_price, observed_at)` updates current price, max favorable/adverse percentage, and observation status.
- `summarize_t_filter_outcomes(events)` reports observed/insufficient counts and factual return ranges only.

- [ ] Add unit tests for favorable, adverse, unchanged, and insufficient-price observations.
- [ ] Run the focused tests and verify the tests fail before implementation.
- [ ] Update existing events on each intraday refresh and persist the extended fields compatibly.
- [ ] Include a compact factual outcome line in the report without declaring strategy success.
- [ ] Run all desktop tests.

### Task 4: Mirror the evidence and outcome layer on Android

**Files:**
- Create: `/Users/stephen/Documents/mi/privacy/stock-monitor-android/app/src/main/java/com/example/stockmonitor/analysis/EvidenceCard.kt`
- Modify: `/Users/stephen/Documents/mi/privacy/stock-monitor-android/app/src/main/java/com/example/stockmonitor/analysis/PortfolioAnalysis.kt`
- Modify: `/Users/stephen/Documents/mi/privacy/stock-monitor-android/app/src/main/java/com/example/stockmonitor/analysis/StrongContinuationEvent.kt`
- Modify: `/Users/stephen/Documents/mi/privacy/stock-monitor-android/app/src/test/java/com/example/stockmonitor/PortfolioAnalysisTest.kt`
- Create: `/Users/stephen/Documents/mi/privacy/stock-monitor-android/app/src/test/java/com/example/stockmonitor/EvidenceCardTest.kt`

**Interfaces:**
- Kotlin `EvidenceCard` mirrors desktop availability/freshness semantics.
- `PortfolioAnalysis` exposes the same four review buckets and keeps existing `StrongContinuationEvent` JSON backward-compatible.
- `StrongContinuationEvent` stores observation status and favorable/adverse excursion fields when available.

- [ ] Add Kotlin tests for evidence formatting and review sections.
- [ ] Run the focused Android tests and verify failure before implementation.
- [ ] Implement Kotlin evidence cards and dynamic report review.
- [ ] Extend event encode/decode with optional outcome fields, retaining old records.
- [ ] Run Android unit tests and debug build.

### Task 5: Final verification and archive notes

**Files:**
- Modify: `CHANGELOG.md` or the project’s existing dated change log.
- Create: `docs/strategies/evidence-review-and-t-filter-tracking.md`

- [ ] Document the adopted design, thresholds, evidence boundaries, and what was deliberately not copied.
- [ ] Run desktop full tests and Android unit tests/build with fresh output.
- [ ] Review diffs for hard-coded directional claims, stale-data misuse, and duplicated thresholds.
- [ ] Record whether the change is a positive optimization based on tests and preserved signal priority.
