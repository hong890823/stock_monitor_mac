#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模拟盘中信号通知测试。

由于当前非交易时段，通过模拟数据测试通知功能。
"""

import rumps
from intraday_tactics import IntradayTactics, limit_pct_for_board


def test_notification():
    """测试各种信号的通知。"""
    tactics = IntradayTactics()

    # 模拟场景
    scenarios = [
        {
            "name": "跌破支撑",
            "params": dict(
                code="sh600519", name="贵州茅台", price=1750.0,
                change_pct=-2.0, volume_ratio=1.0, limit_pct=10.0,
                support=1780.0, resistance=1850.0, score=3, quantity=100,
            ),
        },
        {
            "name": "放量突破",
            "params": dict(
                code="sz300750", name="宁德时代", price=210.0,
                change_pct=3.5, volume_ratio=1.8, limit_pct=20.0,
                support=190.0, resistance=205.0, score=4, quantity=0,
            ),
        },
        {
            "name": "冲高做T",
            "params": dict(
                code="sh600522", name="中天科技", price=18.5,
                change_pct=4.0, volume_ratio=1.2, limit_pct=10.0,
                support=17.0, resistance=18.2, score=3, quantity=300,
            ),
        },
        {
            "name": "回踩接回",
            "params": dict(
                code="sh600522", name="中天科技", price=17.3,
                change_pct=-3.5, volume_ratio=0.8, limit_pct=10.0,
                support=17.2, resistance=18.5, score=3, quantity=300,
            ),
        },
        {
            "name": "回踩支撑",
            "params": dict(
                code="sh600884", name="杉杉股份", price=15.2,
                change_pct=-1.0, volume_ratio=0.9, limit_pct=10.0,
                support=15.0, resistance=16.5, score=3, quantity=0,
            ),
        },
    ]

    print("=" * 50)
    print("🔔 盘中买卖点通知测试")
    print("=" * 50)

    for scenario in scenarios:
        print(f"\n📍 场景: {scenario['name']}")
        print(f"   股票: {scenario['params']['name']} ({scenario['params']['code']})")
        print(f"   价格: {scenario['params']['price']}")
        print(f"   涨跌: {scenario['params']['change_pct']:+.2f}%")
        print(f"   量比: {scenario['params']['volume_ratio']}")

        signal = tactics.check_signal(**scenario["params"])

        if signal:
            print(f"   ✅ 触发信号: {signal.title}")
            print(f"   📝 详情: {signal.body}")

            # 发送 macOS 通知
            try:
                rumps.notification(
                    signal.title,
                    f"现价 {scenario['params']['price']:.2f}",
                    signal.body,
                    sound=True,
                )
                print("   🔔 通知已发送!")
            except Exception as e:
                print(f"   ❌ 通知发送失败: {e}")
        else:
            print("   ⚠️ 未触发信号（可能已被触发过或条件不满足）")

    print("\n" + "=" * 50)
    print("测试完成！")
    print("=" * 50)


if __name__ == "__main__":
    test_notification()
