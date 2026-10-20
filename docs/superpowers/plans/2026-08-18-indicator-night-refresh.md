# 指标策略夜间刷新 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restrict indicator screening to one successful refresh per 18:00–06:00 window, with persisted state, eight failed attempts maximum, and no overlapping scans.

**Architecture:** Add a pure-Python scheduler module that owns window calculation, persisted attempt state, and status messages. `StockMenuBar` delegates scheduling decisions to that module, uses an instance lock for a single active scan, and only replaces the menu results after a completed scan. Keep AkShare's Sina spot and daily-data sources, while applying a default request timeout around each Sina call so a blocked upstream connection becomes a recorded failure.

**Tech Stack:** Python 3, standard-library `datetime`/`json`/`threading`/`unittest`, `requests`, `pandas`.

---

### Task 1: Test the persisted refresh scheduler

**Files:**
- Create: `tests/test_indicator_refresh.py`
- Create: `indicator_refresh.py`

- [ ] **Step 1: Write failing tests for window ownership and limits**

```python
from datetime import datetime, timedelta

from indicator_refresh import IndicatorRefreshScheduler


def test_night_window_uses_previous_date_after_midnight(tmp_path):
    scheduler = IndicatorRefreshScheduler(tmp_path / "state.json")

    assert scheduler.window_date_for(datetime(2026, 8, 18, 17, 59)) is None
    assert scheduler.window_date_for(datetime(2026, 8, 18, 18, 0)) == "2026-08-18"
    assert scheduler.window_date_for(datetime(2026, 8, 19, 5, 59)) == "2026-08-18"
    assert scheduler.window_date_for(datetime(2026, 8, 19, 6, 0)) is None


def test_success_blocks_same_window_after_restart(tmp_path):
    state_file = tmp_path / "state.json"
    now = datetime(2026, 8, 18, 18, 0)
    scheduler = IndicatorRefreshScheduler(state_file)

    assert scheduler.can_attempt(now)
    scheduler.begin_attempt(now)
    scheduler.record_success(now)

    restarted = IndicatorRefreshScheduler(state_file)
    assert not restarted.can_attempt(datetime(2026, 8, 19, 1, 0))
    assert restarted.status(datetime(2026, 8, 19, 1, 0)) == "本窗口已成功刷新"


def test_failures_are_limited_to_eight_and_reset_next_window(tmp_path):
    scheduler = IndicatorRefreshScheduler(tmp_path / "state.json")
    now = datetime(2026, 8, 18, 18, 0)

    for attempt in range(8):
        attempt_time = now + timedelta(minutes=90 * attempt)
        scheduler.begin_attempt(attempt_time)
        scheduler.record_failure(attempt_time, "network unavailable")

    assert not scheduler.can_attempt(datetime(2026, 8, 19, 5, 59))
    assert scheduler.status(datetime(2026, 8, 19, 5, 59)) == "本窗口已尝试 8 次，等待下一晚"
    assert scheduler.can_attempt(datetime(2026, 8, 19, 18, 0))


def test_scheduler_defers_failed_retry_for_ninety_minutes(tmp_path):
    scheduler = IndicatorRefreshScheduler(tmp_path / "state.json")
    now = datetime(2026, 8, 18, 18, 0)

    scheduler.begin_attempt(now)
    scheduler.record_failure(now, "spot request timed out")

    assert not scheduler.can_attempt(datetime(2026, 8, 18, 19, 29))
    assert scheduler.can_attempt(datetime(2026, 8, 18, 19, 30))
```

- [ ] **Step 2: Run the tests to confirm the missing module failure**

Run: `python3 -m unittest discover -s tests -p 'test_indicator_refresh.py' -v`

Expected: collection fails because `indicator_refresh` does not exist.

- [ ] **Step 3: Implement the minimal scheduler and atomic state persistence**

```python
class IndicatorRefreshScheduler:
    MAX_ATTEMPTS = 8
    RETRY_DELAY = timedelta(minutes=90)

    def can_attempt(self, now):
        self._sync_window(now)
        return (
            self.window_date_for(now) is not None
            and not self.state["success_at"]
            and self.state["attempt_count"] < self.MAX_ATTEMPTS
            and self._next_attempt_is_due(now)
        )
```

Implement `window_date_for`, `begin_attempt`, `record_success`, `record_failure`, `status`, `_load_state`, `_save_state`, and `_sync_window`. `_save_state` must write a temporary file in the target directory then use `os.replace`.

- [ ] **Step 4: Run scheduler tests**

Run: `python3 -m unittest discover -s tests -p 'test_indicator_refresh.py' -v`

Expected: all scheduler tests pass.

### Task 2: Integrate bounded screening with the menu bar

**Files:**
- Modify: `stock_menubar.py:8-25`
- Modify: `stock_menubar.py:75-115`
- Modify: `stock_menubar.py:462-595`
- Modify: `stock_menubar.py:597-629`
- Modify: `stock_menubar.py:790-804`

- [ ] **Step 1: Wire scheduling, locking, and status into `StockMenuBar`**

```python
def maybe_refresh_indicators(self):
    now = datetime.now()
    if not self.indicator_refresh_scheduler.can_attempt(now):
        return
    if not self.indicator_screen_lock.acquire(blocking=False):
        return
    self.indicator_refresh_scheduler.begin_attempt(now)
    try:
        result = self.screen_by_indicators()
        if result is None:
            self.indicator_refresh_scheduler.record_failure(datetime.now(), self.indicator_last_error)
            return
        self.indicator_data = result
        self.indicator_refresh_scheduler.record_success(datetime.now())
    finally:
        self.indicator_screen_lock.release()
```

Call this method in `_fetch_data` instead of incrementing `indicator_update_count`. Extend `indicator_menu_titles` with the scheduler status and preserve existing result lists on a failed scan. Use `rumps.Timer` to refresh the menu after a successful or failed scan.

- [ ] **Step 2: Bound Sina spot and daily-data requests**

```python
with self.requests_timeout(15):
    spot_df = ak.stock_zh_a_spot()
    history_df = ak.stock_zh_a_daily(
        symbol=code,
        start_date=start_date,
        end_date=end_date,
        adjust="qfq",
    )
```

Apply the timeout wrapper while holding the existing indicator API lock. Keep AkShare's Sina-returned `代码`、`名称`、`成交量` columns and existing active-volume sorting. Make `screen_by_indicators` return `None` for a source-request failure while retaining `[]` as a valid successful zero-result screen.

- [ ] **Step 3: Run the focused scheduler tests**

Run: `python3 -m unittest discover -s tests -p 'test_indicator_refresh.py' -v`

Expected: all tests pass.

### Task 3: Validate syntax and behavior

**Files:**
- Modify: `stock_menubar.py`
- Create: `indicator_refresh.py`
- Create: `tests/test_indicator_refresh.py`

- [ ] **Step 1: Compile modified Python modules**

Run: `python3 -m py_compile stock_menubar.py indicator_refresh.py`

Expected: no output and exit code 0.

- [ ] **Step 2: Run all non-interactive tests**

Run: `python3 -m unittest discover -s tests -p 'test_indicator_refresh.py' -v`

Expected: all tests pass without invoking macOS notifications.

- [ ] **Step 3: Review changed files**

Run: `git diff -- stock_menubar.py indicator_refresh.py tests/test_indicator_refresh.py`

Expected: not available because this workspace is not a Git repository; use `diff -u` copies or inspect the named files directly instead.

- [ ] **Step 4: Do not create a commit**

This workspace is not a Git repository, so leave the validated working files uncommitted.
