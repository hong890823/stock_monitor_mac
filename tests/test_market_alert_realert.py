#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""大盘预警"继续下探再提醒"回归测试。

复现 2026-10-08 的真实故障：
创业板 13:24 在 -2.63% 敲响 CRASH 之后，一路砸到 -3.93%，
旧逻辑因 level_triggered 已含 crash 且未反弹到 -0.6% 而全程静默。
"""
import sys
import os
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from market_alert import MarketAlertEngine, IndexSnapshot, AlertLevel


def snap(code, name, pct, ts):
    return IndexSnapshot(
        code=code, name=name, price=1000 + pct, change_pct=pct,
        volume=10.0, timestamp=ts,
    )


def run_sequence(engine, code, name, pcts, start):
    alerts = []
    for i, pct in enumerate(pcts):
        ts = start + timedelta(minutes=i)
        alerts.extend(engine.update(snap(code, name, pct, ts)))
    return alerts


def test_crash_then_further_slide_realers():
    """CRASH 触发后继续下跌，必须再次提醒。"""
    engine = MarketAlertEngine()
    start = datetime(2026, 10, 8, 13, 20)

    # 13:20 -2.63% -> CRASH 触发
    # 之后一路跌到 -3.93%，每 0.3% 一档
    pcts = [-2.63, -2.90, -3.20, -3.50, -3.80, -3.93]
    alerts = run_sequence(engine, 'sz399006', '创业板指', pcts, start)

    first = [a for a in alerts if a.alert_type == 'absolute']
    cont = [a for a in alerts if a.alert_type == 'continue']

    assert first, "首次 CRASH 必须触发"
    assert first[0].level == AlertLevel.CRASH
    assert cont, (
        "CRASH 之后继续大跌必须再次提醒（这正是 2026-10-08 静默故障的回归点）"
    )
    # 每再跌 0.5% 左右应再喊一次；这里至少要有两次
    assert len(cont) >= 2, f"继续下探提醒次数不足: {len(cont)}"
    # 再提醒里必须写明"仍在创新低"和最弱指数
    assert any('仍在创新低' in a.body for a in cont)
    print(f"  ✓ 首次 {len(first)} 条 + 继续下探 {len(cont)} 条")
    return True


def test_headline_uses_worst_index():
    """合并通知标题的跌幅必须取全场最惨指数，不是触发那条的。"""
    engine = MarketAlertEngine()
    start = datetime(2026, 10, 8, 13, 20)

    # 上证只跌 1.21%，够 DANGER
    engine.update(snap('sh000001', '上证指数', -1.21, start))
    # 创业板跌 3.76%，够 CRASH
    engine.update(snap('sz399006', '创业板指', -3.76, start))
    engine.update(snap('sz399001', '深证成指', -2.55, start))

    merged = engine.flush_alerts()
    assert merged is not None
    assert merged.change_pct <= -3.7, (
        f"标题跌幅应取最惨的创业板 -3.76%，却是 {merged.change_pct}"
    )
    assert '创业板' in merged.index_name
    print(f"  ✓ 标题跌幅 {merged.change_pct:+.2f}%（{merged.index_name}）")
    return True


def test_rearm_still_works():
    """反弹到 -0.6% 以上后重新武装，回到 -1.2% 应再次报 DANGER。"""
    engine = MarketAlertEngine()
    start = datetime(2026, 10, 8, 10, 0)

    pcts = [-1.30, -1.35, -0.50, -0.40, -1.30]
    alerts = run_sequence(engine, 'sh000001', '上证指数', pcts, start)
    levels = [a.level for a in alerts if a.alert_type == 'absolute']
    assert AlertLevel.DANGER in levels, "首次应触发 DANGER"
    assert levels.count(AlertLevel.DANGER) >= 2, "重新武装后应再次触发 DANGER"
    print(f"  ✓ 重新武装后再次触发 DANGER（共 {levels.count(AlertLevel.DANGER)} 次）")
    return True


def test_icon_severity_order():
    """CRASH 图标必须比 DANGER 更醒目。"""
    engine = MarketAlertEngine()
    start = datetime(2026, 10, 8, 10, 0)
    engine.update(snap('sh000001', '上证指数', -1.30, start))
    danger = engine.flush_alerts()
    engine2 = MarketAlertEngine()
    engine2.update(snap('sz399006', '创业板指', -3.00, start))
    crash = engine2.flush_alerts()
    assert crash.icon == '🛑', f"CRASH 图标应为 🛑，却是 {crash.icon}"
    assert danger.icon == '🚨'
    assert crash.icon != danger.icon
    print(f"  ✓ CRASH={crash.icon} / DANGER={danger.icon}")
    return True


def test_no_false_realert_on_small_bounce():
    """小幅反弹不应触发继续下探提醒。"""
    engine = MarketAlertEngine()
    start = datetime(2026, 10, 8, 13, 20)
    pcts = [-2.63, -2.55, -2.50, -2.45]
    alerts = run_sequence(engine, 'sz399006', '创业板指', pcts, start)
    cont = [a for a in alerts if a.alert_type == 'continue']
    assert not cont, f"小反弹不该再提醒，却有 {len(cont)} 条"
    print("  ✓ 小幅反弹不误报")
    return True


def test_level_matches_headline_pct():
    """级别必须和标题跌幅对齐，不能出现「转弱」配 -3.56%。"""
    engine = MarketAlertEngine()
    start = datetime(2026, 10, 8, 13, 20)

    # 上证只够 WARNING，创业板已是 CRASH
    engine.update(snap('sh000001', '上证指数', -0.90, start))
    engine.update(snap('sz399006', '创业板指', -3.56, start))

    merged = engine.flush_alerts()
    assert merged is not None
    assert merged.change_pct <= -3.5, f"跌幅应取最惨: {merged.change_pct}"
    assert merged.level == AlertLevel.CRASH, (
        f"跌幅 {merged.change_pct:+.2f}% 应配 CRASH 级，却是 {merged.level.value}"
    )
    assert '严重风险' in merged.title or '🛑' in merged.title
    print(f"  ✓ level={merged.level.value} 与跌幅 {merged.change_pct:+.2f}% 对齐")
    return True


if __name__ == '__main__':
    tests = [
        test_crash_then_further_slide_realers,
        test_headline_uses_worst_index,
        test_level_matches_headline_pct,
        test_rearm_still_works,
        test_icon_severity_order,
        test_no_false_realert_on_small_bounce,
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
