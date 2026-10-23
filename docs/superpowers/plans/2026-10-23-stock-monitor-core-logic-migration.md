# Stock Monitor 核心逻辑迁移 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Align `stock_monitor_mac` with the Android core-logic summary for today's headlines, limit-up quality, fund-flow evidence, candidate promotion, environment gating, persistence, and presentation.

**Architecture:** Add pure policy modules for each rule domain, upgrade `ObservationPool` to a versioned state machine, then wire the policies into the existing refresh/menu/floating-window orchestration. Collection and persistence stay outside pure policy code, while all trading actions remain behind the existing T+1 guard.

**Tech Stack:** Python 3, `unittest`, `requests`, existing JSON state files, `rumps` menu UI, and the existing flat-module project structure.

**Spec:** `docs/superpowers/specs/2026-10-23-stock-monitor-core-logic-migration-design.md`

## Global Constraints

- Fund flow is auxiliary evidence and cannot independently create a buy signal.
- Sealed limit-up stocks never enter `NEAR_ENTRY` or `BUY_CANDIDATE`.
- A-share trading remains T+1; candidate promotion is never an order or execution promise.
- Missing market, board, fund-flow, or seal-quality data remains unknown/low-confidence and is never guessed.
- `promotion_environment_gate_enabled` defaults to `False`.
- Today's headlines only include items published on the current `Asia/Shanghai` calendar day.
- East Money is the primary headline source; Sina is used only when East Money has no usable current-day items.
- The displayed news `source` is the publisher; `fetch_channel` records the API actually used.
- Preserve existing files and behavior outside the listed migration surfaces.
- Do not create Git commits or branches.

---

## File Structure

- Create `capital_flow_policy.py`: pure single-day and 3–5-day fund-flow assessment.
- Create `limit_up_policy.py`: pure East Money limit-pool admission, quality score, and ranking.
- Create `candidate_state_policy.py`: pure candidate strategy/state/confirmation transitions.
- Create `promotion_environment_policy.py`: pure market/sector promotion gate.
- Create `market_news_client.py`: East Money/Sina collection and normalization.
- Create `news_digest_policy.py`: pure same-day filtering, classification, deduplication, and ranking.
- Create `today_headlines.py`: cache, refresh-window policy, report formatting, and serialization.
- Modify `capital_flow.py`: retain fetching/cache and delegate policy decisions.
- Modify `observation_pool.py`: version-3 migration and state-machine persistence.
- Modify `observation_ui.py`: strategy/state grouping, page size, entry destination, and expandable detail models.
- Modify `stock_menubar.py`: refresh orchestration, configuration, menu/report integration, and stale-cache retention.
- Tests: focused test modules under `tests/`, then the existing full suite.

---

### Task 1: Fund-flow policy

**Files:**
- Create: `capital_flow_policy.py`
- Modify: `capital_flow.py:183-242`
- Test: `tests/test_capital_flow_policy.py`

**Interfaces:**
- Consumes: `CapitalFlowSnapshot` fields `quote_date`, `net_ratio_pct`, `net_yi`, `turnover_yi`, and a normalized price-change percentage.
- Produces: `FundFlowTone`, `FundFlowAssessment`, `assess_single_day(change_pct, snapshot)`, `assess_window(snapshots, price_change_pct)`, and `score_delta(assessment)`.

- [ ] **Step 1: Write the failing policy tests**

```python
import unittest
from datetime import datetime
from capital_flow_policy import FundFlowTone, assess_single_day, assess_window, score_delta
from capital_flow import CapitalFlowSnapshot


def snap(day, net_ratio, amount_yi=1.0):
    return CapitalFlowSnapshot(
        key="sh600000", name="样本", kind="stock", quote_date=f"2026-10-{day}",
        as_of=datetime.fromisoformat(f"2026-10-{day}T15:00:00"),
        source="EastMoney.fflow.kline", scope="个股", session="daily",
        net_yi=amount_yi * net_ratio / 100, net_ratio_pct=net_ratio,
        turnover_yi=amount_yi,
    )


class FundFlowPolicyTests(unittest.TestCase):
    def test_single_day_four_directions(self):
        self.assertEqual(assess_single_day(1.0, snap(23, 3.0)).tone, FundFlowTone.SUPPORTIVE)
        self.assertEqual(assess_single_day(1.0, snap(23, -3.0)).tone, FundFlowTone.DIVERGENT)
        self.assertEqual(assess_single_day(-1.0, snap(23, -3.0)).tone, FundFlowTone.RISK)
        self.assertEqual(assess_single_day(-1.0, snap(23, 3.0)).tone, FundFlowTone.ABSORPTION_WATCH)

    def test_small_turnover_is_low_confidence_and_zero_score(self):
        result = assess_single_day(1.0, snap(23, 4.0, amount_yi=0.04))
        self.assertTrue(result.low_confidence)
        self.assertEqual(score_delta(result), 0)

    def test_three_day_window(self):
        flows = [snap(21, 3.0), snap(22, 4.0), snap(23, 2.0)]
        result = assess_window(flows, 1.0)
        self.assertEqual(result.tone, FundFlowTone.SUSTAINED_INFLOW)
        self.assertEqual(result.score_delta, 1)

    def test_missing_third_sample_accumulates_without_scoring(self):
        result = assess_window([snap(22, 4.0), snap(23, 4.0)], 1.0)
        self.assertEqual(result.tone, FundFlowTone.ACCUMULATING)
        self.assertEqual(result.score_delta, 0)
```

- [ ] **Step 2: Run the test and verify the expected import failure**

Run: `python3 -m unittest tests.test_capital_flow_policy -v`
Expected: FAIL with `ModuleNotFoundError: capital_flow_policy`.

- [ ] **Step 3: Implement the pure policy**

Implement `FundFlowTone` with `SUPPORTIVE`, `DIVERGENT`, `RISK`, `ABSORPTION_WATCH`, `NEUTRAL`, `UNAVAILABLE`, `ACCUMULATING`, `SUSTAINED_INFLOW`, and `SUSTAINED_OUTFLOW`. Use `MIN_RELIABLE_TURNOVER_YI = 0.05`, `SINGLE_DAY_NET_PCT = 3.0`, and `WINDOW_NET_PCT = 3.0`. `assess_window` keeps the newest three trading-date snapshots, requires three distinct dates, and returns `SUSTAINED_INFLOW` only when the mean is at least `3.0` and at least two values are positive; it returns `SUSTAINED_OUTFLOW` only when the mean is at most `-3.0` and no more than one value is positive. Any low-turnover sample makes the result low-confidence with score `0`.

Add `turnover_yi: Optional[float] = None` to `CapitalFlowSnapshot` and populate it in `normalize_flow_frame` when a turnover field exists. Keep the existing intraday `flow_confirmation_state` unchanged so this policy cannot alter盘中买卖/T+1 behavior; the candidate state layer will consume `assess_single_day` and `assess_window` in Task 6.

- [ ] **Step 4: Run focused and adjacent tests**

Run: `python3 -m unittest tests.test_capital_flow_policy tests.test_capital_flow -v`
Expected: PASS with no failures.

---

### Task 2: Limit-up admission and ranking

**Files:**
- Create: `limit_up_policy.py`
- Test: `tests/test_limit_up_policy.py`

**Interfaces:**
- Consumes: normalized limit-stock dictionaries containing `source`, `limit_streak`, `open_count`, `seal_to_float_pct`, `seal_to_turnover_pct`, `change_pct`, `first_seal_time`, `last_seal_time`, and optional fund-flow percentage.
- Produces: `LimitCandidatePolicy.eligible(stock)`, `evaluate_limit_candidate(stock)`, `seal_quality_score(stock)`, `rank_limit_up(stocks)`, and `rank_limit_down(stocks)`.

- [ ] **Step 1: Write the failing tests**

```python
import unittest
from limit_up_policy import LimitCandidatePolicy, rank_limit_down, rank_limit_up


def stock(code, **overrides):
    row = {"code": code, "name": code, "source": "EASTMONEY_LIMIT_POOL", "limit_streak": 1,
           "open_count": 1, "seal_to_float_pct": 0.5, "seal_to_turnover_pct": 9.0,
           "change_pct": 10.0, "first_seal_time": "09:35:00", "last_seal_time": "14:00:00"}
    row.update(overrides)
    return row


class LimitUpPolicyTests(unittest.TestCase):
    def test_admission_boundaries(self):
        self.assertTrue(LimitCandidatePolicy.eligible(stock("a")))
        self.assertTrue(LimitCandidatePolicy.eligible(stock("b", seal_to_float_pct=None,
                                                           seal_to_turnover_pct=10.0)))
        self.assertFalse(LimitCandidatePolicy.eligible(stock("c", source="SINA_SCAN")))
        self.assertFalse(LimitCandidatePolicy.eligible(stock("d", open_count=2)))
        self.assertFalse(LimitCandidatePolicy.eligible(stock("e", limit_streak=0)))

    def test_limit_up_orders_by_seal_quality(self):
        weak = stock("weak", seal_to_float_pct=0.2, seal_to_turnover_pct=20.0, open_count=1)
        strong = stock("strong", seal_to_float_pct=2.0, seal_to_turnover_pct=30.0, open_count=0)
        self.assertEqual([row["code"] for row in rank_limit_up([weak, strong])], ["strong", "weak"])

    def test_limit_down_uses_change_order_without_limit_score(self):
        rows = rank_limit_down([stock("a", change_pct=-8.0), stock("b", change_pct=-10.0)])
        self.assertEqual([row["code"] for row in rows], ["b", "a"])
        self.assertNotIn("seal_quality_score", rows[0])
```

- [ ] **Step 2: Run the test and verify `limit_up_policy` is missing**

Run: `python3 -m unittest tests.test_limit_up_policy -v`
Expected: FAIL with `ModuleNotFoundError: limit_up_policy`.

- [ ] **Step 3: Implement admission, quality, and ranking**

`eligible` must require `source == "EASTMONEY_LIMIT_POOL"`, `limit_streak >= 1`, `0 <= open_count <= 1`, and at least one ratio threshold. `seal_quality_score` uses bounded components: seal/float ratio up to 40 points, seal/turnover ratio up to 30 points, `open_count == 0` for 15 points or `open_count == 1` for 7 points, and `limit_streak` up to 15 points. `rank_limit_up` assigns that score and coverage percentage to copies, then sorts descending; `rank_limit_down` sorts `change_pct` ascending and must not add limit-up quality fields.

- [ ] **Step 4: Run the focused test**

Run: `python3 -m unittest tests.test_limit_up_policy -v`
Expected: PASS.

---

### Task 3: Promotion environment gate

**Files:**
- Create: `promotion_environment_policy.py`
- Test: `tests/test_promotion_environment_policy.py`

**Interfaces:**
- Consumes: `PromotionEnvironmentInput(shanghai_change_pct, shenzhen_change_pct, limit_up_count, limit_down_count, board_changes)`.
- Produces: `evaluate_environment(input, enabled=False) -> PromotionEnvironmentDecision`, with `blocked: bool`, `reason: str | None`, and `evidence: list[str]`.

- [ ] **Step 1: Write the failing boundary tests**

```python
import unittest
from promotion_environment_policy import PromotionEnvironmentInput, evaluate_environment


class PromotionEnvironmentPolicyTests(unittest.TestCase):
    def test_default_disabled_records_but_does_not_block(self):
        decision = evaluate_environment(PromotionEnvironmentInput(-1.0, 0.0, 10, 30, [-2.0]))
        self.assertFalse(decision.blocked)
        self.assertTrue(decision.evidence)

    def test_enabled_gate_has_three_independent_reasons(self):
        cases = [
            PromotionEnvironmentInput(-0.8, 0.0, 100, 1, []),
            PromotionEnvironmentInput(0.0, 0.0, 19, 20, []),
            PromotionEnvironmentInput(0.0, 0.0, 100, 1, [-1.0, -1.2]),
        ]
        for item in cases:
            with self.subTest(item=item):
                self.assertTrue(evaluate_environment(item, enabled=True).blocked)

    def test_unknown_data_and_partial_weak_boards_do_not_block(self):
        decision = evaluate_environment(PromotionEnvironmentInput(None, None, None, None, [None]), enabled=True)
        self.assertFalse(decision.blocked)
        decision = evaluate_environment(PromotionEnvironmentInput(0.0, 0.0, 10, 1, [-2.0, 1.0]), enabled=True)
        self.assertFalse(decision.blocked)
```

- [ ] **Step 2: Run the test and verify the module is missing**

Run: `python3 -m unittest tests.test_promotion_environment_policy -v`
Expected: FAIL with `ModuleNotFoundError: promotion_environment_policy`.

- [ ] **Step 3: Implement the pure gate**

When enabled, block on an index `<= -0.8`, `limit_down_count >= 20` and greater than limit-up count, or every known board change `<= -1.0`. Unknown values contribute no block. Always return the observed evidence; when disabled, `blocked` is always `False`.

- [ ] **Step 4: Run the focused test**

Run: `python3 -m unittest tests.test_promotion_environment_policy -v`
Expected: PASS.

---

### Task 4: Candidate state and confirmation policy

**Files:**
- Create: `candidate_state_policy.py`
- Test: `tests/test_candidate_state_policy.py`

**Interfaces:**
- Consumes: `CandidateContext` and `CandidateReviewInput` dataclasses with strategy, limit streak/seal state, ordinary gates, trend weakness, timestamps, and observation dates.
- Produces: `required_confirmation_rounds(limit_streak)`, `advance_candidate(record, review) -> CandidateTransition`, and `route_source(source, strong_candidate=False, limit_candidate=False) -> CandidateStrategy`.

- [ ] **Step 1: Write the failing tests**

```python
import unittest
from datetime import datetime
from candidate_state_policy import (
    CandidateReviewInput, CandidateState, CandidateStrategy,
    advance_candidate, required_confirmation_rounds, route_source,
)


def review(at, ordinary=True, trend_weak=False, **overrides):
    values = dict(at=at, ordinary_gates_passed=ordinary, trend_weak=trend_weak,
                  price_trend_valid=True, fresh_sample=True, sealed=False,
                  limit_streak=1, environment_blocked=False)
    values.update(overrides)
    return CandidateReviewInput(**values)


class CandidateStatePolicyTests(unittest.TestCase):
    def test_rounds_and_source_routing(self):
        self.assertEqual(required_confirmation_rounds(2), 2)
        self.assertEqual(required_confirmation_rounds(3), 3)
        self.assertEqual(route_source("strict"), CandidateStrategy.PRE_BREAKOUT)
        self.assertEqual(route_source("limit", limit_candidate=True),
                         CandidateStrategy.LIMIT_UP_CONTINUATION)

    def test_confirmation_requires_min_gap_and_expires_after_45_minutes(self):
        first = datetime(2026, 10, 23, 10, 0)
        state = advance_candidate({"state": CandidateState.OBSERVING.value}, review(first))
        self.assertEqual(state.record["confirmation_rounds"], 1)
        state = advance_candidate(state.record, review(datetime(2026, 10, 23, 10, 10)))
        self.assertEqual(state.record["confirmation_rounds"], 1)
        state = advance_candidate(state.record, review(datetime(2026, 10, 23, 11, 1)))
        self.assertEqual(state.record["confirmation_rounds"], 1)
        self.assertEqual(state.record["state"], CandidateState.NEAR_ENTRY.value)

    def test_sealed_candidate_observes_and_reseal_resets_confirmation(self):
        record = {"state": CandidateState.NEAR_ENTRY.value, "confirmation_rounds": 1,
                  "confirmation_started_at": datetime(2026, 10, 23, 10, 0).isoformat()}
        state = advance_candidate(record, review(datetime(2026, 10, 23, 10, 30), sealed=True))
        self.assertEqual(state.record["state"], CandidateState.OBSERVING.value)
        self.assertEqual(state.record["confirmation_rounds"], 0)
        self.assertEqual(state.blockers, ())
```

- [ ] **Step 2: Run the test and verify the module is missing**

Run: `python3 -m unittest tests.test_candidate_state_policy -v`
Expected: FAIL with `ModuleNotFoundError: candidate_state_policy`.

- [ ] **Step 3: Implement the state policy**

Define strategy `PRE_BREAKOUT`, `LEADER`, `LIMIT_UP_CONTINUATION`; state `OBSERVING`, `NEAR_ENTRY`, `BUY_CANDIDATE`, `ELIMINATED`. Require 2 fresh time-separated rounds for streaks 1–2 and 3 rounds for streaks 3+. Consecutive samples must be at least 15 minutes apart and within 45 minutes of confirmation start. Cumulative volume is not accepted as evidence. A sealed review forces `OBSERVING`, clears rounds, blocks buy promotion, and preserves continuous observation. Three distinct weak trading dates eliminate; ten distinct observed trading dates without confirmation eliminate. Missing/invalid/future/non-trading samples do not mutate the record.

- [ ] **Step 4: Run the focused test**

Run: `python3 -m unittest tests.test_candidate_state_policy -v`
Expected: PASS.

---

### Task 5: Today's headlines

**Files:**
- Create: `market_news_client.py`
- Create: `news_digest_policy.py`
- Create: `today_headlines.py`
- Test: `tests/test_today_headlines.py`

**Interfaces:**
- Produces: `NewsItem`, `parse_eastmoney(text)`, `parse_sina(text)`, `fetch_today(now=None)`, `select_headlines(items, now, card_limit=5, detail_limit=20)`, `TodayHeadlinesState.should_refresh(now)`, and `format_headlines(items, updated_at, detail=False)`.

- [ ] **Step 1: Write the failing selection and persistence tests**

```python
import unittest
from datetime import datetime
from market_news_client import NewsItem
from news_digest_policy import NewsCategory, select_headlines
from today_headlines import TodayHeadlinesState


def item(title, minute, source="第一财经", channel="EASTMONEY"):
    return NewsItem(title=title, published_at=datetime(2026, 10, 23, 10, minute),
                    publisher_source=source, fetch_channel=channel,
                    url="https://example.com/news")


class TodayHeadlinesTests(unittest.TestCase):
    def test_select_filters_today_deduplicates_and_ranks(self):
        rows = [
            item("市场快讯", 1),
            item("央行发布政策", 2),
            item("半导体行业订单增加", 3),
            item("公司披露业绩", 4),
            item("央行发布政策", 2, source="其他媒体"),
            NewsItem("旧闻", datetime(2026, 10, 22, 10, 0), "新华社", "SINA"),
        ]
        selected = select_headlines(rows, datetime(2026, 10, 23, 12, 0))
        self.assertEqual(selected[0].category, NewsCategory.MACRO_POLICY)
        self.assertEqual(len(selected), 4)

    def test_card_and_detail_limits(self):
        rows = [item(f"央行新闻{i}", i) for i in range(25)]
        self.assertEqual(len(select_headlines(rows, datetime(2026, 10, 23, 12, 0), 5, 20)), 20)

    def test_refresh_window_and_thirty_minute_interval(self):
        state = TodayHeadlinesState(last_attempt_at=datetime(2026, 10, 23, 9, 0))
        self.assertFalse(state.should_refresh(datetime(2026, 10, 23, 9, 15)))
        self.assertTrue(state.should_refresh(datetime(2026, 10, 23, 9, 30)))
        self.assertFalse(state.should_refresh(datetime(2026, 10, 23, 22, 1)))
```

- [ ] **Step 2: Run the test and verify imports fail**

Run: `python3 -m unittest tests.test_today_headlines -v`
Expected: FAIL with `ModuleNotFoundError: market_news_client`.

- [ ] **Step 3: Implement collection, selection, and cache**

Use the East Money endpoint `https://np-listapi.eastmoney.com/comm/web/getNewsByColumns?client=web&biz=web_news&column=350&order=1&needInteractData=0&page_index=1&page_size=50` first and Sina `https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid=2516&k=&num=50&page=1` only if East Money returns no usable items from the current `Asia/Shanghai` date. Parse JSON/JSONP defensively, preserve publisher and URL, and set `fetch_channel` to the actual endpoint. Classification order is `MACRO_POLICY`, `SECTOR_INDUSTRY`, `COMPANY_EVENT`, `MARKET_GENERAL`, then newest publication time. Preserve at most 20 selected rows in cache, retain the last valid list on failed refresh, and never relabel older items as today. `format_headlines` includes category, publisher, time, and URL without directional claims.

- [ ] **Step 4: Run focused tests and compile**

Run: `python3 -m unittest tests.test_today_headlines -v && python3 -m py_compile market_news_client.py news_digest_policy.py today_headlines.py`
Expected: PASS and successful compilation.

---

### Task 6: Observation pool version 3

**Files:**
- Modify: `observation_pool.py:55-470`
- Test: `tests/test_observation_pool_v3.py`

**Interfaces:**
- Consumes: `route_source`, `advance_candidate`, `required_confirmation_rounds`, `FundFlowAssessment`, `LimitCandidatePolicy`, and `PromotionEnvironmentDecision`.
- Produces: `ObservationPool.state_version = 3`, `ObservationPool.required_confirmation_rounds(limit_streak)`, `ObservationPool.review(...)`, and JSON records containing the spec's version-3 fields.

- [ ] **Step 1: Write failing migration/state tests**

```python
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from observation_pool import ObservationPool


class ObservationPoolV3Tests(unittest.TestCase):
    def test_version_two_migrates_without_guessing_new_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pool.json"
            path.write_text(json.dumps({"version": 2, "records": {"sh600000": {
                "code": "sh600000", "name": "样本", "sources": ["strict"],
                "first_observed_date": "2026-10-20", "last_seen_date": "2026-10-22",
                "last_reviewed_date": None, "observation_days": 1, "status": "observing",
                "reason": "历史", "data_status": "已复核", "price": 10, "score": 1,
                "stop_price": None, "history": [],
            }}}), encoding="utf-8")
            pool = ObservationPool(path)
            self.assertIsNone(pool.load_error)
            record = pool.records["sh600000"]
            self.assertEqual(record["strategy"], "PRE_BREAKOUT")
            self.assertEqual(record["state"], "OBSERVING")
            self.assertIsNone(record["seal_quality_score"])
            self.assertEqual(record["flow_window"], [])

    def test_three_board_requires_three_confirmations(self):
        pool = ObservationPool()
        self.assertEqual(pool.required_confirmation_rounds(3), 3)

    def test_environment_gate_off_does_not_block_but_persists_evidence(self):
        pool = ObservationPool()
        pool.records["sh600001"] = {"code": "sh600001", "name": "样本", "sources": ["strict"],
                                   "first_observed_date": "2026-10-20", "history": [],
                                   "status": "observing", "strategy": "PRE_BREAKOUT"}
        record = pool.records["sh600001"]
        result = pool.apply_promotion_environment(record, {
            "shanghai_change_pct": -1.0, "limit_down_count": 30,
        }, enabled=False, at=datetime(2026, 10, 23, 15))
        self.assertFalse(result["blocked"])
        self.assertTrue(result["evidence"])
```

- [ ] **Step 2: Run the test and verify the new methods fail**

Run: `python3 -m unittest tests.test_observation_pool_v3 -v`
Expected: FAIL with `AttributeError` or the existing version validation error.

- [ ] **Step 3: Implement version 3 migration and state persistence**

Change serialization to `{"version": 3, "config": {...}, "records": {...}}`. Accept version 2 and normalize old statuses `observing/ready/expired/eliminated` to `OBSERVING/NEAR_ENTRY/ELIMINATED/ELIMINATED`, preserving history. Missing seal/flow/sector/confirmation fields become `None`, `[]`, `[]`, and zero without inference. Keep atomic writes and failure-safe loading. Integrate `advance_candidate` for all state changes, add `apply_promotion_environment`, and retain the existing trend/high-position/strong-continuation evidence as individual risk blockers.

- [ ] **Step 4: Run focused and existing observation tests**

Run: `python3 -m unittest tests.test_observation_pool_v3 tests.test_observation_pool -v`
Expected: PASS.

---

### Task 7: UI and orchestration

**Files:**
- Modify: `observation_ui.py:29-104`
- Modify: `stock_menubar.py:3423-3715`, `stock_menubar.py:4373-4535`
- Test: `tests/test_core_migration_ui.py`

**Interfaces:**
- Consumes: version-3 observation records, `select_headlines`, `format_headlines`, `rank_limit_up`, `rank_limit_down`, and `TodayHeadlinesState`.
- Produces: `build_observation_view(pool, at, page=1, expanded=False)`, `build_limit_views(up_rows, down_rows)`, menu lines showing `入池去向`, and a `📰 今日要闻` menu/report action.

- [ ] **Step 1: Write failing structural tests**

```python
import unittest
from datetime import datetime
from types import SimpleNamespace
from observation_ui import build_observation_view


class CoreMigrationUiTests(unittest.TestCase):
    def record(self, code, strategy, state):
        return {"code": code, "name": code, "strategy": strategy, "state": state,
                "entry_destination": strategy + " / 测试", "status": state.lower(),
                "score": 1, "sources": ["strict"], "history": []}

    def test_groups_by_strategy_and_excludes_non_entry_states(self):
        pool = SimpleNamespace(load_error=None, records={
            "a": self.record("a", "PRE_BREAKOUT", "NEAR_ENTRY"),
            "b": self.record("b", "LIMIT_UP_CONTINUATION", "OBSERVING"),
            "c": self.record("c", "LEADER", "ELIMINATED"),
        })
        view = build_observation_view(pool, datetime(2026, 10, 23, 15))
        self.assertIn("启动前潜力", "\n".join(view["focus_lines"]))
        self.assertIn("涨停持续观察", "\n".join(view["focus_lines"]))
        self.assertNotIn("强势龙头", "\n".join(view["focus_lines"]))

    def test_page_limit_is_eight(self):
        rows = {str(i): self.record(str(i), "PRE_BREAKOUT", "NEAR_ENTRY") for i in range(20)}
        view = build_observation_view(SimpleNamespace(load_error=None, records=rows),
                                      datetime(2026, 10, 23, 15), page=1)
        self.assertEqual(len(view["items"]), 8)
        self.assertEqual(view["page"], 1)
        self.assertEqual(view["page_count"], 3)
```

- [ ] **Step 2: Run the test and verify view fields fail**

Run: `python3 -m unittest tests.test_core_migration_ui -v`
Expected: FAIL with missing `items`, `page`, or `page_count`.

- [ ] **Step 3: Implement structured views and service wiring**

Return structured `items`, `groups`, `page`, `page_count`, `expanded`, `entry_destination`, `flow_window`, `support_pressure`, `structure_evidence`, and `risk_line` fields. Keep focus text generated from these fields. Use 8 entries per page. Separate limit-up and limit-down views and apply only `rank_limit_up`/`rank_limit_down`. Add a `📰 今日要闻` menu with refresh and full-report actions; automatic refresh runs only 08:30–22:00 at a 30-minute interval and uses cached state in render callbacks. Preserve the last valid news list on failure. Do not route candidate states into `trade_action_guard`; existing action outputs remain authoritative.

- [ ] **Step 4: Run focused UI/integration tests**

Run: `python3 -m unittest tests.test_core_migration_ui tests.test_observation_ui_integration tests.test_all_intraday_action_surfaces -v`
Expected: PASS, proving UI changes do not bypass action guards.

---

### Task 8: Documentation, regression, and metric report

**Files:**
- Modify: `A股监控使用说明.md`
- Modify: `backtest_entry_structure.py:185-255`
- Create: `docs/2026-10-23-core-logic-migration-report.md`
- Test: `tests/test_backtest_promotion_metrics.py` and full suite

**Interfaces:**
- Consumes: all completed migration modules and tests.
- Produces: user-facing thresholds/caveats and a final validation report containing test totals and backtest metric columns.

- [ ] **Step 1: Update user documentation**

Document today's headline source fallback and 08:30–22:00 refresh behavior, fund-flow order-size caveat, limit-up admission thresholds, three strategy paths, 2/3-round 15–45 minute confirmation, disabled environment gate, observation status boundaries, pagination/expansion, and T+1 wording. State that promotion is not an order and risk thresholds do not guarantee returns.

- [ ] **Step 2: Run full verification**

Run: `python3 -m unittest discover -s tests`
Expected: all tests PASS; existing GUI tests may remain explicitly skipped only if their current suite already skips them.

Run: `python3 -m py_compile *.py`
Expected: successful compilation.

Run: `git diff --check`
Expected: no whitespace errors.

- [ ] **Step 3: Add promotion metric output and produce the historical report**

Extend `attach_forward_returns` to record the forward close path and calculate each record's maximum peak-to-trough drawdown through 5 trading days. Extend `summarize` to emit `ret_1d`, `ret_3d`, and `ret_5d`, `max_drawdown_5d`, and `miss_rate`. Define `miss_rate = eligible_not_promoted / eligible`, where `eligible` counts records passing ordinary daily structure gates and `eligible_not_promoted` counts those without `can_buy`; expose the counts so the rate is auditable. Add `tests/test_backtest_promotion_metrics.py` using fixed `SignalRecord`/bar fixtures to assert one positive case, one stop-path drawdown case, and `miss_rate = 2 / 4`.

Run:

```bash
python3 backtest_entry_structure.py --samples 300 --seed 42 --out /tmp/stock-monitor-backtest-2026-10-23.json
python3 backtest_compare.py /tmp/stock-monitor-backtest-2026-10-23.json
```

Copy the promotion groups' next-day/3-day/5-day average return, maximum drawdown, win rate, eligible count, and miss rate into `docs/2026-10-23-core-logic-migration-report.md`. Do not tune thresholds unless the report identifies a concrete boundary failure; retain documented thresholds when evidence is inconclusive.

- [ ] **Step 4: Final review**

Verify every acceptance criterion in the spec maps to a passing test, UI behavior, or documented runtime configuration. Check that only intended files changed and leave all work uncommitted for user review.

## Execution Handoff

This plan should be executed inline in this session unless the user explicitly requests subagents. Use `superpowers:executing-plans`, stop at each task boundary for a quick diff/test review, and do not create commits.
