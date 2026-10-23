#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""告警对话框回归测试。

背景：com.apple.python3 的 timeSensitiveSetting=0，TimeSensitive 中断等级
会被系统降级；专注模式下系统通知只进通知列表、不弹横幅。2026-10-08 大盘
跌 3.7% 因此全程零横幅。告警对话框（NSAlert「知道了」）是与通知
系统完全解耦的兜底通道。

2026-10-22 起取代置顶横幅卡片（深红/橙棕圆角卡片废弃）：danger/crash 与
观察类打断统一用对话框，级别写进标题（【危险提醒】【系统性风险】等）。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from stock_menubar import StockMenuBar


class AlertDialogTests(unittest.TestCase):
    def test_level_labels_cover_all_levels(self):
        for level in ('crash', 'danger', 'warning', 'info'):
            self.assertTrue(
                StockMenuBar._LEVEL_LABELS.get(level),
                f"缺少 {level} 级对话框标签",
            )

    def test_dialog_is_modal_with_level_and_main_thread(self):
        import inspect
        src = inspect.getsource(StockMenuBar._show_alert_dialog)
        self.assertIn('NSAlert', src, "缺少模态对话框")
        self.assertIn('runModal', src, "对话框必须模态（强制确认）")
        self.assertIn("addButtonWithTitle_('知道了')", src, "缺统一按钮文案")
        self.assertIn('【{label}】', src, "对话框标题未带级别前缀")
        self.assertIn('_run_on_main_once', src, "对话框没切回主线程（会触发退出码133）")

    def test_raise_urgency_uses_dialog_not_popup_card(self):
        import inspect
        src = inspect.getsource(StockMenuBar._raise_urgency)
        self.assertIn('_show_alert_dialog', src)
        self.assertNotIn('alert_popup', src, "置顶横幅卡片已废弃")
        self.assertNotIn('popup.show', src, "置顶横幅卡片已废弃")
        self.assertLess(
            src.index('_show_alert_dialog'), src.index('afplay'),
            "对话框必须排在音效之前（它是主通道）",
        )

    def test_urgency_dialog_is_throttled(self):
        import inspect
        src = inspect.getsource(StockMenuBar._raise_urgency)
        self.assertIn('_popup_should_show', src, "对话框缺 180s 冷却/级别抬升节流")

    def test_app_no_longer_wires_popup_card(self):
        import inspect
        src = inspect.getsource(StockMenuBar.__init__)
        self.assertNotIn('AlertPopup', src, "不应再实例化置顶横幅")


if __name__ == '__main__':
    unittest.main()
