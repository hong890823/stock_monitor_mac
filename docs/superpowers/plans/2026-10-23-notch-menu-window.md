# 刘海屏菜单入口替代窗口 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在刘海可能遮挡菜单栏时自动显示右上角替代窗口，并从该窗口弹出现有完整菜单；普通屏幕继续使用系统菜单栏，移除旧浮动窗口。

**Architecture:** 新增 `menu_bar_window.py` 承担屏幕几何、窗口状态、摘要格式化、原生菜单弹出和 AppKit 悬浮面板。`stock_menubar.py` 继续拥有数据和 `rumps` 菜单，只负责初始化、刷新摘要、评估屏幕和转发菜单根节点；后台刷新与提醒逻辑不进入新组件。

**Tech Stack:** Python 3.9、PyObjC/AppKit、rumps、`unittest`、现有 JSON 日志目录。

**Spec:** `docs/superpowers/specs/2026-10-23-notch-menu-window-design.md`

## Global Constraints

- 普通屏幕点击系统菜单栏后使用现有完整菜单。
- 刘海风险屏幕启动后自动显示右上角替代窗口，不依赖点击被遮挡的菜单项。
- 点击替代窗口弹出的菜单必须复用当前 `rumps` 菜单树和原 callback。
- 浏览器全屏、全屏应用、所有桌面空间中替代窗口保持可见，且不抢占当前应用焦点。
- 窗口可拖动并按显示器配置保存位置；越界或状态损坏时回到右上角安全区域。
- 新窗口不读取行情、不新增轮询、不生成信号、不参与 T+1 或盘中动作。
- 移除旧 `FloatingWindow` 及其菜单入口、模式和数据投影。
- 屏幕检测、AppKit 初始化或菜单构建失败时，系统菜单栏入口和后台监控保持可用。
- 不创建 Git commit 或 branch，直接在 `/Users/stephen/Desktop/stock_monitor_mac` 修改。

## File Structure

- Create `menu_bar_window.py`: 屏幕判定、窗口几何/状态、摘要格式化、菜单弹出和 AppKit 面板。
- Modify `stock_menubar.py`: 接入替代窗口，复用 `_menu._menu`，删除旧浮动窗口。
- Create `tests/test_menu_bar_fallback.py`: 纯逻辑、状态存储和菜单弹出测试。
- Modify `tests/test_observation_ui_integration.py`: 将浮动窗口摘要断言迁移到替代窗口。
- Delete `tests/test_floating_window.py`: 旧组件不复用，原点击行为由新组件测试覆盖。
- Modify `A股监控使用说明.md`: 记录刘海屏入口行为和浮窗移除。

---

### Task 1: 屏幕适配与窗口状态

**Files:**
- Create: `menu_bar_window.py`
- Test: `tests/test_menu_bar_fallback.py`

**Interfaces:**
- Consumes: 屏幕 frame、visibleFrame、`safeAreaInsets.top`、系统状态栏厚度和 JSON 状态路径。
- Produces: `ScreenGeometry(frame, visible_frame, safe_area_top)`, `should_show_fallback(safe_area_top, menu_bar_thickness=24.0)`, `default_window_origin(screen, window_size=(360.0, 72.0), margin=16.0)`, `clamp_window_origin(origin, screen, window_size=(360.0, 72.0), margin=16.0)`, `load_window_origin(path, screen_key)`, `save_window_origin(path, screen_key, origin)`。

- [ ] **Step 1: Write the failing geometry/state tests**

```python
import json
import tempfile
import unittest
from pathlib import Path

from menu_bar_window import (
    ScreenGeometry,
    clamp_window_origin,
    default_window_origin,
    load_window_origin,
    save_window_origin,
    should_show_fallback,
)


class MenuBarFallbackTests(unittest.TestCase):
    def setUp(self):
        self.screen = ScreenGeometry(
            frame=(0.0, 0.0, 1728.0, 1117.0),
            visible_frame=(0.0, 0.0, 1728.0, 1085.0),
            safe_area_top=37.0,
        )

    def test_notch_safe_area_shows_fallback_but_plain_menu_bar_does_not(self):
        self.assertTrue(should_show_fallback(37.0, 24.0))
        self.assertFalse(should_show_fallback(24.0, 24.0))
        self.assertTrue(should_show_fallback(None, 24.0))
        self.assertTrue(should_show_fallback(float("nan"), 24.0))

    def test_default_origin_uses_top_right_visible_frame(self):
        self.assertEqual(
            default_window_origin(self.screen, (360.0, 72.0), 16.0),
            (1352.0, 997.0),
        )

    def test_clamp_keeps_saved_origin_inside_visible_frame(self):
        self.assertEqual(
            clamp_window_origin((-500.0, 5000.0), self.screen, (360.0, 72.0), 16.0),
            (16.0, 997.0),
        )

    def test_window_origin_round_trip_and_bad_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "window.json"
            save_window_origin(path, "display-1", (100.0, 200.0))
            self.assertEqual(load_window_origin(path, "display-1"), (100.0, 200.0))
            self.assertIsNone(load_window_origin(path, "display-2"))
            path.write_text("{broken", encoding="utf-8")
            self.assertIsNone(load_window_origin(path, "display-1"))
```

- [ ] **Step 2: Run the tests and verify the expected failure**

Run: `python3 -m unittest tests.test_menu_bar_fallback -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'menu_bar_window'`.

- [ ] **Step 3: Implement geometry and state persistence**

Create `menu_bar_window.py` with the exact public API:

```python
from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Optional, Tuple


DEFAULT_WINDOW_SIZE = (360.0, 72.0)
DEFAULT_MARGIN = 16.0


@dataclass(frozen=True)
class ScreenGeometry:
    frame: Tuple[float, float, float, float]
    visible_frame: Tuple[float, float, float, float]
    safe_area_top: Optional[float]


def should_show_fallback(safe_area_top, menu_bar_thickness=24.0):
    if safe_area_top is None:
        return True
    try:
        value = float(safe_area_top)
    except (TypeError, ValueError):
        return True
    if not math.isfinite(value):
        return True
    return value >= float(menu_bar_thickness) + 4.0


def default_window_origin(screen, window_size=DEFAULT_WINDOW_SIZE, margin=DEFAULT_MARGIN):
    x = screen.visible_frame[0] + screen.visible_frame[2] - window_size[0] - margin
    y = screen.visible_frame[1] + screen.visible_frame[3] - window_size[1] - margin
    return (x, y)


def clamp_window_origin(origin, screen, window_size=DEFAULT_WINDOW_SIZE, margin=DEFAULT_MARGIN):
    x, y = (float(origin[0]), float(origin[1]))
    min_x = screen.visible_frame[0] + margin
    max_x = screen.visible_frame[0] + screen.visible_frame[2] - window_size[0] - margin
    min_y = screen.visible_frame[1] + margin
    max_y = screen.visible_frame[1] + screen.visible_frame[3] - window_size[1] - margin
    return (min(max(x, min_x), max_x), min(max(y, min_y), max_y))


def load_window_origin(path, screen_key):
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        value = payload[str(screen_key)]
        origin = (float(value[0]), float(value[1]))
        if not all(math.isfinite(item) for item in origin):
            return None
        return origin
    except (OSError, ValueError, TypeError, KeyError, IndexError, json.JSONDecodeError):
        return None


def save_window_origin(path, screen_key, origin):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {}
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            payload = {}
    payload[str(screen_key)] = [float(origin[0]), float(origin[1])]
    fd, temporary = tempfile.mkstemp(prefix=path.name, dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
```

- [ ] **Step 4: Run the tests and verify they pass**

Run: `python3 -m unittest tests.test_menu_bar_fallback -v`
Expected: PASS.

---

### Task 2: 菜单弹出与悬浮面板

**Files:**
- Modify: `menu_bar_window.py`
- Test: `tests/test_menu_bar_fallback.py`

**Interfaces:**
- Consumes: callable `menu_provider() -> NSMenu`、AppKit window/fake window、菜单根节点。
- Produces: `configure_overlay_window(window)`, `MenuPopupController(menu_provider, presenter)`, `MenuBarFallbackWindow(menu_provider, state_path, on_error=None)`，以及 `show()`, `hide()`, `close()`, `set_summary(text)`, `evaluate_screen(screen=None)`, `show_menu()`。

- [ ] **Step 1: Write failing menu/window behavior tests**

Append to `tests/test_menu_bar_fallback.py`:

```python
from menu_bar_window import MenuPopupController, configure_overlay_window


class MenuPopupTests(unittest.TestCase):
    def test_popup_always_uses_latest_native_menu(self):
        menus = ["first", "second"]
        shown = []
        controller = MenuPopupController(lambda: menus.pop(0), shown.append)
        controller.show()
        controller.show()
        self.assertEqual(shown, ["first", "second"])


class OverlayWindowTests(unittest.TestCase):
    def test_overlay_configuration_enables_fullscreen_and_nonactivation(self):
        calls = []
        window = type("Window", (), {
            "setLevel_": lambda self, value: calls.append(("level", value)),
            "setCollectionBehavior_": lambda self, value: calls.append(("behavior", value)),
            "setMovableByWindowBackground_": lambda self, value: calls.append(("movable", value)),
            "setIgnoresMouseEvents_": lambda self, value: calls.append(("mouse", value)),
            "setReleasedWhenClosed_": lambda self, value: calls.append(("released", value)),
        })()
        configure_overlay_window(window, floating_level=3, behavior=273)
        self.assertEqual(calls, [
            ("level", 3), ("behavior", 273), ("movable", True),
            ("mouse", False), ("released", False),
        ])
```

- [ ] **Step 2: Run the tests and verify the expected failure**

Run: `python3 -m unittest tests.test_menu_bar_fallback.MenuPopupTests tests.test_menu_bar_fallback.OverlayWindowTests -v`
Expected: FAIL because the two public classes/functions do not exist.

- [ ] **Step 3: Implement menu popup and overlay configuration**

Append to `menu_bar_window.py`:

```python
class MenuPopupController:
    def __init__(self, menu_provider, presenter):
        self._menu_provider = menu_provider
        self._presenter = presenter

    def show(self):
        self._presenter(self._menu_provider())


def configure_overlay_window(window, floating_level=3, behavior=273):
    window.setLevel_(floating_level)
    window.setCollectionBehavior_(behavior)
    window.setMovableByWindowBackground_(True)
    window.setIgnoresMouseEvents_(False)
    window.setReleasedWhenClosed_(False)
```

Implement `MenuBarFallbackWindow` as thin AppKit glue:

- `__init__` creates `MenuPopupController(menu_provider, self._present_menu)` and stores `state_path`; `evaluate_screen`/`close` call `load_window_origin`/`save_window_origin` directly.
- `_create_window` creates an `NSPanel` with borderless + nonactivating style mask, 360×72 content, dark rounded background, `NSTextView` summary and a full-size transparent `_MenuBarClickView`.
- `configure_overlay_window` receives `NSFloatingWindowLevel`, `NSWindowCollectionBehaviorCanJoinAllSpaces | NSWindowCollectionBehaviorFullScreenAuxiliary | NSWindowCollectionBehaviorStationary`.
- `_present_menu(menu)` calls `menu.popUpMenuPositioningItem_atLocation_inView_(None, point, content_view)` on the main thread.
- `evaluate_screen` reads `safeAreaInsets().top`, `NSStatusBar.systemStatusBar().thickness()`, frame and visibleFrame; it calls `should_show_fallback`, then restores saved origin or `default_window_origin`, clamps it and shows/hides the panel.
- `set_summary` updates the text view and resets its frame to the current content height.
- `show`, `hide`, and `close` only change AppKit window order. `close` saves the origin first.
- AppKit import/create errors are passed to `on_error` and return a disabled object whose methods do not raise.

- [ ] **Step 4: Run focused tests and compile**

Run: `python3 -m unittest tests.test_menu_bar_fallback -v`
Expected: PASS.

Run: `PYTHONPYCACHEPREFIX=/tmp/stock-monitor-pycache python3 -m py_compile menu_bar_window.py`
Expected: exit 0.

---

### Task 3: StockMenuBar 集成与旧浮窗移除

**Files:**
- Modify: `stock_menubar.py`
- Modify: `tests/test_observation_ui_integration.py`
- Delete: `tests/test_floating_window.py`

**Interfaces:**
- Consumes: `MenuBarFallbackWindow(menu_provider=lambda: self._menu._menu, state_path=os.path.join(self.log_dir, "menu_bar_window.json"), on_error=self.log)`。
- Produces: `StockMenuBar._update_menu_bar_fallback(menu)`，内部调用 `set_summary(build_summary(menu, self._market_turnover_lines()))`。

- [ ] **Step 1: Write failing integration tests**

Replace the old floating assertion in `tests/test_observation_ui_integration.py` with:

```python
def test_menu_bar_fallback_has_compact_count_and_no_stale_menu_snapshot(self):
    self.app.menu_bar_fallback = Mock()
    self.app._update_menu_bar_fallback({
        "indicator": {"items": self.titles(), "buy_count": 1},
        "index": {"items": [{"name": "上证", "price": 3000.0, "pct": 1.2}]},
        "stock": {"items": [{"name": "样本", "price": 10.0, "pct": -0.5}]},
    })
    text = self.app.menu_bar_fallback.set_summary.call_args.args[0]
    self.assertIn("今日曾提醒1只", text)
    self.assertIn("上证", text)
    self.assertNotIn("菜单快照", text)
```

Add `tests/test_menu_bar_integration.py`:

```python
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from menu_bar_window import native_menu_root


class MenuBarIntegrationTests(unittest.TestCase):
    def test_native_menu_root_reuses_current_rumps_menu(self):
        native = object()
        app = SimpleNamespace(_menu=SimpleNamespace(_menu=native))
        self.assertIs(native_menu_root(app), native)

    def test_menu_error_keeps_window_disabled_without_raising(self):
        errors = []
        window = Mock()
        window.show.side_effect = RuntimeError("AppKit unavailable")
        from menu_bar_window import safe_show
        self.assertFalse(safe_show(window, errors.append))
        self.assertEqual(len(errors), 1)
```

- [ ] **Step 2: Run tests and verify the expected failures**

Run: `python3 -m unittest tests.test_menu_bar_integration tests.test_observation_ui_integration -v`
Expected: FAIL because `native_menu_root`/`safe_show` and `_update_menu_bar_fallback` do not exist.

- [ ] **Step 3: Add menu helpers and replace floating integration**

Add to `menu_bar_window.py`:

```python
def native_menu_root(app):
    return app._menu._menu


def safe_show(window, on_error):
    try:
        window.show()
        return True
    except Exception as error:
        if on_error:
            on_error(error)
        return False


def build_summary(menu, turnover_lines=()):
    lines = ["📈 A股实时监控", "─────────────"]
    buy_count = menu.get("indicator", {}).get("buy_count", 0)
    if buy_count:
        lines.append(f"📋 今日曾提醒{buy_count}只（非当前买点）")
    rows = menu.get("index", {}).get("items", []) + menu.get("stock", {}).get("items", [])
    if not rows:
        lines.append("暂无行情数据 · 等待刷新")
    for item in rows:
        arrow = "▲" if item.get("pct", 0) > 0 else "▼" if item.get("pct", 0) < 0 else "─"
        lines.append(f"{item.get('name', '')} {item.get('price', 0):.2f} {arrow}{abs(item.get('pct', 0)):.2f}%")
    lines.extend(turnover_lines)
    return "\n".join(lines)
```

In `stock_menubar.py`:

- Import `MenuBarFallbackWindow`, `build_summary`, and `native_menu_root`.
- Delete `FloatingWindow`, `_ClickView`, `_FWTableViewDelegate`, `_FWDataSource`, and all old floating constants.
- Replace `self.floating_window = FloatingWindow(...)` with `self.menu_bar_fallback = MenuBarFallbackWindow(...)`.
- Remove `_float_menu_item`, `_toggle_floating`, `_on_floating_closed`, `_on_floating_mode_change`, `_update_float_menu_title`, and the floating menu entry.
- Replace `_update_floating_window(menu)` with `_update_menu_bar_fallback(menu)` calling `self.menu_bar_fallback.set_summary(build_summary(menu, self._market_turnover_lines()))`.
- Rename the old refresh error label from “浮动窗口更新失败” to “菜单替代窗口更新失败”.
- Call `self.menu_bar_fallback.evaluate_screen()` after menu construction. Register one retained observer with:

```python
from AppKit import NSApplicationDidChangeScreenParametersNotification
from Foundation import NSNotificationCenter

self._screen_observer = NSNotificationCenter.defaultCenter().addObserverForName_object_queue_usingBlock_(
    NSApplicationDidChangeScreenParametersNotification,
    None,
    None,
    lambda _note: self._dispatch_to_main(self.menu_bar_fallback.evaluate_screen),
)
```

- In `quit_app`, remove that observer through `NSNotificationCenter.defaultCenter().removeObserver_(self._screen_observer)` before closing the fallback window.
- `quit_app` closes `self.menu_bar_fallback` instead of the floating window.

- [ ] **Step 4: Run focused integration tests**

Run: `python3 -m unittest tests.test_menu_bar_integration tests.test_observation_ui_integration tests.test_stock_menubar_import -v`
Expected: PASS.

---

### Task 4: 文档与全量验证

**Files:**
- Modify: `A股监控使用说明.md`
- Modify: `docs/2026-10-23-notch-menu-window-design.md` only if runtime behavior differs from the approved design.

**Interfaces:**
- Consumes: 已完成的替代窗口行为和现有刷新/提醒功能。
- Produces: 用户文档和最终验证结果。

- [ ] **Step 1: Update user documentation**

在 `A股监控使用说明.md` 增加“刘海屏菜单入口”：

```markdown
### 刘海屏菜单入口
- 普通屏幕仍点击右上角系统菜单栏使用完整功能。
- 启动检测到刘海可能遮挡菜单栏时，自动在屏幕右上角安全区域显示替代窗口。
- 点击替代窗口弹出与系统菜单栏一致的完整菜单；拖动窗口后会记住显示器内位置。
- Safari/Chrome 普通窗口和全屏模式下替代窗口仍可见。菜单收起不影响盘中刷新、提醒和通知。
- 原“浮动窗口”已由替代窗口取代，不再单独启动或显示。
```

- [ ] **Step 2: Run full verification**

Run: `python3 -m unittest discover -s tests`
Expected: all tests PASS；仅保留当前原本就跳过的测试。

Run: `PYTHONPYCACHEPREFIX=/tmp/stock-monitor-pycache python3 -m py_compile *.py`
Expected: exit 0。

Run: `git diff --check`
Expected: no whitespace errors。

- [ ] **Step 3: Review acceptance criteria**

逐项确认：

- 普通屏使用原系统菜单栏。
- 刘海风险屏自动显示右上角替代窗口。
- 窗口点击弹出完整动态 `rumps` 菜单且 callback 原样执行。
- 浏览器全屏和所有桌面空间中窗口可见。
- 后台刷新、提醒、通知、盘中策略和 T+1 不变。
- 旧浮动窗口、菜单入口、更新逻辑和测试均不存在。
- 屏幕检测/AppKit/菜单异常不使后台监控退出。
- 不创建 commit 或 branch。

## Execution Handoff

用户已要求直接在当前目录实施且不使用隔离区，因此采用 Inline Execution。使用 `superpowers:executing-plans`，每个任务完成后运行对应测试；全量验证通过后使用 `superpowers:verification-before-completion` 再报告完成。
