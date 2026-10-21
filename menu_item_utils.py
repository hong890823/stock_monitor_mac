#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AppKit 菜单项安全替换工具。"""

from __future__ import annotations

import rumps


def replace_menu_item_title(item, title):
    """Replace a rumps menu item's native object while preserving its wrapper."""
    native_item = getattr(item, "_menuitem", None)
    if native_item is None:
        item.title = str(title)
        return item

    replacement = rumps.MenuItem(
        str(title),
        callback=getattr(item, "callback", None),
        key=getattr(item, "key", None),
        icon=getattr(item, "icon", None),
        template=getattr(item, "_template", False),
    )
    menu = native_item.menu() if hasattr(native_item, "menu") else None
    if menu is not None:
        for index in range(menu.numberOfItems()):
            if menu.itemAtIndex_(index) is native_item:
                menu.removeItemAtIndex_(index)
                menu.insertItem_atIndex_(replacement._menuitem, index)
                break
    item._menuitem = replacement._menuitem
    return item
