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
    # 容器项（callback=None、靠子菜单开门，如「📦 持仓信息」）必须把旧子菜单搬过来：
    # 不搬的话新原生项既无 action 也无 submenu，AppKit autoenablesItems 直接置灰、
    # 悬停也拉不出子菜单（2026-10-22 用户实盘反馈「持仓信息」被置灰）。
    # setSubmenu_ 会顺带把 action 设回 submenuAction:，项恢复可点开。
    old_submenu = native_item.submenu() if hasattr(native_item, "submenu") else None
    if old_submenu is not None:
        replacement._menuitem.setSubmenu_(old_submenu)
    item._menuitem = replacement._menuitem
    return item
