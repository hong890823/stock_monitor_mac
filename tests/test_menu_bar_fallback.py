#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import inspect
import tempfile
import unittest
from pathlib import Path

import menu_bar_window
from menu_bar_window import (
    MenuBarFallbackWindow,
    MenuPopupController,
    ScreenGeometry,
    clamp_window_origin,
    configure_overlay_window,
    default_window_origin,
    build_window_layout,
    resolve_close_action,
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

    def test_window_layout_reserves_polished_header_and_compact_body(self):
        layout = build_window_layout()
        self.assertEqual((layout.width, layout.height), (460.0, 137.5))
        self.assertEqual(layout.header_height, 32.0)
        self.assertEqual(layout.corner_radius, 16.0)
        self.assertEqual(layout.body_gap, 12.0)
        self.assertGreaterEqual(layout.body_height, 75.0)
        self.assertEqual(layout.body_x, 18.0)

    def test_window_header_uses_clean_close_control_without_menu_hint(self):
        source = inspect.getsource(MenuBarFallbackWindow._create_window)
        self.assertNotIn("点击展开菜单", source)
        self.assertNotIn("setStringValue_(\"×\")", source)
        self.assertIn("setCornerRadius_(10.0)", source)
        self.assertIn("colorWithWhite_alpha_(0.92, 0.92)", source)

    def test_close_restores_native_menu_bar_when_notch_does_not_require_fallback(self):
        self.assertEqual(resolve_close_action(24.0, 24.0), "restore_menu_bar")
        self.assertEqual(resolve_close_action(37.0, 24.0), "close_window")

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


class MenuPopupTests(unittest.TestCase):
    def test_popup_always_uses_latest_native_menu(self):
        menus = ["first", "second"]
        shown = []
        controller = MenuPopupController(lambda: menus.pop(0), shown.append)
        controller.show()
        controller.show()
        self.assertEqual(shown, ["first", "second"])


class ClickGestureTests(unittest.TestCase):
    def test_dragging_window_does_not_open_menu(self):
        dispatcher_type = getattr(menu_bar_window, "ClickGestureDispatcher", None)
        self.assertIsNotNone(dispatcher_type, "缺少点击/拖拽动作分发器")

        calls = []
        dispatcher = dispatcher_type(lambda: calls.append("menu"))
        dispatcher.mouse_down((100.0, 100.0))
        dispatcher.mouse_drag((180.0, 100.0))
        dispatcher.mouse_up((220.0, 100.0))
        self.assertEqual(calls, [])

    def test_click_opens_menu_immediately_on_mouse_up(self):
        dispatcher_type = getattr(menu_bar_window, "ClickGestureDispatcher", None)
        self.assertIsNotNone(dispatcher_type, "缺少点击/拖拽动作分发器")

        calls = []
        dispatcher = dispatcher_type(lambda: calls.append("menu"))
        dispatcher.mouse_down((100.0, 100.0))
        dispatcher.mouse_up((101.0, 101.0))
        self.assertEqual(calls, ["menu"])

    def test_close_control_can_rebind_click_action(self):
        dispatcher_type = getattr(menu_bar_window, "ClickGestureDispatcher", None)
        self.assertIsNotNone(dispatcher_type, "缺少点击/拖拽动作分发器")

        calls = []
        dispatcher = dispatcher_type(lambda: calls.append("menu"))
        self.assertTrue(
            hasattr(dispatcher, "set_callback"),
            "点击动作不支持关闭按钮重新绑定",
        )
        dispatcher.set_callback(lambda: calls.append("close"))
        dispatcher.mouse_down((100.0, 100.0))
        dispatcher.mouse_up((100.0, 100.0))
        self.assertEqual(calls, ["close"])

    def test_drag_return_to_start_stays_a_drag(self):
        dispatcher_type = getattr(menu_bar_window, "ClickGestureDispatcher", None)
        self.assertIsNotNone(dispatcher_type, "缺少点击/拖拽动作分发器")

        calls = []
        dispatcher = dispatcher_type(lambda: calls.append("menu"))
        dispatcher.mouse_down((100.0, 100.0))
        dispatcher.mouse_drag((180.0, 100.0))
        dispatcher.mouse_up((100.0, 100.0))
        self.assertEqual(calls, [])


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


@unittest.skipUnless(
    os.environ.get("STOCK_TEST_APPKIT") == "1",
    "需要显式启用 macOS GUI 测试：STOCK_TEST_APPKIT=1",
)
class MenuBarFallbackWindowAppKitTests(unittest.TestCase):
    def test_panel_initializes_and_uses_safe_screen_geometry(self):
        from AppKit import NSMenu

        with tempfile.TemporaryDirectory() as directory:
            errors = []
            menu = NSMenu.alloc().init()
            window = MenuBarFallbackWindow(
                lambda: menu,
                Path(directory) / "window.json",
                errors.append,
            )
            try:
                self.assertTrue(window.enabled)
                window.set_summary("上证 3000.00 ▲1.20%")
                self.assertIsInstance(window.evaluate_screen(), bool)
                self.assertTrue(window._content_view.subviews()[-1].acceptsFirstMouse_(None))
                self.assertEqual(errors, [])
            finally:
                window.close()


if __name__ == "__main__":
    unittest.main()
