#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import unittest

from AppKit import NSMenu
import rumps

from menu_item_utils import replace_menu_item_title


class ReplaceMenuItemTitleTests(unittest.TestCase):
    def test_replaces_native_item_without_mutating_title(self):
        item = rumps.MenuItem("old", callback=None)
        menu = NSMenu.alloc().init()
        menu.addItem_(item._menuitem)

        replace_menu_item_title(item, "new")

        self.assertEqual(item.title, "new")
        self.assertEqual(menu.numberOfItems(), 1)
        self.assertEqual(menu.itemAtIndex_(0).title(), "new")

    def test_unattached_wrapper_still_uses_new_native_item(self):
        item = rumps.MenuItem("old", callback=None)

        replace_menu_item_title(item, "new")

        self.assertEqual(item.title, "new")

    def test_container_item_keeps_submenu_after_replacement(self):
        """「📦 持仓信息」置灰回归：换新原生项时子菜单必须搬过去。

        容器项 callback=None，可点开全靠子菜单触发的 submenuAction:；
        旧实现换完 action/submenu 双丢，AppKit 直接置灰。
        """
        item = rumps.MenuItem("📦 持仓信息", callback=None)
        item.add(rumps.MenuItem("📄 打开完整报告", callback=lambda _s: None))
        menu = NSMenu.alloc().init()
        menu.addItem_(item._menuitem)

        replace_menu_item_title(item, "📦 持仓信息 · 今日 -1.2%")

        native = item._menuitem
        self.assertIsNotNone(native.submenu(), "子菜单未随替换迁移")
        self.assertEqual(str(native.action()), "submenuAction:")
        self.assertEqual(native.submenu().numberOfItems(), 1)
        self.assertEqual(menu.numberOfItems(), 1)
        self.assertIs(menu.itemAtIndex_(0), native)

        # 替换后继续 clear + add（_update_daily_report_menu 的实际路径），
        # 新子项必须落在挂在新原生项上的那个子菜单里。
        item.clear()
        item.add(rumps.MenuItem("📄 打开完整报告", callback=lambda _s: None))
        self.assertIs(native.submenu(), item._menu)
        self.assertEqual(native.submenu().numberOfItems(), 1)


if __name__ == "__main__":
    unittest.main()
