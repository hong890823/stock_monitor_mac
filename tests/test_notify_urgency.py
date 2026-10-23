#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通知层回归测试。

覆盖 2026-10-08 的故障：通知进了通知中心，但没有横幅/提醒弹出。
根因是 Active 级通知在专注模式下被静默降级，只落进列表。
"""
import os
import sys
import time
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

# 只取静态方法做验证，不启动 rumps 应用
from stock_menubar import StockMenuBar


class _FakeDockApp:
    def __init__(self):
        self.policies = []
        self.attention_requests = []

    def setActivationPolicy_(self, policy):
        self.policies.append(int(policy))

    def activateIgnoringOtherApps_(self, _flag):
        raise AssertionError('Dock 弹跳不应抢占用户当前焦点')

    def requestUserAttention_(self, request_type):
        current_policy = self.policies[-1] if self.policies else None
        self.attention_requests.append((current_policy, request_type))


class DockAttentionTests(unittest.TestCase):
    def test_background_dispatch_uses_appkit_main_queue(self):
        import stock_menubar as sm

        subject = object.__new__(StockMenuBar)
        subject.log = lambda *_args, **_kwargs: None
        callbacks = []
        executed = []

        def capture_call_after(callback):
            callbacks.append(callback)

        background_thread = object()
        main_thread = object()
        with patch.object(sm.AppHelper, 'callAfter', side_effect=capture_call_after), \
                patch.object(sm.rumps, 'Timer') as timer, \
                patch.object(sm.threading, 'current_thread', return_value=background_thread), \
                patch.object(sm.threading, 'main_thread', return_value=main_thread):
            subject._run_on_main_once(
                lambda: executed.append('ran'), tag='Dock图标弹跳'
            )

        timer.assert_not_called()
        self.assertEqual(len(callbacks), 1)
        self.assertEqual(executed, [])
        callbacks[0]()
        self.assertEqual(executed, ['ran'])

    def test_attention_request_keeps_python_dock_icon_visible(self):
        import stock_menubar as sm

        app = _FakeDockApp()
        subject = object.__new__(StockMenuBar)
        subject._popup_should_show = lambda *_args, **_kwargs: False
        subject.log = lambda *_args, **_kwargs: None

        with patch.object(sm, '_HAS_OBJC', True), \
                patch.object(sm, '_get_ns_app', return_value=app), \
                patch.object(sm.time, 'sleep'), \
                patch.object(sm.subprocess, 'Popen'), \
                patch.dict(sys.modules, {
                    'AppKit': types.SimpleNamespace(NSCriticalRequest=1),
                }):
            subject._raise_urgency(
                '测试标题', '测试副标题', '测试正文', '测试正文',
                'danger', force_popup=False,
            )

        self.assertTrue(app.attention_requests)
        self.assertEqual(app.attention_requests[0][0], 0)
        self.assertEqual(app.policies[-1], 0)

    def test_normal_intraday_notification_requests_informational_attention(self):
        import stock_menubar as sm

        app = _FakeDockApp()
        subject = object.__new__(StockMenuBar)
        subject._popup_should_show = lambda *_args, **_kwargs: False
        subject.log = lambda *_args, **_kwargs: None

        with patch.object(sm, '_HAS_OBJC', True), \
                patch.object(sm, '_get_ns_app', return_value=app), \
                patch.object(sm.time, 'sleep'), \
                patch.object(sm.subprocess, 'Popen'), \
                patch.object(
                    sm.StockMenuBar,
                    '_notify_via_usernotifications',
                    return_value=(True, 'UserNotifications/Active'),
                ), \
                patch.dict(sys.modules, {
                    'AppKit': types.SimpleNamespace(
                        NSCriticalRequest=1,
                        NSInformationalRequest=2,
                    ),
                }):
            sent = subject._notify('盘中提醒', '现价 14.03', '跌破支撑', level='info')

        self.assertTrue(sent)
        self.assertEqual(app.attention_requests, [(0, 2)])


def test_urgent_uses_timesensitive():
    """danger/crash 级必须用 TimeSensitive 中断等级，才能穿透专注模式。"""
    ok, channel = StockMenuBar._notify_via_usernotifications(
        "🛑 测试-穿透专注", "大盘  -3.76%", "正文", True, urgent=True
    )
    assert ok, f"发送失败: {channel}"
    assert 'TimeSensitive' in channel, f"危险级未用 TimeSensitive: {channel}"
    print(f"  ✓ urgent → {channel}")
    return True


def test_normal_uses_active():
    """普通级用 Active，不强行打扰。"""
    ok, channel = StockMenuBar._notify_via_usernotifications(
        "📉 测试-普通", "大盘  -1.21%", "正文", True, urgent=False
    )
    assert ok, f"发送失败: {channel}"
    assert 'Active' in channel, f"普通级通道异常: {channel}"
    assert 'TimeSensitive' not in channel
    print(f"  ✓ normal → {channel}")
    return True


def test_timeout_not_reported_as_success():
    """回调超时绝不能算成功——旧实现在这里静默吞掉失败。"""
    import inspect
    src = inspect.getsource(StockMenuBar._notify_via_usernotifications)
    assert 'return False, "timeout"' in src, "缺少超时判失败分支"
    assert 'result["added"] in (None, "")' not in src, (
        "仍在把 None(超时) 与 \"\"(成功) 混为一谈"
    )
    print("  ✓ 超时按失败处理，不再静默吞掉")
    return True


def test_raise_urgency_has_backup_channels():
    """danger/crash 级必须有通知系统之外的打断手段。"""
    import inspect
    src = inspect.getsource(StockMenuBar._raise_urgency)
    assert 'afplay' in src, "缺少独立音效"
    assert '_request_dock_attention' in src, "缺少 Dock 弹跳"
    assert '_show_alert_dialog' in src, "缺少告警对话框"
    assert 'alert_popup' not in src and 'popup.show' not in src, (
        "置顶横幅卡片已废弃，不应再出现在打断链路"
    )
    dialog_src = inspect.getsource(StockMenuBar._show_alert_dialog)
    assert 'NSAlert' in dialog_src, "缺少模态对话框"
    assert '_run_on_main_once' in dialog_src, "对话框没切回主线程（会触发退出码133）"
    assert "addButtonWithTitle_('知道了')" in dialog_src, "缺统一按钮文案"
    attention_src = inspect.getsource(StockMenuBar._request_dock_attention)
    assert 'requestUserAttention_' in attention_src, "Dock 弹跳未调用系统接口"
    print("  ✓ danger/crash 有告警对话框 + 音效 + Dock 弹跳（置顶横幅已废弃）")
    return True


def test_dialog_carries_level_label():
    """级别必须写进对话框（【危险提醒】【系统性风险】等）。"""
    import inspect
    labels = StockMenuBar._LEVEL_LABELS
    for level in ('crash', 'danger', 'warning', 'info'):
        assert labels.get(level), f"缺少 {level} 级对话框标签"
    dialog_src = inspect.getsource(StockMenuBar._show_alert_dialog)
    assert '【{label}】' in dialog_src, "对话框标题未带级别前缀"
    print("  ✓ 对话框标题标注级别")
    return True


def test_notify_passes_level():
    """大盘预警必须把级别传给通知层，否则无法分级打断。"""
    import inspect
    src = inspect.getsource(StockMenuBar._check_market_alerts)
    assert 'level=merged_alert.level.value' in src, "预警未把 level 传给 _notify"
    print("  ✓ 预警把 level 传给通知层")
    return True


def test_daily_reset_is_called():
    """reset_daily 必须在跨日时被调用，否则昨天的级别会压制今天的预警。"""
    import inspect
    src = inspect.getsource(StockMenuBar._check_market_alerts)
    assert 'reset_daily()' in src, "reset_daily 仍未被调用"
    print("  ✓ 跨日会重置预警状态")
    return True


if __name__ == '__main__':
    tests = [
        test_urgent_uses_timesensitive,
        test_normal_uses_active,
        test_timeout_not_reported_as_success,
        test_raise_urgency_has_backup_channels,
        test_dialog_carries_level_label,
        test_notify_passes_level,
        test_daily_reset_is_called,
    ]
    failed = 0
    for t in tests:
        print(f"[{t.__name__}]")
        try:
            t()
        except AssertionError as e:
            print(f"  ✗ FAIL: {e}")
            failed += 1
        except Exception as e:
            print(f"  ✗ ERROR: {type(e).__name__}: {e}")
            failed += 1
    print()
    print("全部通过" if failed == 0 else f"{failed} 个测试失败")
    sys.exit(1 if failed else 0)
