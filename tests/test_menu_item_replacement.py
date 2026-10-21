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


if __name__ == "__main__":
    unittest.main()
