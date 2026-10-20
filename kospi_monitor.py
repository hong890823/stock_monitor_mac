#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
KOSPI 韩国综合股指实时监控脚本
功能：每15分钟查询一次，跌幅超过5%时红色字体预警 + 系统通知
"""

import urllib.request
import re
import time
from datetime import datetime
import sys
import subprocess
import platform

# ANSI颜色代码
class Colors:
    RED = '\033[91m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    PURPLE = '\033[95m'
    CYAN = '\033[96m'
    WHITE = '\033[97m'
    BOLD = '\033[1m'
    UNDERLINE = '\033[4m'
    END = '\033[0m'

# 配置
CHECK_INTERVAL = 15 * 60  # 15分钟（秒）
DROP_THRESHOLD = 5.0      # 跌幅预警阈值（%）
KOSPI_URL = "https://finance.naver.com/sise/sise_index.naver?code=KOSPI"

def get_kospi_data():
    """从Naver Finance获取KOSPI数据"""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
        }
        req = urllib.request.Request(KOSPI_URL, headers=headers)

        with urllib.request.urlopen(req, timeout=15) as response:
            html = response.read().decode('euc-kr')

            # 提取当前价格
            now_match = re.search(r'id="now_value"[^>]*>([\d,]+\.\d+)</em>', html)
            if not now_match:
                return None

            now_value = float(now_match.group(1).replace(',', ''))

            # 提取涨跌幅和涨跌额
            change_match = re.search(
                r'id="change_value_and_rate"[^>]*>'
                r'<span>([\d,]+\.\d+)</span>\s*([+-]?[\d.]+)%',
                html
            )

            if change_match:
                change_value = float(change_match.group(1).replace(',', ''))
                change_rate = float(change_match.group(2))
            else:
                change_value = 0
                change_rate = 0

            # 提取最高价和最低价
            high_match = re.search(r'id="high_value"[^>]*>([\d,]+\.\d+)</td>', html)
            low_match = re.search(r'id="low_value"[^>]*>([\d,]+\.\d+)</td>', html)

            high_value = float(high_match.group(1).replace(',', '')) if high_match else 0
            low_value = float(low_match.group(1).replace(',', '')) if low_match else 0

            # 提取更新时间
            time_match = re.search(r'id="time"[^>]*>([\d.]+\s+[\d:]+)', html)
            update_time = time_match.group(1) if time_match else datetime.now().strftime("%Y.%m.%d %H:%M")

            return {
                'price': now_value,
                'change': change_value,
                'change_rate': change_rate,
                'high': high_value,
                'low': low_value,
                'update_time': update_time
            }

    except Exception as e:
        print(f"{Colors.RED}[错误] 获取数据失败: {e}{Colors.END}")
        return None

def send_notification(title, message):
    """发送系统通知"""
    system = platform.system()

    try:
        if system == "Darwin":  # macOS
            script = f'display notification "{message}" with title "{title}" sound name "Submarine"'
            subprocess.run(["osascript", "-e", script], check=True)
        elif system == "Linux":
            subprocess.run(["notify-send", title, message], check=True)
        elif system == "Windows":
            # Windows 10/11 toast notification
            ps_script = f'''
            [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
            [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null

            $template = @"
            <toast>
                <visual>
                    <binding template="ToastGeneric">
                        <text>{title}</text>
                        <text>{message}</text>
                    </binding>
                </visual>
                <audio src="ms-winsoundevent:Notification.Looping.Alarm"/>
            </toast>
"@

            $xml = New-Object Windows.Data.Xml.Dom.XmlDocument
            $xml.LoadXml($template)
            $toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
            [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier("KOSPI Monitor").Show($toast)
            '''
            subprocess.run(["powershell", "-Command", ps_script], check=True)
    except Exception as e:
        print(f"{Colors.YELLOW}[通知] 系统通知发送失败: {e}{Colors.END}")

def format_number(num, decimals=2):
    """格式化数字，添加千位分隔符"""
    if decimals == 0:
        return f"{num:,.0f}"
    return f"{num:,.{decimals}f}"

def display_data(current, previous_price=None):
    """显示KOSPI数据"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    print("\n" + "=" * 60)
    print(f"{Colors.CYAN}{Colors.BOLD}📊 KOSPI 韩国综合股指监控{Colors.END}")
    print("=" * 60)
    print(f"⏰ 查询时间: {now}")
    print(f"📡 数据更新: {current['update_time']}")
    print("-" * 60)

    # 判断涨跌颜色
    if current['change_rate'] > 0:
        change_color = Colors.GREEN
        change_symbol = "📈"
        change_text = "涨"
    elif current['change_rate'] < 0:
        change_color = Colors.RED
        change_symbol = "📉"
        change_text = "跌"
    else:
        change_color = Colors.WHITE
        change_symbol = "➡️"
        change_text = "平"

    # 显示当前价格
    print(f"💰 当前价格: {Colors.BOLD}{format_number(current['price'])}{Colors.END}")
    print(f"{change_symbol} 今日涨跌: {change_color}{format_number(current['change'])} ({current['change_rate']:+.2f}%){Colors.END} [{change_text}]")
    print(f"📊 今日最高: {Colors.GREEN}{format_number(current['high'])}{Colors.END}")
    print(f"📉 今日最低: {Colors.RED}{format_number(current['low'])}{Colors.END}")

    # 计算与上次查询的对比
    if previous_price is not None:
        diff = current['price'] - previous_price
        diff_rate = (diff / previous_price) * 100

        print("-" * 60)
        print(f"🔄 与上次查询对比:")

        if diff > 0:
            print(f"   价格变化: {Colors.GREEN}+{format_number(diff)} (+{diff_rate:.2f}%){Colors.END}")
        elif diff < 0:
            print(f"   价格变化: {Colors.RED}{format_number(diff)} ({diff_rate:.2f}%){Colors.END}")

            # 跌幅预警
            if abs(diff_rate) >= DROP_THRESHOLD:
                print("\n" + "!" * 60)
                print(f"{Colors.RED}{Colors.BOLD}⚠️  跌幅预警！{Colors.END}")
                print(f"{Colors.RED}{Colors.BOLD}   KOSPI较上次查询下跌 {abs(diff_rate):.2f}%{Colors.END}")
                print(f"{Colors.RED}{Colors.BOLD}   上次价格: {format_number(previous_price)}{Colors.END}")
                print(f"{Colors.RED}{Colors.BOLD}   当前价格: {format_number(current['price'])}{Colors.END}")
                print("!" * 60)

                # 发送系统通知
                send_notification(
                    "⚠️ KOSPI 跌幅预警",
                    f"KOSPI较上次查询下跌 {abs(diff_rate):.2f}%\n当前价格: {format_number(current['price'])}"
                )
        else:
            print(f"   价格变化: {Colors.WHITE}持平{Colors.END}")

    print("=" * 60)

    # 每次查询都发送系统通知
    notification_msg = f"当前价格: {format_number(current['price'])}\n"
    notification_msg += f"今日涨跌: {current['change_rate']:+.2f}%"

    if previous_price is not None:
        diff = current['price'] - previous_price
        diff_rate = (diff / previous_price) * 100
        notification_msg += f"\n较上次: {diff_rate:+.2f}%"

    send_notification("📊 KOSPI 数据更新", notification_msg)

def main():
    """主函数"""
    print(f"{Colors.CYAN}{Colors.BOLD}")
    print("╔═══════════════════════════════════════════════════════════╗")
    print("║         KOSPI 韩国综合股指实时监控系统                   ║")
    print("║         每15分钟自动查询 | 跌幅超5%红色预警              ║")
    print("╚═══════════════════════════════════════════════════════════╝")
    print(f"{Colors.END}")

    print(f"⚙️  配置信息:")
    print(f"   查询间隔: {CHECK_INTERVAL // 60} 分钟")
    print(f"   预警阈值: 跌幅 {DROP_THRESHOLD}%")
    print(f"   数据源: Naver Finance")
    print()

    previous_price = None
    check_count = 0

    try:
        while True:
            check_count += 1
            print(f"\n🔍 第 {check_count} 次查询...")

            # 获取数据
            data = get_kospi_data()

            if data:
                display_data(data, previous_price)
                previous_price = data['price']
            else:
                print(f"{Colors.YELLOW}[警告] 本次查询未获取到数据，将在下次重试{Colors.END}")

            # 等待下次查询
            if check_count > 0:  # 第一次查询后开始计时
                print(f"\n⏳ 等待 {CHECK_INTERVAL // 60} 分钟后进行下次查询...")
                print(f"   (按 Ctrl+C 可随时停止监控)")

                # 分段睡眠，以便响应中断
                for i in range(CHECK_INTERVAL):
                    time.sleep(1)
                    # 每60秒显示一次倒计时
                    if (i + 1) % 60 == 0:
                        remaining = (CHECK_INTERVAL - i - 1) // 60
                        if remaining > 0:
                            sys.stdout.write(f"\r   剩余时间: {remaining} 分钟...")
                            sys.stdout.flush()

                print()  # 换行

    except KeyboardInterrupt:
        print(f"\n\n{Colors.YELLOW}⏹️  监控已停止{Colors.END}")
        print(f"📊 共完成 {check_count} 次查询")
        print(f"👋 再见！")
        sys.exit(0)

if __name__ == "__main__":
    main()
