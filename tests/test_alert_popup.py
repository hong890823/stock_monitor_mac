#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""置顶告警横幅回归测试。

背景：com.apple.python3 的 timeSensitiveSetting=0，TimeSensitive 中断等级
会被系统降级；专注模式下系统通知只进通知列表、不弹横幅。2026-10-08 大盘
跌 3.7% 因此全程零横幅。置顶横幅是与通知系统完全解耦的兜底通道。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from stock_menubar import StockMenuBar, AlertPopup, _HAS_OBJC
import stock_menubar as _sm


def test_popup_class_exists():
    assert _HAS_OBJC, "PyObjC 不可用，置顶横幅无法创建"
    p = AlertPopup()
    assert p._window is None, "应懒创建，__init__ 阶段不建窗（NSApplication 可能未就绪）"
    p.show("t", "s", "b", level='danger')
    assert p._window is not None, "首次 show() 后应创建窗口"
    print("  ✓ AlertPopup 懒创建，首次 show 时建窗")
    return True


def test_window_level_is_above_floating():
    """必须高于普通浮动窗口，才能压在其他窗口之上。"""
    p = AlertPopup()
    p.show("t", "s", "b", level='danger')
    level = p._window.level()
    assert level >= 3, f"窗口层级过低: {level}（NSScreenSaverWindowLevel 通常为 1000+）"
    print(f"  ✓ 窗口层级 = {level}（高于普通浮动窗口）")
    return True


def test_show_sets_text_and_makes_visible():
    p = AlertPopup()
    p.show("🛑 大盘严重风险", "创业板指  -3.76%", "正文内容", level='crash')
    assert p.visible, "show() 后窗口应可见"
    assert '大盘严重风险' in str(p._title_field.stringValue())
    assert '-3.76%' in str(p._subtitle_field.stringValue())
    assert p.frame() is not None, "frame() 应返回位置尺寸用于诊断"
    p.hide()
    assert not p.visible
    print(f"  ✓ show/hide 与文案绑定正常（frame={p.frame() if p.frame() else '已隐藏'}）")
    return True


def test_level_colors_differ():
    """不同级别配色必须不同，crash 要最刺眼。"""
    assert AlertPopup.COLORS['crash'] != AlertPopup.COLORS['danger']
    assert AlertPopup.COLORS['danger'] != AlertPopup.COLORS['warning']
    crash_bg = AlertPopup.COLORS['crash'][0]
    danger_bg = AlertPopup.COLORS['danger'][0]
    # crash 背景红色分量要高于 danger
    assert crash_bg[0] > danger_bg[0], f"crash 配色不够红: {crash_bg} vs {danger_bg}"
    print("  ✓ 三级配色区分明确，crash 最红")
    return True


def test_raise_urgency_uses_popup_as_primary():
    """告警打断必须以置顶横幅为主通道，不能只靠通知。"""
    import inspect
    src = inspect.getsource(StockMenuBar._raise_urgency)
    assert 'popup.show(' in src or 'alert_popup.show' in src, (
        "_raise_urgency 未调用置顶横幅"
    )
    assert '_run_on_main_once' in src, "置顶横幅没切回主线程（会触发退出码133）"
    idx_popup = src.index('_show_popup')
    idx_sound = src.index('afplay')
    assert idx_popup < idx_sound, "置顶横幅必须排在音效之前（它是主通道）"
    print("  ✓ _raise_urgency 以置顶横幅为主通道，且在主线程执行")
    return True


def test_app_wires_popup():
    """StockMenuBar 必须挂上 alert_popup 实例。"""
    import inspect
    src = inspect.getsource(StockMenuBar.__init__)
    assert 'self.alert_popup = AlertPopup()' in src
    print("  ✓ StockMenuBar 已挂载 alert_popup")
    return True


def test_test_notification_exercises_urgent_path():
    """「测试通知」按钮必须测 danger 级，否则测不到置顶横幅。"""
    import inspect
    src = inspect.getsource(StockMenuBar.test_notification)
    assert "level='danger'" in src or 'level="danger"' in src, (
        "测试通知没走 danger 级，验不到真实告警链路"
    )
    print("  ✓ 测试通知走 danger 级完整链路")
    return True


def test_popup_cooldown_prevents_spam():
    """三个指数轮流触发会合并成 CRASH，必须节流，否则每 15 秒弹一次模态框。"""
    class Dummy(StockMenuBar):
        def __init__(self):
            # 跳过 rumps.App 的重初始化，只测节流逻辑
            self._popup_last_time = 0
            self._popup_last_level = None

    d = Dummy()
    now = __import__('time').time()

    # 首次：应弹
    assert d._popup_should_show('crash'), "首次必须弹"
    # 紧接着同级：应跳过
    assert not d._popup_should_show('crash'), "同级连续触发必须节流"
    # 级别上升：立即弹
    assert d._popup_should_show('crash', force=True)  # 先把状态置为 danger 之上
    d._popup_last_level = 'warning'
    assert d._popup_should_show('crash'), "级别上升必须立即弹"
    # 冷却过后：再弹
    d._popup_last_time = now - 181
    assert d._popup_should_show('warning'), "冷却过后应允许再弹"
    # force 无视冷却
    d._popup_last_time = now
    assert d._popup_should_show('info', force=True), "force 必须无视冷却"
    print("  ✓ 冷却节流 + 级别上升立即弹 + force 无视冷却")
    return True


def test_test_notification_forces_popup():
    """手动测试必须无视冷却，否则用户会误判成坏了。"""
    import inspect
    src = inspect.getsource(StockMenuBar.test_notification)
    assert 'force_popup=True' in src
    print("  ✓ 测试通知强制弹出")
    return True


def test_run_on_main_once_is_one_shot():
    """rumps.Timer 是重复定时器，必须自行 stop，否则 0.001s 间隔会变成忙循环。"""
    import inspect
    src = inspect.getsource(StockMenuBar._run_on_main_once)
    assert 'timer.stop()' in src, "未停止重复定时器，会变成每秒千次忙循环"
    assert "state['done']" in src, "缺少一次性保护"
    assert 'threading.main_thread()' in src, "未处理已在主线程的情况"
    print("  ✓ 主线程调度为一次性，且已在主线程时直接调用")
    return True


def test_popup_dispatch_uses_one_shot_helper():
    """置顶横幅与模态框都必须走一次性调度，不能直接 rumps.Timer。"""
    import inspect, re
    src = inspect.getsource(StockMenuBar._raise_urgency)
    assert '_run_on_main_once' in src
    # 不应再有 rumps.Timer(...) 直接用作一次性调度
    for m in re.finditer(r'rumps\.Timer\([^)]*0\.001', src):
        raise AssertionError("仍在用 rumps.Timer(..., 0.001) 当一次性调度")
    print("  ✓ 置顶横幅/模态框均走一次性主线程调度")
    return True


def test_popup_logs_success():
    """必须记录横幅是否真的显示，否则失败时无迹可查。"""
    import inspect
    src = inspect.getsource(StockMenuBar._raise_urgency)
    assert '置顶横幅已显示' in src, "缺少横幅显示成功日志"
    print("  ✓ 横幅显示结果会写进日志")
    return True


def test_text_bands_do_not_overlap():
    """四段文案控件的 frame 必须自上而下互不相交。

    曾经 h=168 时正文 frame(y=52..118) 把副标题(y=88..110)整条框在里面，
    而正文后 addSubview 画在上层，3 行正文的头两行直接压住副标题文字。
    """
    p = AlertPopup()
    p.show("🚨 告警打断测试", "danger 级 · 与盘中暴跌同一链路",
           "① 通知：…\n② 横幅：…\n③ 音效：…", level='danger')

    def band(view):
        f = view.frame()
        return f.origin.y, f.origin.y + f.size.height

    bands = [
        ('title', band(p._title_field)),
        ('subtitle', band(p._subtitle_field)),
        ('body', band(p._body_view)),
        ('button', band(p._dismiss_btn)),
    ]
    # 从窗口顶部往下走，每段的 top 都不得超过上一段的 bottom
    prev_top = p._window.frame().size.height
    for name, (bot, top) in bands:
        assert top <= prev_top + 0.5, (
            f"{name} 与上一段文案重叠: top={top:.1f} > prev_bottom={prev_top:.1f}"
        )
        prev_top = bot
    print(f"  ✓ 四段文案互不相交: {bands}")
    return True


def test_test_notification_copy_not_redundant():
    """subtitle 与正文不能把同一份内容说两遍。

    曾经 subtitle「置顶横幅 + 通知 + 音效」列一遍三通道，
    正文 ①②③ 又把同样的三通道各讲一遍，③ 的「完整链路」再复述 subtitle。
    """
    import inspect, re
    src = inspect.getsource(StockMenuBar.test_notification)
    # 只看 _notify(...) 的文案实参，日志行不算用户可见文案
    m = re.search(r'self\._notify\((.*?)level=', src, re.S)
    assert m, "未找到 _notify 调用"
    copy = m.group(1)
    assert '置顶横幅 + 通知 + 音效' not in copy, (
        "subtitle 仍在枚举通道，与正文 ①②③ 重复"
    )
    assert '完整链路' not in copy, "文案仍在用「完整链路」复述 subtitle"
    assert '这条' not in copy, "「这条」指代在通知/横幅两个面都会出现，应改为点名通道"
    print("  ✓ 测试文案无 subtitle/正文互相复述")
    return True


def test_test_notification_splits_popup_body():
    """通知和置顶横幅必须各带各的文案，不能同一串字弹两份。

    两个面会同时出现在屏幕上；共用 body 的话用户看到两份一样的内容，
    反而看不出自己在验哪一层。
    """
    import inspect, re
    src = inspect.getsource(StockMenuBar.test_notification)
    assert 'popup_body=' in src, "测试通知未分流 popup_body，两面仍是同一串字"

    m = re.search(
        r'self\._notify\(\s*"(?P<title>[^"]*)",\s*"(?P<subtitle>[^"]*)",\s*'
        r'(?P<body>(?:"[^"]*"\s*)+),\s*level=.*?popup_body=(?P<popup>(?:"[^"]*"\s*)+)',
        src, re.S,
    )
    assert m, "未能解析 _notify 的 body / popup_body 实参"
    body = m.group('body')
    popup = m.group('popup')
    assert body != popup, "body 与 popup_body 完全相同，等于没分流"
    # 通知正文讲通知，横幅正文讲横幅，各说各的
    assert '系统通知' in body, f"通知正文未点名通知面: {body!r}"
    assert '置顶横幅' not in body, f"通知正文不该替横幅发言: {body!r}"
    assert '置顶横幅' in popup, f"横幅正文未点名横幅面: {popup!r}"
    assert '系统通知' not in popup or '系统通知那边' in popup, (
        f"横幅正文不该替通知发言: {popup!r}"
    )
    print("  ✓ 通知/横幅各带各的文案（body ≠ popup_body）")
    return True


def test_notify_forwards_popup_body():
    """popup_body 必须真的传到 _raise_urgency，否则分流只改了签名。"""
    import inspect
    src = inspect.getsource(StockMenuBar._notify)
    assert src.count('_raise_urgency(') == 3, "通知有三条兜底通道，每条都要传 popup_body"
    assert src.count('popup_body, level_name') == 3, (
        "存在 _raise_urgency 调用没带上 popup_body"
    )
    # 缺省行为不变：没传就复用 body
    assert 'popup_body is None' in src and 'popup_body = body' in src, (
        "popup_body 缺省应复用 body，真预警行为不能变"
    )
    urg = inspect.getsource(StockMenuBar._raise_urgency)
    assert 'popup_body' in urg, "_raise_urgency 未接收 popup_body"
    assert 'popup.show(title, subtitle, popup_body' in urg, "置顶横幅没用 popup_body"
    print("  ✓ popup_body 贯通三条通道，缺省仍复用 body")
    return True


def test_dismiss_button_actually_closes():
    """按钮必须真能关掉横幅。

    NSButton 的 setTarget_ 要求 NSObject 子类；用纯 Python 对象当 target
    分发不到 ObjC selector，按钮点了没反应且不报错。
    """
    p = AlertPopup()
    p.show("🛑 测试", "创业板指  -3.76%", "正文", level='crash')
    assert p.visible, "show 后应可见"

    # 真实触发按钮的 target/action
    p._dismiss_btn.performClick_(None)

    assert not p.visible, "点「知道了」后横幅应关闭"
    assert p._btn_target is not None, "缺少按钮 target 代理"
    assert hasattr(p._btn_target, 'onPress_'), "target 必须暴露 onPress: selector"
    print("  ✓ 点「知道了」能真正关闭横幅")
    return True


def test_btn_target_is_nsobject():
    """按钮 target 必须是 NSObject 子类，否则 selector 分发失败。"""
    from Foundation import NSObject as _NSO
    target_cls = getattr(_sm, '_AlertButtonTarget', None)
    assert target_cls is not None, "缺少 _AlertButtonTarget"
    assert issubclass(target_cls, _NSO), (
        f"按钮 target 必须继承 NSObject，实际是 {target_cls.__mro__}"
    )
    print("  ✓ 按钮 target 是 NSObject 子类")
    return True


if __name__ == '__main__':
    tests = [
        test_popup_class_exists,
        test_window_level_is_above_floating,
        test_show_sets_text_and_makes_visible,
        test_level_colors_differ,
        test_raise_urgency_uses_popup_as_primary,
        test_app_wires_popup,
        test_test_notification_exercises_urgent_path,
        test_popup_cooldown_prevents_spam,
        test_test_notification_forces_popup,
        test_run_on_main_once_is_one_shot,
        test_popup_dispatch_uses_one_shot_helper,
        test_popup_logs_success,
        test_btn_target_is_nsobject,
        test_text_bands_do_not_overlap,
        test_test_notification_copy_not_redundant,
        test_test_notification_splits_popup_body,
        test_notify_forwards_popup_body,
        test_dismiss_button_actually_closes,
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
