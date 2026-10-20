import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import stock_menubar as sm


@unittest.skipUnless(
    os.environ.get('STOCK_TEST_APPKIT') == '1' and sm._HAS_OBJC,
    '需要显式启用 macOS GUI 测试：STOCK_TEST_APPKIT=1',
)
class FloatingWindowClickTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sm._get_ns_app().setActivationPolicy_(1)

    def setUp(self):
        self.closed = Mock()
        self.floating = sm.FloatingWindow(on_close=self.closed)
        self.floating._window.setReleasedWhenClosed_(False)
        self.floating.set_compact('上证 3200\n点击展开功能')
        self.floating.set_menu_data({'indicator': {
            'title': '指标策略',
            'items': [{'name': '测试买点', 'value': '10.00', 'color': 'red'}],
        }})

    def tearDown(self):
        self.floating.close()

    def click_at(self, point):
        target = self.floating._window.contentView().hitTest_(point)
        self.assertIsInstance(target, sm._ClickView)
        event = SimpleNamespace(locationInWindow=lambda: SimpleNamespace(x=point[0], y=point[1]))
        target.mouseDown_(event)
        target.mouseUp_(event)

    def select_row(self, row):
        from Foundation import NSIndexSet
        self.floating._table_view.selectRowIndexes_byExtendingSelection_(
            NSIndexSet.indexSetWithIndex_(row), False,
        )

    def test_clicking_compact_text_expands_menu(self):
        self.click_at((50, 50))
        self.assertTrue(self.floating.expanded)
        self.assertTrue(self.floating._click_view.isHidden())
        self.assertFalse(self.floating._scroll_view.isHidden())

    def test_overlay_accepts_first_click_in_inactive_window(self):
        self.assertTrue(self.floating._click_view.acceptsFirstMouse_(None))
        self.assertTrue(self.floating._close_click.acceptsFirstMouse_(None))

    def test_navigation_back_and_collapse_after_refresh(self):
        self.floating.toggle_mode()
        self.select_row(2)
        self.assertEqual(self.floating._menu_path, ['indicator'])
        self.floating.set_menu_data(self.floating._menu_data)
        self.select_row(0)
        self.assertEqual(self.floating._menu_path, [])
        self.select_row(0)
        self.assertFalse(self.floating.expanded)
        self.click_at((50, 50))
        self.assertTrue(self.floating.expanded)

    def test_close_control_stays_above_compact_and_menu_content(self):
        for expanded in (False, True):
            with self.subTest(expanded=expanded):
                if expanded:
                    self.floating.toggle_mode()
                self.floating._visible = True
                frame = self.floating._close_click.frame()
                self.click_at((frame.origin.x + 12, frame.origin.y + 12))
                self.assertFalse(self.floating.visible)
        self.assertEqual(self.closed.call_count, 2)
