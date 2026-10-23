#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import tempfile
from typing import Mapping, Optional, Tuple


DEFAULT_WINDOW_SIZE = (460.0, 137.5)
DEFAULT_MARGIN = 16.0


@dataclass(frozen=True)
class WindowLayout:
    width: float
    height: float
    header_height: float
    corner_radius: float
    accent_width: float
    body_x: float
    body_y: float
    body_gap: float
    body_width: float
    body_height: float


def build_window_layout(
    width=460.0,
    height=137.5,
    header_height=32.0,
    corner_radius=16.0,
):
    body_x = 18.0
    body_y = 14.0
    body_gap = 12.0
    body_height = height - header_height - body_y - body_gap
    return WindowLayout(
        width=float(width),
        height=float(height),
        header_height=float(header_height),
        corner_radius=float(corner_radius),
        accent_width=4.0,
        body_x=body_x,
        body_y=body_y,
        body_gap=body_gap,
        body_width=float(width) - body_x * 2,
        body_height=body_height,
    )


def order_main_menu(entries: Mapping[str, object]):
    order = (
        "market", "watchlist", None,
        "top_sector", "bottom_sector", "concept", "limit",
        "selection", "technical", "fundamentals", "global", "ai",
        "portfolio_info", "portfolio_settings", "sector_leaders",
        "trade_calculator", "target_cost",
        "summary", "headlines", "settings",
        None, "add", "remove", "refresh", None, "quit",
    )
    return [None if key is None else entries[key] for key in order]


def resolve_close_action(safe_area_top, menu_bar_thickness=24.0):
    return (
        "restore_menu_bar"
        if not should_show_fallback(safe_area_top, menu_bar_thickness)
        else "close_window"
    )


def build_watchlist_quotes(watchlist, positions, stock_data, include_missing=False):
    rows = {}
    for group, items in (("自选股", watchlist), ("持仓股", positions)):
        for item in items:
            code = str(item.get("code") or "").strip()
            if not code:
                continue
            data = stock_data.get(code)
            if not data and not include_missing:
                continue
            row = rows.setdefault(code, {
                "code": code,
                "name": item.get("name") or code,
                "price": data.get("price", 0) if data else 0,
                "pct": data.get("change_pct", 0) if data else 0,
                "available": bool(data),
                "groups": [],
            })
            if group not in row["groups"]:
                row["groups"].append(group)
    return list(rows.values())


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


class ClickGestureTracker:
    """区分单击与拖动，拖动窗口不得顺带触发菜单或关闭动作。"""

    def __init__(self, threshold=5.0):
        self.threshold = float(threshold)
        self._start = None
        self._dragged = False

    def begin(self, location):
        self._start = (float(location[0]), float(location[1]))
        self._dragged = False

    def drag(self, location):
        if self._start is None:
            return
        x, y = float(location[0]), float(location[1])
        if math.hypot(x - self._start[0], y - self._start[1]) > self.threshold:
            self._dragged = True

    def end(self, location):
        if self._start is None:
            return False
        x, y = float(location[0]), float(location[1])
        moved = math.hypot(x - self._start[0], y - self._start[1])
        clicked = not self._dragged and moved <= self.threshold
        self._start = None
        self._dragged = False
        return clicked


class ClickGestureDispatcher:
    """只把无位移的完整单击交给动作，拖动仅用于移动窗口。"""

    def __init__(self, callback, threshold=5.0):
        self._callback = callback
        self._gesture = ClickGestureTracker(threshold=threshold)

    def set_callback(self, callback):
        self._callback = callback

    def mouse_down(self, location):
        self._gesture.begin(location)

    def mouse_drag(self, location):
        self._gesture.drag(location)

    def mouse_up(self, location):
        if self._gesture.end(location):
            self._callback()


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
    indicator = menu.get("indicator", {})
    buy_count = int(indicator.get("buy_count", 0) or 0)
    ready_count = int(indicator.get("ready_count", 0) or 0)
    lines = [f"📋 今日曾提醒{buy_count}  🟡 候选{ready_count}"]

    rows = (
        menu.get("watchlist", {}).get("items", [])
        + menu.get("index", {}).get("items", [])
        + menu.get("stock", {}).get("items", [])
    )
    available_rows = [item for item in rows if item.get("available", True)]
    if available_rows:
        rows = available_rows
    unique_rows = []
    seen = set()
    for item in rows:
        key = (item.get("name", ""), item.get("price"))
        if key in seen:
            continue
        seen.add(key)
        unique_rows.append(item)

    if not unique_rows:
        lines.append("暂无行情数据 · 等待刷新")
    for start in range(0, min(len(unique_rows), 6), 2):
        cells = []
        for item in unique_rows[start:start + 2]:
            pct = item.get("pct", 0)
            arrow = "▲" if pct > 0 else "▼" if pct < 0 else "─"
            cells.append(
                f"{item.get('name', '')} {item.get('price', 0):.2f} {arrow}{abs(pct):.2f}%"
            )
        lines.append("  |  ".join(cells))
    if len(unique_rows) > 6:
        lines.append(f"其余{len(unique_rows) - 6}只见菜单")

    if turnover_lines:
        lines.append(" · ".join(str(line) for line in turnover_lines[:2]))
    return "\n".join(lines)


class MenuBarFallbackWindow:
    WIDTH = build_window_layout().width
    HEIGHT = build_window_layout().height
    OVERLAY_BEHAVIOR = 273

    def __init__(self, menu_provider, state_path, on_error=None):
        self._menu_provider = menu_provider
        self._state_path = Path(state_path)
        self._on_error = on_error
        self._window = None
        self._text_view = None
        self._content_view = None
        self._screen_key = "main"
        self._screen = None
        self._disabled_reason = None
        self._dismissed = False
        self._popup = MenuPopupController(menu_provider, self._present_menu)
        try:
            self._create_window()
        except Exception as error:
            self._disabled_reason = str(error)
            self._report_error(error)

    @property
    def enabled(self):
        return self._window is not None and self._disabled_reason is None

    def _report_error(self, error):
        if self._on_error:
            try:
                self._on_error(error)
            except Exception:
                pass

    def _create_window(self):
        import objc
        from AppKit import (
            NSBackingStoreBuffered,
            NSBorderlessWindowMask,
            NSColor,
            NSFloatingWindowLevel,
            NSFont,
            NSMakeRect,
            NSNonactivatingPanelMask,
            NSPanel,
            NSTextField,
            NSTextView,
            NSView,
            NSVisualEffectView,
        )

        owner = self

        class MenuBarClickView(NSView):
            def initWithFrame_(self, frame):
                self = objc.super(MenuBarClickView, self).initWithFrame_(frame)
                if self is None:
                    return None
                self._owner = owner
                self._gesture = ClickGestureDispatcher(owner.show_menu)
                return self

            def set_callback(self, callback):
                self._gesture.set_callback(callback)

            def acceptsFirstMouse_(self, event):
                return True

            @objc.typedSelector(b"v@:@")
            def mouseDown_(self, event):
                point = event.locationInWindow()
                self._gesture.mouse_down((point.x, point.y))

            @objc.typedSelector(b"v@:@")
            def mouseDragged_(self, event):
                point = event.locationInWindow()
                self._gesture.mouse_drag((point.x, point.y))

            @objc.typedSelector(b"v@:@")
            def mouseUp_(self, event):
                point = event.locationInWindow()
                self._gesture.mouse_up((point.x, point.y))

        self._window = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, self.WIDTH, self.HEIGHT),
            NSBorderlessWindowMask | NSNonactivatingPanelMask,
            NSBackingStoreBuffered,
            False,
        )
        configure_overlay_window(
            self._window,
            floating_level=NSFloatingWindowLevel,
            behavior=self.OVERLAY_BEHAVIOR,
        )
        self._window.setOpaque_(False)
        self._window.setBackgroundColor_(NSColor.clearColor())
        self._window.setHasShadow_(True)
        self._window.setBecomesKeyOnlyIfNeeded_(True)
        self._window.setHidesOnDeactivate_(False)

        self._content_view = self._window.contentView()
        self._content_view.setWantsLayer_(True)
        layout = build_window_layout(self.WIDTH, self.HEIGHT)
        self._content_view.layer().setCornerRadius_(layout.corner_radius)
        self._content_view.layer().setMasksToBounds_(True)
        self._content_view.layer().setBackgroundColor_(
            NSColor.clearColor().CGColor()
        )

        background = NSVisualEffectView.alloc().initWithFrame_(
            NSMakeRect(0, 0, self.WIDTH, self.HEIGHT)
        )
        background.setMaterial_(5)
        background.setBlendingMode_(0)
        background.setState_(1)
        self._content_view.addSubview_(background)

        self._accent_view = NSView.alloc().initWithFrame_(
            NSMakeRect(0, 0, layout.accent_width, self.HEIGHT)
        )
        self._accent_view.setWantsLayer_(True)
        self._accent_view.layer().setBackgroundColor_(
            NSColor.colorWithRed_green_blue_alpha_(0.24, 0.55, 0.96, 0.95).CGColor()
        )
        self._content_view.addSubview_(self._accent_view)

        self._header_view = NSView.alloc().initWithFrame_(
            NSMakeRect(0, self.HEIGHT - layout.header_height, self.WIDTH, layout.header_height)
        )
        self._header_view.setWantsLayer_(True)
        self._header_view.layer().setBackgroundColor_(
            NSColor.colorWithWhite_alpha_(0.92, 0.10).CGColor()
        )
        self._content_view.addSubview_(self._header_view)

        self._title_label = NSTextField.alloc().initWithFrame_(
            NSMakeRect(18, 7, 220, 18)
        )
        self._title_label.setStringValue_("📈 A股实时监控")
        self._title_label.setBezeled_(False)
        self._title_label.setBordered_(False)
        self._title_label.setDrawsBackground_(False)
        self._title_label.setEditable_(False)
        self._title_label.setSelectable_(False)
        self._title_label.setFont_(NSFont.systemFontOfSize_weight_(13.0, 0.2))
        self._title_label.setTextColor_(NSColor.colorWithWhite_alpha_(0.96, 1.0))
        self._header_view.addSubview_(self._title_label)

        self._text_view = NSTextView.alloc().initWithFrame_(
            NSMakeRect(layout.body_x, layout.body_y, layout.body_width, layout.body_height)
        )
        self._text_view.setEditable_(False)
        self._text_view.setSelectable_(False)
        self._text_view.setDrawsBackground_(False)
        self._text_view.setFont_(NSFont.monospacedSystemFontOfSize_weight_(11.2, 0))
        self._text_view.setTextColor_(NSColor.colorWithWhite_alpha_(0.92, 1.0))
        self._text_view.setVerticallyResizable_(False)
        self._text_view.setHorizontallyResizable_(False)
        self._text_view.setTextContainerInset_((0.0, 0.0))
        self._content_view.addSubview_(self._text_view)

        click_view = MenuBarClickView.alloc().initWithFrame_(
            NSMakeRect(0, 0, self.WIDTH, self.HEIGHT)
        )
        self._content_view.addSubview_(click_view)

        close_click = MenuBarClickView.alloc().initWithFrame_(
            NSMakeRect(self.WIDTH - 34, self.HEIGHT - 28, 20, 20)
        )
        close_click.setWantsLayer_(True)
        close_click.layer().setCornerRadius_(10.0)
        close_click.layer().setBackgroundColor_(
            NSColor.colorWithWhite_alpha_(0.92, 0.92).CGColor()
        )
        close_click.layer().setBorderColor_(
            NSColor.colorWithWhite_alpha_(0.52, 0.36).CGColor()
        )
        close_click.layer().setBorderWidth_(1.0)
        close_click.layer().setShadowColor_(
            NSColor.colorWithWhite_alpha_(0.0, 0.30).CGColor()
        )
        close_click.layer().setShadowOpacity_(0.24)
        close_click.layer().setShadowRadius_(3.5)
        close_click.layer().setShadowOffset_((0.0, -1.0))
        close_click.set_callback(self._on_close_click)
        self._content_view.addSubview_(close_click)

    def set_summary(self, text):
        if not self.enabled:
            return
        self._text_view.setString_(str(text or "等待数据..."))

    def _on_close_click(self):
        geometry, menu_bar_thickness = self._screen_details()
        action = resolve_close_action(geometry.safe_area_top, menu_bar_thickness)
        self._dismissed = action == "close_window"
        self.hide()

    def _screen_details(self, screen=None):
        from AppKit import NSStatusBar

        screen = screen or self._screen
        if screen is None:
            from AppKit import NSScreen
            screen = NSScreen.mainScreen()
        if screen is None:
            raise RuntimeError("没有可用显示器")
        self._screen = screen
        frame_rect = screen.frame()
        visible_rect = screen.visibleFrame()
        try:
            safe_area_top = float(screen.safeAreaInsets().top)
        except Exception:
            safe_area_top = None
        screen_key = "main"
        try:
            screen_key = str(screen.deviceDescription().get("NSScreenNumber", "main"))
        except Exception:
            pass
        self._screen_key = screen_key
        return (
            ScreenGeometry(
                frame=(frame_rect.origin.x, frame_rect.origin.y, frame_rect.size.width, frame_rect.size.height),
                visible_frame=(
                    visible_rect.origin.x,
                    visible_rect.origin.y,
                    visible_rect.size.width,
                    visible_rect.size.height,
                ),
                safe_area_top=safe_area_top,
            ),
            float(NSStatusBar.systemStatusBar().thickness()),
        )

    def evaluate_screen(self, screen=None):
        if not self.enabled:
            return False
        try:
            geometry, menu_bar_thickness = self._screen_details(screen)
            required = should_show_fallback(geometry.safe_area_top, menu_bar_thickness)
            origin = load_window_origin(self._state_path, self._screen_key)
            if origin is None:
                origin = default_window_origin(geometry, (self.WIDTH, self.HEIGHT))
            origin = clamp_window_origin(origin, geometry, (self.WIDTH, self.HEIGHT))
            self._window.setFrameOrigin_(origin)
            if required and not self._dismissed:
                self.show()
            else:
                self.hide()
            return required
        except Exception as error:
            self._report_error(error)
            self.show()
            return True

    def show_menu(self):
        if not self.enabled:
            return False
        try:
            self._popup.show()
            return True
        except Exception as error:
            self._report_error(error)
            return False

    def _present_menu(self, menu):
        point = (0.0, self.HEIGHT)
        menu.popUpMenuPositioningItem_atLocation_inView_(None, point, self._content_view)

    def show(self):
        if self.enabled:
            self._dismissed = False
            self._window.orderFrontRegardless()

    def hide(self):
        if self.enabled:
            self._window.orderOut_(None)

    def close(self):
        if not self.enabled:
            return
        try:
            origin = self._window.frame().origin
            save_window_origin(self._state_path, self._screen_key, (origin.x, origin.y))
        except Exception as error:
            self._report_error(error)
        self._window.close()


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
